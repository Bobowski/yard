import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cli import judge_swap, parse_slot_sets, slot_patch, split_args
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
    parse_inspect,
    parse_ps,
    parse_stats,
    ps_args,
    stats_args,
)
from app.rows import slot_rows
from app.store import Slot, Store
from app.text import slot_env
from app.yard import Yard


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
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\nexit 0\n")
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
        assert log.read_text().splitlines()[0] == "pull ghcr.io/bobowski/yard:latest"

    try:
        asyncio.run(check())
    finally:
        store.close()


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
    assert rows[2].slot == ""
    idle = slot_rows([Slot("nightly", "", "", "", "", "", "5m", {}, "")], {}, {}, running_only=False)
    assert len(idle) == 1
    assert idle[0].health == "idle"
    running = slot_rows(slots, listed, samples, running_only=True)
    assert [row.container for row in running] == ["heldso", "caddy"]


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
