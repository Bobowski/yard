import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import package_version
from app.cli import (
    Client,
    as_json,
    format_deploys,
    format_exps,
    format_inspect,
    format_record,
    format_repos,
    format_slot_table,
    format_tokens,
    judge_swap,
    main,
    parse_slot_sets,
    slot_hint,
    slot_patch,
    split_args,
)
from app.config import Config, hook_url
from app.errors import YardError
from app.machine import (
    JOB_RETRY,
    Paths,
    RunState,
    caddy_document,
    caddy_run_args,
    containerfile,
    image_tag,
    job_due,
    plan_job,
    routes_from,
    yard_route,
)
from app.podman import (
    Podman,
    ids_for_slot,
    inspect_args,
    latest_for_slot,
    page_log_rows,
    page_logs,
    parse_info,
    parse_inspect,
    parse_ps,
    parse_stats,
    ps_args,
    stats_args,
)
from app.rows import SlotRow, build_label, dir_size, slot_label, slot_rows, total_row
from app.store import Slot, Store
from app.text import slot_env
from app.yard import _SWAP, Yard


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]):
    main(["--version"])
    assert capsys.readouterr().out.strip() == package_version()
    main(["version"])
    assert capsys.readouterr().out.strip() == package_version()


def test_help_is_a_short_list(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert f"yard {package_version()}" in out
    assert "git push -u yard main" in out
    assert "Save the host and the token" in out
    assert "yard <command> --help" in out


def test_help_flag_lists_the_commands(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "revoke" in out
    assert "https://github.com/Bobowski/yard" in out


def test_unknown_command_exits(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["nope"])
    assert caught.value.code == 2
    assert "Show, log, and edit apps" in capsys.readouterr().err


def test_a_missing_verb_lists_the_verbs(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["slot"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "logs <name>" in out
    assert "Read the container log" in out
    assert "deploys <name>" in out


def test_half_a_command_prints_its_flags(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["slot", "logs"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "yard slot logs <name>" in err
    assert "--follow" in err
    assert "Keep reading" in err


def test_help_flag_on_a_command(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["help", "slot", "logs"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "yard slot logs <name>" in out
    assert "--tail <n>" in out


def test_records_stay_short():
    secret = format_record('{"name":"ci","token":"yard-secret","parent":"admin"}')
    assert secret == "ci\nyard-secret\n"
    assert "parent" not in secret
    settings = format_record(
        '{"domain":"yard.example.com","email":"","self_repo":"","self_ref":"main","self_image":""}'
    )
    assert "yard.example.com" in settings
    assert "ghcr.io/bobowski/yard:latest" in settings
    assert "self_repo" not in settings
    assert format_record('{"name":"heldso","created":"t"}') == "heldso\n"


def test_deploy_list_drops_the_repeated_columns():
    started = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ended = (datetime.now(UTC) - timedelta(hours=3) + timedelta(seconds=19)).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = format_deploys(
        [
            {
                "id": 4,
                "slot": "honorata",
                "repo": "honorata",
                "status": "ok",
                "sha": "dfa7dd027e8bd735",
                "started": started,
                "ended": ended,
            }
        ]
    )
    assert text.splitlines()[0].split() == ["id", "status", "rev", "took", "when"]
    assert "dfa7dd0" in text
    assert "dfa7dd027e8b" not in text
    assert "honorata" not in text
    assert "3h" in text
    failed = format_deploys([{"id": 5, "status": "failed", "sha": "abc", "started": started}], color=True)
    assert "\033[31m" in failed
    assert "took" not in failed.splitlines()[0]


def test_formats_stay_on_their_paths(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert as_json(split_args(["slot", "list"])) is True
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert as_json(split_args(["slot", "list"])) is False
    assert as_json(split_args(["--json", "slot", "list"])) is True
    assert as_json(split_args(["--text", "slot", "list"])) is False


def test_every_list_uses_the_same_table():
    started = (datetime.now(UTC) - timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = format_repos([{"name": "heldso", "created": started}])
    assert repos.splitlines()[0].split() == ["repo", "when"]
    assert "12m" in repos
    assert "\033[" not in repos
    tokens = format_tokens(
        [
            {"name": "ci", "parent": "admin", "revoked": False},
            {"name": "old", "parent": "admin", "revoked": True},
        ],
        color=True,
    )
    assert "revoked" in tokens
    assert "\033[31m" in tokens
    assert "state" not in format_tokens([{"name": "ci", "parent": "admin", "revoked": False}])
    assert format_exps([{"id": 1, "title": "first", "labels": []}]).splitlines()[0].split() == ["id", "title"]
    raw = json.dumps(
        [
            {
                "Name": "/honorata",
                "State": {"Status": "running", "ExitCode": 0, "StartedAt": started},
                "ImageName": "ghcr.io/bobowski/honorata:latest",
            }
        ]
    )
    inspected = format_inspect(raw, color=True)
    assert inspected.splitlines()[0].startswith("\033[2m")
    assert "up 12m" in inspected
    failed = format_inspect(
        json.dumps([{"Name": "/shop", "State": {"Status": "exited", "ExitCode": 1, "StartedAt": started}}]),
        color=True,
    )
    assert "failed" in failed
    assert "\033[31m" in failed


def test_slot_table_is_one_line_per_app():
    text = format_slot_table(
        [
            {
                "slot": "honorata",
                "container": "honorata",
                "state": "running",
                "status": "Up 18 minutes",
                "cpu": "0.40%",
                "mem": "116.1MB",
                "sha": "dfa7dd027e8bd735",
                "expected": "dfa7dd027e8bd735",
                "build": "ok 47s",
            },
            {
                "slot": "shop",
                "container": "shop",
                "state": "running",
                "status": "Exited (1) 4 minutes ago",
                "health": "unhealthy",
                "cpu": "1%",
                "mem": "20MB",
                "data": "12.4MB",
                "sha": "aaaaaaabbbbb",
                "expected": "cccccccddddd",
                "build": "failed",
            },
            {
                "slot": "caddy",
                "container": "caddy",
                "state": "running",
                "status": "Up About an hour",
                "cpu": "0.05%",
                "mem": "17.13MB",
            },
            {"slot": "total", "cpu": "1.45% / 1200%", "mem": "153.5MB / 67.32GB", "data": "12.4MB / 468GB"},
        ]
    )
    assert "HEALTH" not in text
    assert "ok 47s" not in text
    assert "up 18m" in text
    assert "dfa7dd0" in text
    assert "dfa7dd027e8b" not in text
    assert "unhealthy · failed" in text
    assert "aaaaaaa → ccccccc" in text
    assert "up 1h" in text
    assert "1.45% of 12 cores" in text
    assert "153.5 MB of 67.32 GB" in text
    assert "116.1 MB" in text
    assert text.splitlines()[0].split() == ["name", "state", "cpu", "mem", "data", "rev"]
    assert text.splitlines()[-1] == "1.45% of 12 cores · 153.5 MB of 67.32 GB · 12.4 MB of 468 GB"
    quiet = format_slot_table(
        [
            {
                "slot": "shop",
                "container": "shop",
                "state": "running",
                "status": "Up 1 minute",
                "cpu": "0%",
                "mem": "1MB",
                "data": "0B",
                "sha": "abcdef0",
                "expected": "abcdef0",
            }
        ]
    )
    assert "0 B" not in quiet
    assert "0%" in quiet


def test_color_marks_only_a_slot_that_needs_attention():
    text = format_slot_table(
        [
            {
                "slot": "honorata",
                "container": "honorata",
                "state": "running",
                "status": "Up 18 minutes",
                "cpu": "0.40%",
                "mem": "116.1MB",
            },
            {
                "slot": "shop",
                "container": "shop",
                "state": "exited",
                "status": "Exited (1) 4 minutes ago",
                "health": "failed",
                "cpu": "0%",
                "mem": "1MB",
            },
        ],
        color=True,
    )
    lines = text.splitlines()
    assert lines[0].startswith("\033[2m")
    honorata = next(line for line in lines if "honorata" in line)
    shop = next(line for line in lines if "shop" in line)
    assert "\033[31m" not in honorata
    assert "\033[33m" not in honorata
    assert "\033[31m" in shop
    assert (
        slot_hint(
            [
                {
                    "slot": "shop",
                    "container": "shop",
                    "state": "exited",
                    "health": "failed",
                    "status": "Exited (1) 1 minute ago",
                }
            ]
        )
        == "yard slot logs shop"
    )
    assert slot_hint([{"slot": "honorata", "container": "honorata", "state": "running", "status": "Up 1 minute"}]) == ""


def test_login_keeps_url_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "config.json"
    monkeypatch.setattr("app.cli.config_path", lambda: path)
    main(["login", "--url", "yard.bobowski.dev", "--token", "secret"])
    assert path.read_text() == '{\n  "url": "https://yard.bobowski.dev",\n  "token": "secret"\n}\n'
    assert capsys.readouterr().out.strip() == "https://yard.bobowski.dev\n" + str(path)
    with pytest.raises(SystemExit):
        main(["login"])


def _init_api(repo_status: int = 200, repo_body: str = '{"name":"heldso","created":"t"}'):
    calls: list[tuple[str, str, object]] = []

    def fake(_client: Client, method: str, path: str, body: object) -> tuple[int, str]:
        calls.append((method, path, body))
        if path == "/api/v1/repos":
            return repo_status, repo_body
        return 200, '{"deploy":{"status":"waiting"}}'

    return calls, fake


def _no_push(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = subprocess.run

    def run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list):
            seen.append(cmd)
            if "push" in cmd:
                raise AssertionError(cmd)
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_init_creates_the_repo_and_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "heldso"
    root.mkdir()
    monkeypatch.chdir(root)
    calls, fake = _init_api()
    monkeypatch.setattr("app.cli._load", lambda _opt: Client("https://yard.example.com", "secret"))
    monkeypatch.setattr("app.cli._api_try", fake)
    seen = _no_push(monkeypatch)
    main(["--text", "init", "--domain", "heldso.example.com"])
    text = capsys.readouterr().out
    assert "repo    heldso" in text
    assert "remote  https://yard.example.com/git/heldso.git" in text
    assert "commit, then" in text
    assert "git push -u yard main" in text
    assert calls[0][0:2] == ("POST", "/api/v1/repos")
    assert calls[1] == (
        "POST",
        "/api/v1/slots",
        {"SLOT_NAME": "heldso", "SLOT_REPO": "heldso", "SLOT_BRANCH": "main", "SLOT_DOMAIN": "heldso.example.com"},
    )
    assert not any("push" in cmd for cmd in seen)
    assert (root / ".git").exists()
    remote = subprocess.run(["git", "remote", "get-url", "yard"], cwd=root, capture_output=True, text=True, check=True)
    assert remote.stdout.strip() == "https://yard.example.com/git/heldso.git"


def test_init_keeps_an_existing_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "heldso"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    (root / "README").write_text("hi\n")
    subprocess.run(["git", "add", "README"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-m", "start"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "app").mkdir()
    monkeypatch.chdir(root / "app")
    calls, fake = _init_api(409, '{"error":{"code":"exists","message":"already exists"}}')
    monkeypatch.setattr("app.cli._load", lambda _opt: Client("https://yard.example.com", "secret"))
    monkeypatch.setattr("app.cli._api_try", fake)
    seen = _no_push(monkeypatch)
    main(["init", "--every", "5m", "--name", "heldso"])
    body = json.loads(capsys.readouterr().out)
    assert body["created"] is False
    assert body["push"] == "git push -u yard main"
    assert body["name"] == "heldso"
    assert calls[1] == (
        "POST",
        "/api/v1/slots",
        {"SLOT_NAME": "heldso", "SLOT_REPO": "heldso", "SLOT_BRANCH": "main", "SLOT_INTERVAL": "5m"},
    )
    assert not any(cmd[0] == "git" and "init" in cmd for cmd in seen)
    assert not (root / "app" / ".git").exists()


def test_init_can_split_the_repo_and_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.chdir(root)
    calls, fake = _init_api()
    monkeypatch.setattr("app.cli._load", lambda _opt: Client("https://yard.example.com", "secret"))
    monkeypatch.setattr("app.cli._api_try", fake)
    _no_push(monkeypatch)
    main(["--text", "init", "--domain", "heldso.example.com", "--repo", "heldso-docs", "--slot", "heldso"])
    text = capsys.readouterr().out
    assert "repo    heldso-docs" in text
    assert "slot    heldso" in text
    assert calls[0][2] == {"name": "heldso-docs", "force": False}
    assert calls[1][2] == {
        "SLOT_NAME": "heldso",
        "SLOT_REPO": "heldso-docs",
        "SLOT_BRANCH": "main",
        "SLOT_DOMAIN": "heldso.example.com",
    }
    remote = subprocess.run(["git", "remote", "get-url", "yard"], cwd=root, capture_output=True, text=True, check=True)
    assert remote.stdout.strip() == "https://yard.example.com/git/heldso-docs.git"


def test_init_needs_a_domain_or_an_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "heldso"
    root.mkdir()
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit):
        main(["init"])
    assert not (root / ".git").exists()


def test_init_rejects_a_bad_directory_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "Heldso"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr("app.cli._load", lambda _opt: Client("https://yard.example.com", "secret"))

    def fail(*_args):  # type: ignore[no-untyped-def]
        raise AssertionError("api")

    monkeypatch.setattr("app.cli._api_try", fail)
    with pytest.raises(SystemExit):
        main(["init", "--domain", "heldso.example.com"])
    assert not (root / ".git").exists()


def test_split_args_keeps_the_name_before_flags():
    opt = split_args(["slot", "create", "heldso", "--repo", "heldso", "--domain", "heldso.example.com", "--json"])
    assert opt.json
    assert opt.rest == ["slot", "create", "heldso", "--repo", "heldso", "--domain", "heldso.example.com"]


def test_parse_slot_sets():
    sets = parse_slot_sets(["--set", "SLOT_DOMAIN", "heldso.com", "--set", "API_TOKEN=secret", "--set", "OLD="])
    assert sets == [("SLOT_DOMAIN", "heldso.com"), ("API_TOKEN", "secret"), ("OLD", "")]
    with pytest.raises(ValueError):
        parse_slot_sets(["--domain", "heldso.com"])


def test_slot_patch_removes_empty():
    patch = slot_patch(
        {"SLOT_DOMAIN": "heldso.com", "API_TOKEN": "secret"},
        {"API_TOKEN": "secret", "PORT": "9000"},
    )
    assert patch["SLOT_DOMAIN"] == ""
    assert patch["PORT"] == "9000"
    assert "API_TOKEN" not in patch


def test_judge_swap():
    done, ok, _message = judge_swap("ready", True)
    assert done and ok
    done, _ok, _message = judge_swap("ready", False)
    assert not done
    done, ok, message = judge_swap("rolled_back", True)
    assert done and not ok and message
    done, _ok, _message = judge_swap("swapping", False)
    assert not done


def test_hook_url_uses_localhost_for_every_interface():
    assert hook_url("0.0.0.0", "8000") == "http://127.0.0.1:8000"
    assert hook_url("127.0.0.1", "9000") == "http://127.0.0.1:9000"


def test_page_logs_advances_the_cursor():
    raw = "2026-10-05T12:00:00.1Z one\n2026-10-05T12:00:01.2Z two\n2026-10-05T12:00:02.3Z three\n"
    text, nxt, more = page_logs(raw, "2026-10-05T12:00:00.1Z", 1)
    assert text == "2026-10-05T12:00:01.2Z two"
    assert more
    assert nxt == "2026-10-05T12:00:01.2Z"
    text, nxt, more = page_logs(raw, nxt, 10)
    assert text == "2026-10-05T12:00:02.3Z three"
    assert not more
    text, _nxt, more = page_logs(raw, nxt, 10)
    assert text == ""
    assert not more


def test_page_log_rows_honors_the_window():
    raw = "2026-10-05T12:00:00Z one\n2026-10-05T12:00:01Z two\n2026-10-05T12:00:02Z three\n"
    rows, nxt, more = page_log_rows(raw, "", "2026-10-05T12:00:01Z", "2026-10-05T12:00:02Z", 10)
    assert not more
    assert len(rows) == 1
    assert rows[0].line == "two"
    assert rows[0].at == "2026-10-05T12:00:01Z"
    assert nxt == "2026-10-05T12:00:01Z"


def test_update_pulls_the_registry_image(tmp_path: Path):
    log = tmp_path / "podman.log"
    binary = tmp_path / "podman"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        'case "$*" in\n'
        "  inspect*) printf '%s\\n' sha256:previous ;;\n"
        "esac\n"
        "exit 0\n"
    )
    binary.chmod(0o755)
    store = Store.open(tmp_path / "data")
    store.set_meta("self_image", "ghcr.io/bobowski/yard:latest")
    yard = Yard(
        Config(
            root=tmp_path,
            caddy_sock=None,
            podman_sock="",
            podman_bin=str(binary),
            public_port=8000,
            hook_url="http://127.0.0.1:8000",
            domain="",
            skip_deploy=True,
            boot=False,
            remote=False,
        ),
        store,
    )

    async def check() -> None:
        body = await yard.update_yard()
        assert isinstance(body, dict)
        assert body["image"] == "ghcr.io/bobowski/yard:latest"
        assert body["status"] == "swapping"
        assert body["log"] == "pull ghcr.io/bobowski/yard:latest"
        text = log.read_text()
        assert text.splitlines()[0].startswith("inspect ")
        assert text.index("tag sha256:previous localhost/yard:previous") < text.index(
            "pull ghcr.io/bobowski/yard:latest"
        )
        assert "stop -t 10 yard" in text
        assert store.meta("swap_status") == "swapping"
        assert store.meta("swap_id")

    try:
        asyncio.run(check())
    finally:
        store.close()


def test_update_stops_when_the_running_image_cannot_be_saved(tmp_path: Path):
    log = tmp_path / "podman.log"
    binary = tmp_path / "podman"
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\nexit 1\n")
    binary.chmod(0o755)
    store = Store.open(tmp_path / "data")
    yard = Yard(
        Config(
            root=tmp_path,
            caddy_sock=None,
            podman_sock="",
            podman_bin=str(binary),
            public_port=8000,
            hook_url="http://127.0.0.1:8000",
            domain="",
            skip_deploy=True,
            boot=False,
            remote=False,
        ),
        store,
    )
    try:
        with pytest.raises(YardError) as caught:
            asyncio.run(yard.update_yard())
        assert caught.value.code == "swap_failed"
        assert store.meta("swap_status") == "failed"
        assert "pull " not in log.read_text()
    finally:
        store.close()


def test_swap_boot_ignores_the_registry_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = Config(
        root=tmp_path,
        caddy_sock=None,
        podman_sock="",
        podman_bin="podman",
        public_port=8000,
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=True,
        boot=False,
        remote=False,
    )
    store = Store.open(config.data_dir)
    yard = Yard(config, store)
    store.set_meta("swap_status", "swapping")
    store.set_meta("swap_id", "abc")
    store.set_meta("swap_image", "ghcr.io/bobowski/yard:latest")
    monkeypatch.setenv("YARD_IMAGE", "ghcr.io/bobowski/yard:latest")
    monkeypatch.delenv("YARD_SWAP_ACK", raising=False)
    monkeypatch.delenv("YARD_SWAP_ID", raising=False)
    try:
        yard._note_swap_boot()
        assert store.meta("swap_status") == "swapping"
        monkeypatch.setenv("YARD_SWAP_ACK", "1")
        monkeypatch.setenv("YARD_SWAP_ID", "other")
        yard._note_swap_boot()
        assert store.meta("swap_status") == "swapping"
        monkeypatch.setenv("YARD_SWAP_ID", "abc")
        yard._note_swap_boot()
        assert store.meta("swap_status") == "ready"
    finally:
        store.close()


def test_swap_boot_reads_a_matching_restore_result(tmp_path: Path):
    config = Config(
        root=tmp_path,
        caddy_sock=None,
        podman_sock="",
        podman_bin="podman",
        public_port=8000,
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=True,
        boot=False,
        remote=False,
    )
    store = Store.open(config.data_dir)
    yard = Yard(config, store)
    store.set_meta("swap_status", "swapping")
    store.set_meta("swap_id", "abc")
    result = config.data_dir / "swap.result"
    try:
        result.write_text("rolled_back nope\n")
        yard._note_swap_boot()
        assert store.meta("swap_status") == "swapping"
        assert not result.exists()
        result.write_text("rolled_back abc\n")
        yard._note_swap_boot()
        assert store.meta("swap_status") == "rolled_back"
        assert not result.exists()
    finally:
        store.close()


_FAKE_PODMAN = """#!/usr/bin/env python3
import os
import sys

log = os.environ["PODMAN_LOG"]
args = sys.argv[1:]
line = " ".join(args)
with open(log, "a") as handle:
    handle.write(line + "\\n")
mode = os.environ.get("PODMAN_MODE", "unhealthy")
if "exec" in args and "yard-next" in args:
    raise SystemExit(0 if mode in {"published-fails", "ready"} else 1)
if "exec" in args:
    raise SystemExit(0 if mode == "ready" else 1)
if "--name" in args:
    name = args[args.index("--name") + 1]
    if name == "yard-next" and mode == "next-fails":
        raise SystemExit(1)
    if name == "yard" and mode == "published-fails":
        raise SystemExit(1)
if args[:3] == ["--remote", "container", "exists"]:
    body = open(log).read()
    raise SystemExit(0 if "rename yard yard-old" in body else 1)
if "inspect" in args and os.environ.get("PODMAN_STATUS"):
    print(os.environ["PODMAN_STATUS"])
raise SystemExit(0)
"""


def _run_swap(tmp_path: Path, mode: str, status: str = "") -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman_log = tmp_path / "podman.log"
    fake = bin_dir / "podman"
    fake.write_text(_FAKE_PODMAN)
    fake.chmod(0o755)
    root = tmp_path / "root"
    (root / "data" / "yard").mkdir(parents=True)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["PODMAN_LOG"] = str(podman_log)
    env["PODMAN_MODE"] = mode
    if status:
        env["PODMAN_STATUS"] = status
    env["YARD_IMAGE"] = "ghcr.io/bobowski/yard:latest"
    env["YARD_ROOT"] = str(root)
    env["YARD_PODMAN_SOCK"] = "/sock"
    env["YARD_CADDY_SOCK"] = "/caddy.sock"
    env["YARD_PUBLIC_PORT"] = "8000"
    env["YARD_DOMAIN"] = "yard.example"
    env["YARD_SELF_IMAGE"] = "ghcr.io/bobowski/yard:latest"
    env["YARD_SWAP_ID"] = "abc"
    env["YARD_SWAP_WAIT"] = "2"
    env["YARD_SWAP_TRIES"] = "1"
    env["YARD_SWAP_DELAY"] = "0"
    done = subprocess.run(["sh", "-c", _SWAP], env=env, capture_output=True, text=True)
    return done, podman_log, root


def test_swap_script_parses():
    subprocess.run(["sh", "-n", "-c", _SWAP], check=True)
    assert "sleep 2" not in _SWAP


def test_swap_script_keeps_the_stopped_container_when_the_new_image_exits(tmp_path: Path):
    done, log, root = _run_swap(tmp_path, "unhealthy", "exited")
    assert done.returncode == 1, done.stderr
    lines = log.read_text().splitlines()
    assert "--remote stop -t 10 yard" in lines
    assert "--remote start yard" in lines
    assert "--remote rm -f yard" not in lines
    assert "--remote rename yard yard-old" not in lines
    assert (root / "data" / "yard" / "swap.result").read_text() == "rolled_back abc\n"


def test_swap_script_restores_when_the_new_container_does_not_start(tmp_path: Path):
    done, log, _root = _run_swap(tmp_path, "next-fails")
    assert done.returncode == 1, done.stderr
    lines = log.read_text().splitlines()
    assert "--remote rm -f yard" not in lines
    assert "--remote start yard" in lines


def test_swap_script_restores_the_renamed_container_when_publish_fails(tmp_path: Path):
    done, log, root = _run_swap(tmp_path, "published-fails")
    assert done.returncode == 1, done.stderr
    lines = log.read_text().splitlines()
    assert lines.index("--remote rename yard yard-old") < lines.index("--remote rm -f yard")
    assert "--remote start yard" in lines
    assert (root / "data" / "yard" / "swap.result").read_text() == "rolled_back abc\n"


def test_swap_script_removes_the_old_container_only_after_health(tmp_path: Path):
    done, log, root = _run_swap(tmp_path, "ready")
    assert done.returncode == 0, done.stderr + done.stdout
    text = log.read_text()
    assert text.index("--remote rename yard yard-old") < text.index("--name yard --network")
    assert text.index("--name yard --network") < text.index("--remote rm -f yard-old")
    assert "--remote start yard" not in text.splitlines()
    next_line = next(line for line in text.splitlines() if "--name yard-next" in line)
    yard_line = next(line for line in text.splitlines() if "--name yard --network" in line)
    assert "YARD_SWAP_ACK" not in next_line
    assert "--group-add" not in next_line
    assert "-p" not in next_line
    assert "YARD_SWAP_ACK=1" in yard_line
    assert "--group-add" not in yard_line
    assert "127.0.0.1:8000:8000" in yard_line
    assert "YARD_DOMAIN=yard.example" in yard_line
    assert not (root / "data" / "yard" / "swap.result").exists()
    assert "ready" in (root / "data" / "yard" / "swap.log").read_text()


def test_a_missing_image_is_absent(tmp_path: Path):
    absent = tmp_path / "absent"
    absent.write_text("#!/bin/sh\nexit 1\n")
    absent.chmod(0o755)
    dead = tmp_path / "dead"
    dead.write_text("#!/bin/sh\nexit 2\n")
    dead.chmod(0o755)

    async def check() -> None:
        assert await Podman(str(absent)).image_exists("localhost/yard:missing") is False
        with pytest.raises(YardError) as caught:
            await Podman(str(dead)).image_exists("localhost/yard:missing")
        assert caught.value.exit_code == 2

    asyncio.run(check())


def test_parse_stats_and_ps():
    samples = parse_stats('[{"name":"heldso","cpu_percent":"1.5%","mem_usage":"20MB / 1GB"}]')
    assert samples["heldso"].cpu == "1.5%"
    assert samples["heldso"].mem == "20MB / 1GB"
    assert ps_args() == ["ps", "-a", "--format", "json"]
    assert stats_args(["caddy"]) == ["stats", "--no-stream", "--format", "json", "caddy"]
    assert inspect_args("heldso") == ["inspect", "heldso"]
    with pytest.raises(YardError):
        inspect_args("../caddy")
    raw = """[
        {"Names":["heldso"],"State":"running","Status":"Up 2 minutes (healthy)","StartedAt":1710000000,"ExitCode":0},
        {"Names":"shop","State":"exited","Status":"Exited (1) 4 minutes ago","ExitedAt":1710000200,"ExitCode":1}
    ]"""
    got = parse_ps(raw)
    assert got["heldso"].running
    assert got["heldso"].health == "healthy"
    assert got["heldso"].started == datetime.fromtimestamp(1710000000, UTC)
    assert not got["shop"].running
    assert got["shop"].exit_code == 1
    assert got["shop"].health == ""
    assert got["shop"].exited == datetime.fromtimestamp(1710000200, UTC)


def test_latest_run_for_a_job_slot():
    raw = """[
        {"Id":"new","Names":["backup"],"State":"running","Labels":{"yard.slot":"backup"},"Created":"2026-10-06T10:00:00Z"},
        {"Id":"web","Names":["heldso"],"State":"running","Created":"2026-10-06T08:00:00Z"}
    ]"""
    item = latest_for_slot(parse_ps(raw), "backup")
    assert item is not None
    assert item.name == "backup"
    assert item.running
    assert item.slot == "backup"
    assert ids_for_slot(raw, "backup") == ["new"]
    rows = parse_inspect('[{"Id":"new","State":{"Running":false,"FinishedAt":"2026-10-06T10:05:00Z"}}]')
    assert len(rows) == 1
    assert rows[0].finished is not None


def test_slot_rows_keep_stopped_containers():
    from app.podman import Listed

    slots = [Slot("heldso", "", "", "", "", "", "", {}, ""), Slot("backup", "", "", "", "", "", "", {}, "")]
    listed = {
        "heldso": Listed(
            name="heldso",
            state="running",
            status="Up 2 hours (healthy)",
            health="healthy",
            running=True,
            created=_unix(10),
            started=_unix(10),
        ),
        "caddy": Listed(name="caddy", state="running", running=True),
    }
    raw = '[{"name":"heldso","cpu_percent":"1%","mem_usage":"20MB"}]'
    samples = {"heldso": parse_stats(raw)["heldso"]}
    rows = slot_rows(slots, listed, samples, running_only=False)
    assert len(rows) == 3
    assert rows[0].container == "heldso"
    assert rows[0].cpu == "1%"
    assert rows[0].mem == "20MB"
    assert rows[0].health == "healthy"
    assert rows[0].status == "Up 2 hours (healthy)"
    assert rows[0].started
    assert rows[1].slot == "backup"
    assert rows[1].state == "absent"
    assert rows[1].container == ""
    assert rows[1].health == "down"
    assert rows[2].container == "caddy"
    assert rows[2].slot == "caddy"
    assert slot_label("heldso", "heldso") == "heldso"
    assert slot_label("backup", "run-1") == "backup (run-1)"
    idle = slot_rows([Slot("nightly", "", "", "", "", "", "5m", {}, "")], {}, {}, running_only=False)
    assert len(idle) == 1
    assert idle[0].health == "idle"
    running = slot_rows(slots, listed, samples, running_only=True)
    assert [row.container for row in running] == ["heldso", "caddy"]


def test_total_row_sums_use_and_keeps_one_data_size():
    rows = [
        SlotRow(slot="heldso", container="heldso", cpu="1.50%", mem="20MB / 67.32GB", data="1MB"),
        SlotRow(slot="heldso", container="run-1", cpu="0.50%", mem="10MB", data="1MB"),
        SlotRow(slot="caddy", container="caddy", cpu="0.04%", mem="13MB", data="2MB"),
    ]
    total = total_row(rows, 12, 67_320_864_768, 467_909_804_032)
    assert total.slot == "total"
    assert total.cpu == "2.04% / 1200%"
    assert total.mem == "43MB / 67.32GB"
    assert total.data == "3MB / 467.9GB"
    assert build_label("ok", "2026-10-07T11:00:00Z", "2026-10-07T11:00:47Z") == "ok 47s"
    assert build_label("running", "2026-10-07T11:00:00Z", "") == "running"


def test_dir_size_and_host_info(tmp_path: Path):
    data = tmp_path / "slot"
    data.mkdir()
    (data / "app.sqlite3").write_bytes(b"x" * 1000)
    nested = data / "wal"
    nested.mkdir()
    (nested / "extra").write_bytes(b"y" * 24)
    assert dir_size(data) == 1024
    host = parse_info('{"host":{"cpus":12,"memTotal":67320864768},"store":{"graphRootAllocated":467909804032}}')
    assert host.cpus == 12
    assert host.mem == 67320864768
    assert host.disk == 467909804032
    assert parse_info("nope").cpus == 0


def test_job_due_and_plan():
    now = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
    every = 5 * 60
    assert job_due([], every, now)
    assert not job_due([], 0, now)
    assert not job_due([RunState("a", True, finished=now)], every, now)
    assert not job_due([RunState("a", False, finished=now - timedelta(minutes=2))], every, now)
    assert job_due([RunState("a", False, finished=now - timedelta(minutes=5))], every, now)
    image = "localhost/heldso:abc"
    plan = plan_job(every, "", [], now)
    assert not plan.start
    assert plan.warn == "no image yet"
    assert plan.sleep == JOB_RETRY
    assert plan_job(every, image, [], now).start
    plan = plan_job(every, image, [RunState("c1", True, started=now - timedelta(minutes=1))], now)
    assert not plan.start
    assert plan.wait_id == "c1"
    assert plan.warn == ""
    plan = plan_job(every, image, [RunState("c1", True, started=now - timedelta(minutes=6))], now)
    assert plan.wait_id == "c1"
    assert plan.warn == "run is still up past the interval"
    plan = plan_job(every, image, [RunState("c1", True), RunState("c2", True)], now)
    assert plan.warn == "more than one run is still up"
    plan = plan_job(every, image, [RunState("c1", False, finished=now - timedelta(minutes=2))], now)
    assert not plan.start
    assert plan.sleep == 3 * 60
    assert plan_job(every, image, [RunState("c1", False, finished=now - timedelta(minutes=5))], now).start


def test_tag_and_containerfile(tmp_path: Path):
    assert image_tag("mono", "", "abc") == "localhost/mono:abc"
    assert image_tag("mono", "projects/Mono_App", "abc") == "localhost/mono-projects-mono-app:abc"
    paths = Paths(Path("/srv/yard"))
    assert paths.build("mono", "", "abc") != paths.build("mono", "projects/a", "abc")
    (tmp_path / "shared" / "lib").mkdir(parents=True)
    (tmp_path / "shared" / "lib" / "x.py").write_text("")
    (tmp_path / "projects" / "mono").mkdir(parents=True)
    (tmp_path / "projects" / "mono" / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "projects" / "custom").mkdir()
    (tmp_path / "projects" / "custom" / "Containerfile").write_text("FROM scratch\n")
    with pytest.raises(RuntimeError, match="no Containerfile"):
        containerfile(tmp_path, "projects/mono")
    file, context = containerfile(tmp_path, "projects/custom")
    assert file == tmp_path / "projects" / "custom" / "Containerfile"
    assert context == tmp_path / "projects" / "custom"
    ops = tmp_path / "projects" / "opsapp" / "ops"
    ops.mkdir(parents=True)
    (ops / "Containerfile").write_text("FROM scratch\n")
    (tmp_path / "projects" / "opsapp" / "pyproject.toml").write_text("[project]\n")
    file, context = containerfile(tmp_path, "projects/opsapp")
    assert file == ops / "Containerfile"
    assert context == tmp_path / "projects" / "opsapp"
    with pytest.raises(RuntimeError, match="not in the repo"):
        containerfile(tmp_path, "projects/missing")
    with pytest.raises(RuntimeError, match="repo root"):
        containerfile(tmp_path, "")


def test_slot_env_overrides_yard():
    env = {"SLOT_DATA_DIR": "/elsewhere", "SESSION_SECRET": "s", "STARIO_PORT": "9000"}
    text = "\n".join(slot_env("shop", env))
    for want in (
        "SLOT_NAME=shop",
        "SLOT_DATA_DIR=/elsewhere",
        "STARIO_PORT=9000",
        "SESSION_SECRET=s",
        "STARIO_HOST=0.0.0.0",
    ):
        assert want in text
    assert not any(line.startswith("DATA_DIR=") for line in text.splitlines())


def test_caddy_document():
    text = caddy_document(Path("/srv/yard/run/caddy.sock"), "a@example.com", []).decode()
    heldso = Slot("heldso", "", "", "", "heldso.example.com", "", "", {}, "")
    plain = Slot("plain", "", "", "", "", "", "", {}, "")
    body = caddy_document(
        Path("/srv/yard/run/caddy.sock"),
        "a@example.com",
        routes_from([heldso, plain]),
    ).decode()
    assert yard_route("") is None
    route = yard_route("yard.example.com")
    assert route is not None
    assert route.host == "yard.example.com"
    assert route.dial == "yard:8000"
    assert '"dial": "heldso:8000"' in body
    for part in (
        '"listen": "unix//srv/yard/run/caddy.sock|0600"',
        '"https_port": 8443',
        '":8443"',
        '"disable_redirects": true',
        '"disabled": true',
        '"email": "a@example.com"',
    ):
        assert part in text or part in body
    args = " ".join(caddy_run_args(Path("/srv/yard"), Path("/srv/yard/run/caddy.sock")))
    for part in (
        "-p 8443:8443",
        "-v /srv/yard/data/caddy:/config/caddy",
        "-v /srv/yard/data/caddy/lib:/data",
        "-v /srv/yard/run:/srv/yard/run",
        "run --resume",
        "--log-driver journald",
    ):
        assert part in args


def _unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
