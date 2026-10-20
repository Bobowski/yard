"""ops/apply/apply.sh against a fake docker-compose on PATH."""

import os
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "apply" / "apply.sh"


def _bins(tmp_path: Path) -> Path:
    """Fake docker-compose, podman-remote, and curl. They echo calls to the log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker-compose").write_text(
        "#!/bin/sh\n"
        'echo "dc:$*" >> "$CALL_LOG"\n'
        'echo "env IMAGE=$IMAGE SHA=$SHA" >> "$CALL_LOG"\n'
        'if [ "$1" = "config" ]; then\n'
        '  printf "%s\\n" "$DC_CONFIG"\n'
        "  exit 0\n"
        "fi\n"
        'echo "dc:$*"\n'
        # FAIL_IMAGE fails `up` when IMAGE matches, so a revert can fail.
        'if [ "$1" = "up" ] && [ -n "${FAIL_IMAGE:-}" ] && [ "$IMAGE" = "$FAIL_IMAGE" ]; then\n'
        "  exit 1\n"
        "fi\n"
        # FAKE_FAIL names the subcommand that fails, once.
        'if [ "$1" = "${FAKE_FAIL:-}" ] && [ ! -f "$FAIL_SPENT" ]; then\n'
        '  touch "$FAIL_SPENT"\n'
        "  exit 1\n"
        "fi\n"
        'exit "${DC_CODE:-0}"\n'
    )
    (bin_dir / "docker-compose").chmod(0o755)
    (bin_dir / "podman-remote").write_text('#!/bin/sh\necho "podman:$*" >> "$CALL_LOG"\nexit 0\n')
    (bin_dir / "podman-remote").chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *argv: str, **extra: str) -> tuple[int, str, str, str]:
    bin_dir = _bins(tmp_path)
    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "FAIL_SPENT": str(tmp_path / "spent"),
            "COMPOSE_PROJECT_NAME": "heldso",
            "COMPOSE_FILE": "a.yml:b.yml",
            "PROJECT_NAME": "heldso",
            "SHA": "abc",
            "IMAGE": "localhost/heldso:abc",
            "DC_CONFIG": "services:\n  app:\n    networks:\n      yard:\n",
        }
    )
    env.update(extra)
    proc = subprocess.run(
        ["sh", str(SCRIPT), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, log.read_text() if log.exists() else "", proc.stderr


def test_up_builds_starts_and_reports_ready(tmp_path: Path):
    code, out, log, _err = _run(tmp_path, "up")
    assert code == 0
    assert re.search(r"^built \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", out, re.MULTILINE)
    keys = ["== build", "dc:", "built ", "== start", "== started"]
    positions = [out.index(key) for key in keys]
    assert positions == sorted(positions)
    assert "dc:up -d --wait --wait-timeout 120 --remove-orphans --no-build" in log
    # `up --wait` is the whole gate; the script never inspects the container.
    assert "podman:" not in log


def test_up_reuses_the_image_when_skip_build_is_set(tmp_path: Path):
    code, out, log, _err = _run(tmp_path, "up", SKIP_BUILD="1")
    assert code == 0
    assert "== reuse localhost/heldso:abc" in out
    assert "== build" not in out
    assert "built" not in out
    assert "dc:up -d --wait --wait-timeout 120 --remove-orphans --no-build" in log


def test_up_reverts_to_the_previous_image(tmp_path: Path):
    code, out, log, _err = _run(
        tmp_path, "up", FAIL_IMAGE="localhost/heldso:abc", PREVIOUS_IMAGE="localhost/heldso:old"
    )
    assert code == 3
    assert "== unhealthy: up failed" in out
    assert "== revert localhost/heldso:old" in out
    assert "== reverted localhost/heldso:old" in out
    assert "dc:up -d --no-build\nenv IMAGE=localhost/heldso:old" in log


def test_up_fails_when_the_revert_fails(tmp_path: Path):
    code, out, log, _err = _run(
        tmp_path,
        "up",
        FAKE_FAIL="up",
        PREVIOUS_IMAGE="localhost/heldso:old",
        FAIL_IMAGE="localhost/heldso:old",
    )
    assert code == 1
    assert "== revert localhost/heldso:old" in out
    assert "== revert failed" in out
    assert "== reverted" not in out
    assert "dc:up -d --no-build\nenv IMAGE=localhost/heldso:old" in log


def test_up_fails_without_a_previous_image(tmp_path: Path):
    code, out, _, _err = _run(tmp_path, "up", FAIL_IMAGE="localhost/heldso:abc")
    assert code == 1
    assert "== unhealthy: up failed" in out
    assert "== revert" not in out
    assert "== started" not in out


def test_wait_seconds_overrides_the_wait_timeout(tmp_path: Path):
    code, _out, log, _err = _run(tmp_path, "up", WAIT_SECONDS="300")
    assert code == 0
    assert "dc:up -d --wait --wait-timeout 300 --remove-orphans --no-build" in log


def test_build_stops_after_the_stamp(tmp_path: Path):
    code, out, _, _err = _run(tmp_path, "build")
    assert code == 0
    assert "== build localhost/heldso:abc" in out
    assert re.search(r"^built \d{4}-", out, re.MULTILINE)
    assert "== start" not in out


def test_unknown_command_exits(tmp_path: Path):
    code, _out, _log, err = _run(tmp_path, "job")
    assert code == 2
    assert "usage:" in err


def test_down_and_purge(tmp_path: Path):
    code, _, log, _err = _run(tmp_path, "down")
    assert code == 0
    assert "dc:down\n" in log
    code, _, log, _err = _run(tmp_path, "purge")
    assert code == 0
    assert "dc:down -v" in log


def test_start_restarts_the_current_image(tmp_path: Path):
    code, out, log, _err = _run(tmp_path, "start")
    assert code == 0
    assert "== start localhost/heldso:abc" in out
    assert "dc:up -d --wait --wait-timeout 120 --no-build" in log


def test_up_rejects_an_app_off_the_yard_network(tmp_path: Path):
    code, _out, _log, err = _run(tmp_path, "up", DC_CONFIG="services:\n  web:\n    image: x\n")
    assert code == 1
    assert "not on network yard" in err
    assert "heldso-app-1:8000" in err


def test_warnings_come_from_the_compose_file(tmp_path: Path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  app:\n    networks:\n      - yard\n")
    code, out, _, _err = _run(tmp_path, "build", COMPOSE_FILE=str(compose))
    assert code == 0
    assert "${APP_ENV}" in out
    assert "${DATA_DIR}" in out
