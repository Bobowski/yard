import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import package_version
from app.apply import DEFAULT_COMPOSE, apply_compose, run_args
from app.cli import (
    Client,
    as_json,
    clone_git_args,
    format_deploys,
    format_exps,
    format_inspect,
    format_record,
    format_repos,
    format_slot_table,
    format_tokens,
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
    Result,
    RunState,
    _apply_env,
    _revert_after_start,
    caddy_document,
    image_tag,
    job_due,
    plan_job,
    prepare_checkout,
    routes_from,
    yard_route,
)
from app.podman import (
    Podman,
    ids_for_slot,
    inspect_args,
    log_hit,
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
from app.store import Slot, Store, check_origin, origin_name
from app.text import parse_env
from app.yard import Yard, job_states, run_cmd, track_origin


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
    assert "yard clone <repo>" in out
    assert "yard git <repo> fetch" in out
    assert "Clone one branch from the host" in out
    assert "Save the host and the token" in out
    assert "yard <command> --help" in out


def test_repo_help_leaves_git_at_the_top(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["repo"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "fetch <name>" not in out
    assert "git <name> <command>" not in out
    assert "pull" not in out
    assert "clone <repo>" not in out
    with pytest.raises(SystemExit) as caught:
        main(["git"])
    assert caught.value.code == 0
    git_help = capsys.readouterr().out
    assert "yard git <name> <command>" in git_help
    assert "fetch" in git_help


def test_clone_is_shallow_until_a_flag_asks_for_more():
    assert clone_git_args(full=False, lazy=False) == ["clone", "--depth", "1", "--single-branch"]
    assert "--filter=blob:none" not in clone_git_args(full=False, lazy=False)
    assert clone_git_args(full=False, lazy=True) == [
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--filter=blob:none",
    ]
    assert clone_git_args(full=True, lazy=False) == ["clone"]
    assert clone_git_args(full=True, lazy=True) == ["clone", "--filter=blob:none"]


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
    assert "Show and edit apps" in capsys.readouterr().err


def test_a_missing_verb_lists_the_verbs(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["slot"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "logs" not in out
    assert "deploys <name>" not in out
    assert "show <name>" in out


def test_half_a_command_prints_its_flags(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["deploys"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "yard deploys <name>" in err
    with pytest.raises(SystemExit) as caught:
        main(["podman", "logs"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "yard podman logs <container>" in err
    assert "--follow" in err


def test_help_flag_on_a_command(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        main(["help", "logs"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "yard logs" in out
    assert "--slot <name>" in out
    assert "--role <name>" in out
    assert "--tail <n>" in out


def test_records_stay_short():
    secret = format_record('{"name":"ci","token":"yard-secret","parent":"admin"}')
    assert secret == "ci\nyard-secret\n"
    assert "parent" not in secret
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
    built = (datetime.now(UTC) - timedelta(hours=3) + timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    split = format_deploys(
        [{"id": 4, "status": "ok", "sha": "dfa7dd027e8bd735", "started": started, "built": built, "ended": ended}]
    )
    header = split.splitlines()[0].split()
    assert header == ["id", "status", "rev", "build", "start", "took", "when"]
    assert "15s" in split
    assert "4s" in split
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
        == "yard logs --slot shop"
    )
    assert (
        slot_hint(
            [
                {
                    "slot": "shop",
                    "container": "shop",
                    "state": "running",
                    "status": "Up 2 hours",
                    "health": "healthy",
                    "build": "failed 19s",
                }
            ]
        )
        == "yard deploys shop"
    )
    assert slot_hint([{"slot": "honorata", "container": "honorata", "state": "running", "status": "Up 1 minute"}]) == ""


def test_login_keeps_url_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "config.json"
    monkeypatch.setattr("app.cli.config_path", lambda: path)
    main(["login", "--url", "yard.bobowski.dev", "--token", "secret"])
    assert path.read_text() == '{\n  "url": "https://yard.bobowski.dev",\n  "token": "secret"\n}\n'
    assert capsys.readouterr().out == f"logged in\nhost    https://yard.bobowski.dev\nsaved   {path}\n"
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
    assert "url     https://yard.example.com/git/heldso.git" in text
    assert "ref     main" in text
    assert "The slot follows main." in text
    assert "Commit first." in text
    assert "git push -u yard main" in text
    assert calls[0][0:2] == ("POST", "/api/v1/repos")
    assert calls[1] == (
        "POST",
        "/api/v1/slots",
        {"SLOT_NAME": "heldso", "SLOT_REPO": "heldso", "REF": "main", "DOMAIN": "heldso.example.com"},
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
        {"SLOT_NAME": "heldso", "SLOT_REPO": "heldso", "REF": "main", "SLOT_INTERVAL": "5m"},
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
        "REF": "main",
        "DOMAIN": "heldso.example.com",
    }
    remote = subprocess.run(["git", "remote", "get-url", "yard"], cwd=root, capture_output=True, text=True, check=True)
    assert remote.stdout.strip() == "https://yard.example.com/git/heldso-docs.git"


def test_init_from_a_github_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.chdir(root)
    calls, fake = _init_api(repo_body='{"name":"heldso","created":"t","ref":"main"}')
    monkeypatch.setattr("app.cli._load", lambda _opt: Client("https://yard.example.com", "secret"))
    monkeypatch.setattr("app.cli._api_try", fake)
    main(
        [
            "--text",
            "init",
            "--remote",
            "https://user:secret@github.com/Org/Heldso.git",
            "--domain",
            "heldso.example.com",
        ]
    )
    text = capsys.readouterr().out
    assert "repo    heldso" in text
    assert "url     https://yard.example.com/git/heldso.git" in text
    assert "remote  origin https://github.com/Org/Heldso.git" in text
    assert "secret" not in text
    assert "ref     origin/main" in text
    assert "The slot follows origin/main." in text
    assert "git push" not in text
    assert calls[0][2] == {
        "name": "heldso",
        "force": False,
        "origin": "https://user:secret@github.com/Org/Heldso.git",
    }
    assert calls[1][2]["REF"] == "origin/main"
    assert not (root / ".git").exists()


def test_init_from_uses_the_requested_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.chdir(root)
    calls, fake = _init_api(repo_body='{"name":"heldso","created":"t","ref":"main"}')
    monkeypatch.setattr("app.cli._load", lambda _opt: Client("https://yard.example.com", "secret"))
    monkeypatch.setattr("app.cli._api_try", fake)
    main(["init", "--remote", "https://github.com/org/heldso", "--every", "5m", "--ref", "feature"])
    body = json.loads(capsys.readouterr().out)
    assert body["ref"] == "origin/feature"
    assert calls[1][2]["REF"] == "origin/feature"
    assert not (root / ".git").exists()


def test_origin_name_comes_from_the_address():
    assert origin_name("https://github.com/Org/Heldso.git") == "heldso"
    with pytest.raises(YardError) as caught:
        check_origin("git@github.com:org/heldso.git")
    assert caught.value.code == "bad_origin"


def test_track_origin_fetches_the_remote_branch(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    (source / "README").write_text("hi\n")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    bare = tmp_path / "heldso.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True, capture_output=True)

    async def check() -> tuple[str, str]:
        ref = await track_origin(bare, str(source))
        sha = subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "refs/remotes/origin/main"],
            check=True,
            capture_output=True,
            text=True,
        )
        return ref, sha.stdout.strip()

    ref, sha = asyncio.run(check())
    assert ref == "main"
    assert len(sha) == 40


def test_create_repo_rejects_a_bad_origin(tmp_path: Path):
    store = Store.open(tmp_path / "data")
    yard = Yard(
        Config(
            root=tmp_path,
            caddy_sock=None,
            podman_sock="",
            podman_bin="podman",
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
            asyncio.run(yard.create_repo("heldso", False, "git@github.com:org/heldso.git"))
        assert caught.value.code == "bad_origin"
        assert not (tmp_path / "repos" / "heldso.git").exists()
    finally:
        store.close()


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
    sets = parse_slot_sets(["--set", "DOMAIN", "heldso.com", "--set", "API_TOKEN=secret", "--set", "OLD="])
    assert sets == [("DOMAIN", "heldso.com"), ("API_TOKEN", "secret"), ("OLD", "")]
    with pytest.raises(ValueError):
        parse_slot_sets(["--domain", "heldso.com"])


def test_slot_patch_removes_empty():
    patch = slot_patch(
        {"DOMAIN": "heldso.com", "API_TOKEN": "secret"},
        {"API_TOKEN": "secret", "PORT": "9000"},
    )
    assert patch["DOMAIN"] == ""
    assert patch["PORT"] == "9000"
    assert "API_TOKEN" not in patch


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


def test_a_fetch_ref_deploys_the_origin_slot(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    (source / "README").write_text("hi\n")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "repos" / "heldso.git"
    repo.parent.mkdir()
    subprocess.run(["git", "init", "--bare", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(source)], check=True, capture_output=True)
    store = Store.open(tmp_path / "data")
    store.create_repo("heldso")
    store.create_slot("heldso", "heldso", "origin/main", "", "", "")
    store.create_slot("local", "heldso", "main", "", "", "")
    yard = Yard(
        Config(
            root=tmp_path,
            caddy_sock=None,
            podman_sock="",
            podman_bin="podman",
            hook_url="http://127.0.0.1:8000",
            domain="",
            skip_deploy=True,
            boot=False,
            remote=False,
        ),
        store,
    )
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "fetch",
                "--depth",
                "1",
                "origin",
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            check=True,
            capture_output=True,
        )
        sha = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main"],
            text=True,
        ).strip()
        status, text = asyncio.run(yard.hook_push("heldso", sha, "refs/remotes/origin/main"))
        assert status == 200
        assert "slot heldso: deploy skipped" in text
        assert "local" not in text
        status, skipped = asyncio.run(yard.hook_push("heldso", sha, "refs/tags/v1"))
        assert status == 200
        assert skipped.startswith("skip ")
        deploy_id = store.begin_deploy("heldso", sha)
        store.finish_deploy(deploy_id, "ok")
        status, current = asyncio.run(yard.hook_push("heldso", sha, "refs/remotes/origin/main"))
        assert status == 200
        assert "slot heldso: current" in current
    finally:
        store.close()


def _yard(tmp_path: Path, store: Store, domain: str) -> Yard:
    return Yard(
        Config(
            root=tmp_path,
            caddy_sock=None,
            podman_sock="",
            podman_bin="podman",
            hook_url="http://127.0.0.1:8000",
            domain=domain,
            skip_deploy=True,
            boot=False,
            remote=False,
        ),
        store,
    )


def test_boot_records_the_yard_slot(tmp_path: Path):
    store = Store.open(tmp_path / "data")
    yard = _yard(tmp_path, store, "yard.example.com")
    try:
        yard._ensure_host_slot()
        with pytest.raises(YardError):
            store.slot("yard")
        (tmp_path / "repos" / "yard.git").mkdir(parents=True)
        yard._ensure_host_slot()
        slot = store.slot("yard")
        assert slot.repo == "yard"
        assert slot.ref == "origin/main"
        assert slot.domain == "yard.example.com"
        assert store.last_deploy("yard") is None
        yard._ensure_host_slot()
        assert store.slot("yard").domain == "yard.example.com"
    finally:
        store.close()


def test_a_yard_upgrade_keeps_the_host_env(tmp_path: Path):
    paths = Paths(tmp_path)
    env_path = paths.run_dir("yard") / "compose.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "YARD_PODMAN_SOCK=/run/user/1/podman/podman.sock\n"
        "YARD_PUBLIC_PORT=8000\n"
        "YARD_CADDY_SOCK=/srv/yard/run/caddy.sock\n"
        "YARD_BOOT=1\n"
        "YARD_DOMAIN=yard.example.com\n"
        "CONTAINER_HOST=unix:///run/user/1/podman/podman.sock\n"
        "PROXY_PORT=8443\n"
        "IMAGE=localhost/yard:old\n"
    )
    checkout = tmp_path / "checkout"
    (checkout / "ops").mkdir(parents=True)
    (checkout / "ops" / "compose.yml").write_text("services: {}\n")
    slot = Slot("yard", "yard", "origin/main", "", "yard.example.com", "localhost/yard:old", "", {}, "")
    compose, written = _apply_env(paths, slot, checkout, "localhost/yard:new", "newsha")
    env = parse_env(written.read_text())
    assert compose == checkout / "ops" / "compose.yml"
    assert env["IMAGE"] == "localhost/yard:new"
    assert env["PREVIOUS"] == "localhost/yard:old"
    assert env["CHECKOUT"] == str(checkout)
    assert env["YARD_PODMAN_SOCK"] == "/run/user/1/podman/podman.sock"
    assert env["YARD_DOMAIN"] == "yard.example.com"
    assert env["CONTAINER_HOST"] == "unix:///run/user/1/podman/podman.sock"
    assert env["YARD_CADDY_SOCK"] == "/srv/yard/run/caddy.sock"
    assert env["YARD_PUBLIC_PORT"] == "8000"
    assert env["YARD_BOOT"] == "1"
    assert env["PROXY_PORT"] == "8443"
    app_env = parse_env((written.parent / "app.env").read_text())
    assert "CONTAINER_HOST" not in app_env
    assert "YARD_DOMAIN" not in app_env


def test_the_new_yard_process_claims_its_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHA", "abc")
    monkeypatch.setenv("IMAGE", "localhost/yard:abc")
    store = Store.open(tmp_path / "data")
    yard = _yard(tmp_path, store, "yard.example.com")
    try:
        (tmp_path / "repos" / "yard.git").mkdir(parents=True)
        yard._ensure_host_slot()
        deploy_id = store.begin_deploy("yard", "abc")
        yard._claim_yard_deploy()
        store.interrupt_deploys()
        last = store.last_deploy("yard")
        assert last is not None
        assert last.id == deploy_id
        assert last.status == "ok"
        assert last.sha == "abc"
        assert store.slot("yard").live_image == "localhost/yard:abc"
    finally:
        store.close()


def test_the_first_boot_records_the_installed_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHA", "abc")
    monkeypatch.setenv("IMAGE", "localhost/yard:abc")
    store = Store.open(tmp_path / "data")
    yard = _yard(tmp_path, store, "yard.example.com")
    try:
        (tmp_path / "repos" / "yard.git").mkdir(parents=True)
        yard._ensure_host_slot()
        assert store.last_deploy("yard") is None
        yard._claim_yard_deploy()
        last = store.last_deploy("yard")
        assert last is not None
        assert last.status == "ok"
        assert last.sha == "abc"
        assert store.slot("yard").live_image == "localhost/yard:abc"
    finally:
        store.close()


def test_a_yard_upgrade_stays_running_until_the_new_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store.open(tmp_path / "data")
    yard = _yard(tmp_path, store, "yard.example.com")

    async def fake_deploy(*_args, **_kwargs):
        return Result("localhost/yard:abc", "started\n", pending=True)

    monkeypatch.setattr("app.yard.deploy_slot", fake_deploy)
    try:
        (tmp_path / "repos" / "yard.git").mkdir(parents=True)
        yard._ensure_host_slot()
        out = asyncio.run(yard.deploy_once("yard", "abc"))
        assert out.error == ""
        assert "upgrading localhost/yard:abc" in out.text
        last = store.last_deploy("yard")
        assert last is not None
        assert last.status == "running"
        assert last.sha == "abc"
        assert store.slot("yard").live_image == ""
    finally:
        store.close()


def test_repo_git_reads_and_a_deleted_branch_drops_the_slot(tmp_path: Path):
    store = Store.open(tmp_path / "data")
    yard = _yard(tmp_path, store, "")
    try:
        asyncio.run(yard.create_repo("heldso", False, ""))
        bare = tmp_path / "repos" / "heldso.git"
        allow = subprocess.check_output(
            ["git", "-C", str(bare), "config", "--get", "uploadpack.allowFilter"],
            text=True,
        )
        assert allow.strip() == "true"
        tree = subprocess.check_output(["git", "-C", str(bare), "hash-object", "-t", "tree", "--stdin"], input=b"")
        commit = subprocess.check_output(
            [
                "git",
                "-C",
                str(bare),
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                "commit-tree",
                tree.decode().strip(),
                "-m",
                "init",
            ],
            text=True,
        )
        sha = commit.strip()
        subprocess.check_call(["git", "-C", str(bare), "update-ref", "refs/heads/main", sha])
        store.create_slot("heldso", "heldso", "main", "", "", "")
        body = asyncio.run(yard.git_repo("heldso", ["log", "-1", "--oneline"]))
        assert sha[:7] in str(body["output"])
        with pytest.raises(YardError) as caught:
            asyncio.run(yard.git_repo("heldso", ["fetch"]))
        assert caught.value.code == "git_failed"
        gone = asyncio.run(yard.git_repo("heldso", ["branch", "-D", "main"]))
        assert "slot heldso removed" in str(gone["output"])
        with pytest.raises(YardError):
            store.slot("heldso")
    finally:
        store.close()


_FAKE_COMPOSE = """#!/usr/bin/env python3
import os
import sys

log = os.environ["COMPOSE_LOG"]
args = sys.argv[1:]
line = " ".join(args)
with open(log, "a") as handle:
    handle.write(line + "\\n")
if "config" in args and "--services" in args:
    text = os.environ.get("COMPOSE_SERVICES", "")
    if text and not text.endswith("\\n"):
        text += "\\n"
    sys.stdout.write(text)
    raise SystemExit(0)
if "config" in args:
    sys.stdout.write(os.environ.get("COMPOSE_CONFIG", ""))
    raise SystemExit(0)
mode = os.environ.get("COMPOSE_MODE", "ok")
count_path = os.environ.get("COMPOSE_COUNT", "")
if "build" in args and "up" not in args and mode == "build-fails":
    raise SystemExit(1)
if "up" in args and mode == "start-fails":
    count = 0
    if count_path and os.path.exists(count_path):
        count = int(open(count_path).read() or "0")
    if count_path:
        open(count_path, "w").write(str(count + 1))
    if count == 0:
        raise SystemExit(1)
raise SystemExit(0)
"""


def _run_up(
    tmp_path: Path,
    mode: str,
    command: str = "up",
    extra: str = "",
    config: str = "",
    shared: bool = False,
    text: str = "",
    shell: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    compose_log = tmp_path / "compose.log"
    fake = bin_dir / "docker-compose"
    fake.write_text(_FAKE_COMPOSE)
    fake.chmod(0o755)
    root = tmp_path / "root"
    data = root / "data" / "yard"
    data.mkdir(parents=True)
    envfile = data / "compose.env"
    envfile.write_text(text or ("IMAGE=localhost/yard:new\nPREVIOUS=localhost/yard:old\n" + extra))
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    script = Path(__file__).resolve().parents[1] / "ops" / "apply" / "apply.sh"
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["COMPOSE_LOG"] = str(compose_log)
    env["COMPOSE_COUNT"] = str(tmp_path / "count")
    env["COMPOSE_MODE"] = mode
    env["DOCKER_HOST"] = "unix:///sock"
    env["COMPOSE_PROJECT_NAME"] = "yard"
    env["COMPOSE_FILE"] = str(compose)
    env["COMPOSE_ENV_FILE"] = str(envfile)
    env["COMPOSE_SERVICES"] = "app\ncaddy"
    env["COMPOSE_CONFIG"] = config
    env["YARD_SLOT"] = "heldso"
    env["YARD_SHA"] = "abc123"
    if shared:
        env["YARD_DEFAULT_COMPOSE"] = "1"
    if shell:
        env.update(shell)
    for line in (text or extra).splitlines():
        key, sep, value = line.partition("=")
        if sep and key == "DATA_DIR":
            env["DATA_DIR"] = value
    env["SERVICE"] = "app"
    done = subprocess.run([str(script), command], env=env, capture_output=True, text=True)
    return done, compose_log, envfile


def test_up_leaves_the_env_file_when_the_build_fails(tmp_path: Path):
    done, log, envfile = _run_up(tmp_path, "build-fails")
    assert done.returncode == 1, done.stderr
    assert "build" in log.read_text()
    assert " up " not in f" {log.read_text()} "
    assert "IMAGE=localhost/yard:new" in envfile.read_text()


def test_up_stops_when_the_start_fails(tmp_path: Path):
    done, log, envfile = _run_up(tmp_path, "start-fails")
    assert done.returncode == 1, done.stderr
    text = log.read_text()
    assert text.index(" build") < text.index(" up ")
    assert text.count(" up ") == 1
    assert done.stdout.startswith("built ")
    assert "revert " not in done.stdout
    assert "IMAGE=localhost/yard:new" in envfile.read_text()


def test_revert_starts_the_previous_image(tmp_path: Path):
    done, log, envfile = _run_up(
        tmp_path,
        "ok",
        "revert",
        text="IMAGE=localhost/yard:old\nSHA=old\nPREVIOUS=localhost/yard:old\n",
        shell={"IMAGE": "localhost/yard:old"},
    )
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.stdout == "revert localhost/yard:old\n"
    assert log.read_text().count(" up ") == 1
    assert "IMAGE=localhost/yard:old" in envfile.read_text()
    assert "SHA=old" in envfile.read_text()
    labels = (envfile.parent / "labels.yml").read_text()
    assert 'yard.role: "app"' in labels
    assert 'yard.sha: "abc123"' in labels


def test_up_starts_the_new_image_after_the_build(tmp_path: Path):
    done, log, envfile = _run_up(tmp_path, "ok")
    assert done.returncode == 0, done.stderr + done.stdout
    text = log.read_text()
    assert text.index(" build") < text.index(" up ")
    assert done.stdout.startswith("built ")
    assert "IMAGE=localhost/yard:new" in envfile.read_text()
    labels = (envfile.parent / "labels.yml").read_text()
    assert 'yard.slot: "heldso"' in labels
    assert labels.count('yard.sha: "abc123"') == 1
    assert 'yard.service: "caddy"' in labels


def test_up_binds_the_slot_directory_when_the_service_does_not(tmp_path: Path):
    config = """services:
  app:
    image: app
  caddy:
    volumes:
      - type: bind
        source: /srv/yard/data/caddy/lib
        target: /data
"""
    done, log, envfile = _run_up(
        tmp_path,
        "ok",
        extra="DATA_DIR=/srv/yard/data/heldso\n",
        config=config,
    )
    assert done.returncode == 0, done.stderr + done.stdout
    text = (envfile.parent / "data.yml").read_text()
    assert '"/srv/yard/data/heldso:/data"' in text
    assert "caddy:" not in text
    assert "data.yml" in log.read_text()


def test_a_job_binds_the_slot_directory(tmp_path: Path):
    done, log, envfile = _run_up(
        tmp_path,
        "ok",
        "job",
        extra="DATA_DIR=/srv/yard/data/heldso\n",
    )
    assert done.returncode == 0, done.stderr + done.stdout
    text = (envfile.parent / "data.yml").read_text()
    assert '"/srv/yard/data/heldso:/data"' in text
    assert "data.yml" in log.read_text()
    assert "yard.role=job" in log.read_text()


def test_a_failed_build_keeps_the_live_image(tmp_path: Path):
    paths = Paths(tmp_path)
    env_path = paths.run_dir("heldso") / "compose.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("IMAGE=localhost/heldso:new\nSHA=new\nDATA_DIR=/srv/yard/data/heldso\nTOKEN=s\n")
    slot = Slot("heldso", "heldso", "main", "", "", "localhost/heldso:old", "", {}, "")
    lines = asyncio.run(
        _revert_after_start(Podman(), paths, slot, "localhost/heldso:new", Path("compose.yml"), env_path, "")
    )
    env = parse_env(env_path.read_text())
    assert lines == ["kept localhost/heldso:old"]
    assert env["IMAGE"] == "localhost/heldso:old"
    assert env["SHA"] == "old"
    assert env["DATA_DIR"] == "/srv/yard/data/heldso"
    assert env["TOKEN"] == "s"


def test_revert_writes_the_live_image_before_compose(tmp_path: Path):
    paths = Paths(tmp_path)
    env_path = paths.run_dir("heldso") / "compose.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("IMAGE=localhost/heldso:new\nSHA=new\n")
    (env_path.parent / "apply.built").write_text("2026-10-09T00:00:00Z\n")
    slot = Slot("heldso", "heldso", "main", "", "", "localhost/heldso:old", "", {}, "")
    seen: dict[str, str] = {}

    class _Capture(Podman):
        async def run(self, *args, env=None, program="", timeout=None):
            seen["image"] = (env or {}).get("YARD_IMAGE", "")
            seen["sha"] = (env or {}).get("YARD_SHA", "")
            return "revert localhost/heldso:old\n"

    lines = asyncio.run(
        _revert_after_start(_Capture(), paths, slot, "localhost/heldso:new", Path("compose.yml"), env_path, "")
    )
    assert seen == {"image": "localhost/heldso:old", "sha": "old"}
    assert lines[0] == "rollback localhost/heldso:old"


def test_run_args_reads_the_image_and_the_data_directory(tmp_path: Path):
    env_path = tmp_path / "compose.env"
    env_path.write_text("IMAGE=localhost/heldso:abc\nDATA_DIR=/srv/yard/data/heldso\n")
    _args, env = run_args(
        root="/srv/yard",
        sock="/sock",
        project="heldso",
        compose="/c.yml",
        env_file=str(env_path),
        command="revert",
        slot="heldso",
        sha="old",
    )
    assert env["YARD_IMAGE"] == "localhost/heldso:abc"
    assert env["YARD_DATA_DIR"] == "/srv/yard/data/heldso"
    assert env["YARD_SHA"] == "old"


def test_apply_env_mounts_the_host_data_directory(tmp_path: Path):
    checkout = tmp_path / "src"
    checkout.mkdir()
    (checkout / "Containerfile").write_text("FROM scratch\n")
    slot = Slot("heldso", "heldso", "main", "", "heldso.example.com", "", "", {"TOKEN": "s"}, "")
    compose, env_path = _apply_env(Paths(tmp_path), slot, checkout, "localhost/heldso:abc", "abc")
    data = tmp_path / "data" / "heldso"
    assert data.is_dir()
    assert env_path == tmp_path / "run" / "heldso" / "compose.env"
    env = parse_env(env_path.read_text())
    assert env["DATA_DIR"] == str(data)
    assert env["SLOT_DATA_DIR"] == "/data"
    assert compose == Path(DEFAULT_COMPOSE)
    assert not (checkout / ".yard-compose.yml").exists()
    template = Path(__file__).resolve().parents[1] / "ops" / "apply" / "default.compose.yml"
    assert "${DATA_DIR}:/data" in template.read_text()
    assert DEFAULT_COMPOSE.endswith(template.name)
    app_env = parse_env((env_path.parent / "app.env").read_text())
    assert app_env["SLOT_DATA_DIR"] == "/data"
    assert "DATA_DIR" not in app_env
    assert app_env["TOKEN"] == "s"
    assert not (data / "compose.env").exists()


def test_default_compose_skips_the_overlay(tmp_path: Path):
    done, log, envfile = _run_up(tmp_path, "ok", shared=True)
    assert done.returncode == 0, done.stderr + done.stdout
    text = log.read_text()
    assert "config" not in text
    assert " build" in text
    assert not (envfile.parent / "labels.yml").exists()
    assert not (envfile.parent / "data.yml").exists()


def test_a_job_run_keeps_the_project_name(tmp_path: Path):
    done, log, _envfile = _run_up(tmp_path, "ok", "job")
    assert done.returncode == 0, done.stderr + done.stdout
    text = log.read_text()
    assert " run " in f" {text} "
    assert "--rm=false" in text
    assert "-p yard" in text
    assert "--name" not in text
    assert "yard.role=job" in text
    assert "yard.slot=heldso" in text
    assert "yard.sha=abc123" in text
    assert "yard.service=app" in text


def test_a_deploy_container_has_labels_and_no_name():
    spec = apply_compose().read_text()
    assert "image: localhost/yard-apply:5.3.1" in spec
    assert "dockerfile: Containerfile" in spec
    assert "context: ." in spec
    assert "${YARD_PODMAN_SOCK}:/run/podman/podman.sock" in spec
    assert "${YARD_ROOT}:${YARD_ROOT}" in spec
    assert "yard.role:" in spec
    assert "DATA_DIR: ${YARD_DATA_DIR:-}" in spec
    assert "IMAGE: ${YARD_IMAGE:-}" in spec
    assert "container_name" not in spec
    args, env = run_args(
        root="/srv/yard",
        sock="",
        project="heldso",
        compose="/c.yml",
        env_file="/e",
        command="up",
        slot="heldso",
        sha="abc",
    )
    text = " ".join(args)
    assert text.endswith("apply up")
    assert "run -d" not in text
    assert "--rm=false" in text
    detached, _detached_env = run_args(
        root="/srv/yard",
        sock="",
        project="yard",
        compose="/c.yml",
        env_file="/e",
        command="up",
        slot="yard",
        sha="abc",
        detach=True,
    )
    assert "run -d -T" in " ".join(detached)
    assert "--name" not in text
    assert "yard.role" not in text
    assert env["YARD_ROLE"] == "deploy"
    assert env["YARD_SLOT"] == "heldso"
    assert env["YARD_SHA"] == "abc"
    assert env["YARD_PROJECT"] == "heldso"
    assert env["YARD_COMPOSE"] == "/c.yml"
    assert env["YARD_DEFAULT_COMPOSE"] == ""
    _shared_args, shared = run_args(
        root="/srv/yard",
        sock="",
        project="heldso",
        compose=DEFAULT_COMPOSE,
        env_file="/e",
        command="up",
        slot="heldso",
        sha="abc",
    )
    assert shared["YARD_DEFAULT_COMPOSE"] == "1"
    _revert_args, revert = run_args(
        root="/srv/yard",
        sock="",
        project="heldso",
        compose="/c.yml",
        env_file="/e",
        command="revert",
        slot="heldso",
        sha="old",
    )
    assert "--rm=false" in " ".join(_revert_args)
    assert revert["YARD_ROLE"] == "revert"
    assert revert["YARD_SHA"] == "old"
    job_args, job = run_args(
        root="/srv/yard",
        sock="",
        project="heldso",
        compose="/c.yml",
        env_file="/e",
        command="job",
        slot="heldso",
        sha="abc",
    )
    assert "--rm" in job_args
    assert "--rm=false" not in job_args
    assert job["YARD_ROLE"] == ""
    assert job["YARD_PROJECT"] == "heldso"
    assert job["YARD_SHA"] == "abc"


def test_parse_stats_and_ps():
    samples = parse_stats('[{"name":"heldso","cpu_percent":"1.5%","mem_usage":"20MB / 1GB"}]')
    assert samples["heldso"].cpu == "1.5%"
    assert samples["heldso"].mem == "20MB / 1GB"
    assert ps_args() == ["ps", "-a", "--format", "json"]
    assert stats_args() == ["stats", "--no-stream", "--format", "json"]
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
    marked = """[
        {"Id":"run","Names":["backup-run"],"Labels":{"yard.slot":"backup","yard.role":"job"}},
        {"Id":"build","Names":["build"],"Labels":{"yard.slot":"backup","yard.role":"deploy"}}
    ]"""
    assert ids_for_slot(marked, "backup", "job") == ["run"]
    parsed = parse_ps(marked)
    assert parsed["backup-run"].role == "job"
    assert log_hit(parsed["backup-run"], slot="backup", role="job", sha="")
    assert not log_hit(parsed["build"], slot="backup", role="app", sha="")
    assert log_hit(parsed["build"], slot="backup", role="deploy", sha="")
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
        "heldso-build": Listed(name="heldso-build", slot="heldso", role="deploy", state="exited"),
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


def test_a_job_wake_keeps_the_running_job_and_the_newest_finish():
    running = """[
        {"Id":"run","Names":["backup-run"],"State":"running","StartedAt":1710000000,
         "Labels":{"yard.slot":"backup","yard.role":"job"}}
    ]"""
    finished = """[
        {"Id":"old","Names":["backup-old"],"State":"exited","ExitedAt":1710000100,
         "Labels":{"yard.slot":"backup","yard.role":"job"}},
        {"Id":"new","Names":["backup-new"],"State":"exited","ExitedAt":1710000200,
         "Labels":{"yard.slot":"backup","yard.role":"job"}}
    ]"""
    states = job_states(running, finished)
    assert [state.id for state in states] == ["run", "new"]
    assert states[0].running
    assert not states[1].running
    assert states[1].finished == datetime.fromtimestamp(1710000200, UTC)


def test_a_slot_list_reads_each_repo_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "README").write_text("one\n")
    _git(source, "init", "-b", "main")
    _git(source, "add", ".")
    _git(source, "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", "init")
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    repo = tmp_path / "repos" / "app.git"
    repo.parent.mkdir()
    subprocess.run(["git", "clone", "--bare", str(source), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha], check=True)
    store = Store.open(tmp_path / "db")
    yard = _yard(tmp_path, store, "")
    calls: list[tuple[str, ...]] = []

    async def spy(*args: str, timeout: float | None = None, env: dict[str, str] | None = None) -> str:
        calls.append(args)
        return await run_cmd(*args, timeout=timeout, env=env)

    monkeypatch.setattr("app.yard.run_cmd", spy)
    web = Slot("web", "app", "main", "", "", "localhost/app:old", "", {}, "")
    mirror = Slot("mirror", "app", "origin/main", "", "", "", "", {}, "")
    try:
        facts = asyncio.run(yard.slot_facts([web, mirror]))
    finally:
        store.close()
    assert [args for args in calls if "for-each-ref" in args]
    assert sum("for-each-ref" in args for args in calls) == 1
    assert facts["web"][1] == sha
    assert facts["mirror"][1] == sha


def test_a_slot_list_sizes_each_data_directory_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store.open(tmp_path / "db")
    yard = _yard(tmp_path, store, "")
    store.create_repo("app")
    store.create_slot("heldso", "app", "main", "", "", "")
    store.create_slot("other", "app", "main", "", "", "")
    (tmp_path / "data" / "heldso").mkdir(parents=True)
    (tmp_path / "data" / "other").mkdir(parents=True)
    calls: list[Path] = []

    def counting(path: Path) -> int:
        calls.append(path)
        return 10

    monkeypatch.setattr("app.yard.dir_size", counting)

    class _Podman:
        async def run(
            self,
            *args: str,
            timeout: float | None = None,
            env: dict[str, str] | None = None,
            program: str = "",
        ) -> str:
            if args and args[0] == "ps":
                return """[
                    {"Id":"a","Names":["heldso-app"],"State":"running","Labels":{"yard.slot":"heldso","yard.role":"app"}},
                    {"Id":"b","Names":["heldso-extra"],"State":"running","Labels":{"yard.slot":"heldso","yard.role":"app"}},
                    {"Id":"c","Names":["other-app"],"State":"running","Labels":{"yard.slot":"other","yard.role":"app"}}
                ]"""
            if args and args[0] == "info":
                return '{"host":{"cpus":2,"memTotal":1000},"store":{"graphRootAllocated":1000}}'
            return "[]"

    yard.podman = _Podman()
    try:
        rows = asyncio.run(yard.list_rows(False))
    finally:
        store.close()
    assert calls == [tmp_path / "data" / "heldso", tmp_path / "data" / "other"]
    sized = [row for row in rows if row["slot"] in {"heldso", "other"}]
    assert len(sized) == 3
    assert all(row["data"] == "10B" for row in sized)


def test_checkout_extracts_one_tree(tmp_path: Path):
    source = tmp_path / "src"
    app = source / "projects" / "app"
    app.mkdir(parents=True)
    (app / "Containerfile").write_text("FROM scratch\n")
    (source / "README").write_text("one\n")
    _git(source, "init", "-b", "main")
    _git(source, "add", ".")
    _git(source, "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", "init")
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (source / "README").write_text("two\n")
    _git(source, "add", "README")
    _git(source, "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", "next")
    repo = tmp_path / "repos" / "app.git"
    repo.parent.mkdir()
    subprocess.run(["git", "clone", "--bare", str(source), str(repo)], check=True, capture_output=True)
    paths = Paths(tmp_path)
    out = asyncio.run(prepare_checkout(paths, "app", "projects/app", sha))
    assert (out / "Containerfile").read_text() == "FROM scratch\n"
    assert not (out / "README").exists()
    assert not (out / ".git").exists()
    (out / "keep.txt").write_text("stay\n")
    again = asyncio.run(prepare_checkout(paths, "app", "projects/app", sha))
    assert again == out
    assert (again / "keep.txt").read_text() == "stay\n"
    assert (out.parent / f".{out.name}.ready").is_file()
    missing = paths.build("app", "projects/missing", sha)
    with pytest.raises(RuntimeError, match="not in the repo"):
        asyncio.run(prepare_checkout(paths, "app", "projects/missing", sha))
    assert not (missing.parent / f".{missing.name}.ready").exists()


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)


def test_image_tag_includes_the_directory():
    assert image_tag("mono", "", "abc") == "localhost/mono:abc"
    assert image_tag("mono", "projects/Mono_App", "abc") == "localhost/mono-projects-mono-app:abc"
    paths = Paths(Path("/srv/yard"))
    assert paths.build("mono", "", "abc") != paths.build("mono", "projects/a", "abc")


def test_caddy_document():
    text = caddy_document(Path("/srv/yard/run/caddy.sock"), []).decode()
    heldso = Slot("heldso", "", "", "", "heldso.example.com", "", "", {}, "")
    plain = Slot("plain", "", "", "", "", "", "", {}, "")
    body = caddy_document(Path("/srv/yard/run/caddy.sock"), routes_from([heldso, plain])).decode()
    assert yard_route("") is None
    route = yard_route("yard.example.com")
    assert route is not None
    assert route.host == "yard.example.com"
    assert route.dial == "yard:8000"
    assert '"dial": "heldso:8000"' in body
    assert '"lb_try_duration": "10s"' in body
    assert '"lb_try_interval": "250ms"' in body
    for part in (
        '"listen": "unix//srv/yard/run/caddy.sock|0600"',
        '"https_port": 8443',
        '":8443"',
        '"disable_redirects": true',
        '"disabled": true',
    ):
        assert part in text or part in body
    spec = (Path(__file__).resolve().parents[1] / "ops" / "compose.yml").read_text()
    for part in (
        "container_name: caddy",
        "${YARD_ROOT}/data/caddy:/config/caddy",
        "${YARD_ROOT}/data/caddy/lib:/data",
        "${YARD_ROOT}/run:${YARD_ROOT}/run",
        'command: ["run", "--resume"]',
        "yard.role: app",
        "yard.slot: yard",
        "yard.service: caddy",
        "${PROXY_PORT:-8443}:${PROXY_PORT:-8443}",
    ):
        assert part in spec


def _unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
