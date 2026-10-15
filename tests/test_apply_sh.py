"""ops/apply/apply.sh against a fake docker-compose and wget on PATH."""

import os
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "apply" / "apply.sh"


def _bins(tmp_path: Path) -> Path:
    """Fake docker-compose and wget. Each echoes its call to stdout and the log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker-compose").write_text(
        "#!/bin/sh\n"
        'echo "dc:$*" | tee -a "$CALL_LOG"\n'
        'echo "env IMAGE=$IMAGE SHA=$SHA" >> "$CALL_LOG"\n'
        # FAKE_FAIL names the subcommand that fails, once.
        'if [ "$1" = "${FAKE_FAIL:-}" ] && [ ! -f "$FAIL_SPENT" ]; then\n'
        '  touch "$FAIL_SPENT"\n'
        "  exit 1\n"
        "fi\n"
        'exit "${DC_CODE:-0}"\n'
    )
    (bin_dir / "wget").write_text('#!/bin/sh\necho "wget:$*" | tee -a "$CALL_LOG"\nexit "${WGET_CODE:-0}"\n')
    for name in ("docker-compose", "wget"):
        (bin_dir / name).chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *argv: str, **extra: str) -> tuple[int, str, str]:
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
            "WAIT_SECONDS": "1",
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
    return proc.returncode, proc.stdout, log.read_text() if log.exists() else ""


def test_up_builds_starts_and_reports_ready(tmp_path: Path):
    code, out, _ = _run(tmp_path, "up")
    assert code == 0
    assert re.search(r"^built \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", out, re.MULTILINE)
    keys = ["== build", "dc:", "built ", "== start", "== wait", "wget:", "== ready"]
    positions = [out.index(key) for key in keys]
    assert positions == sorted(positions)


def test_up_failure_reverts_to_the_previous_image(tmp_path: Path):
    code, out, log = _run(tmp_path, "up", FAKE_FAIL="up", PREVIOUS="localhost/heldso:old9")
    assert code == 1
    assert "== revert localhost/heldso:old9" in out
    assert "== kept localhost/heldso:old9" in out
    # The revert runs `up` with the previous image and its sha in the env.
    assert "env IMAGE=localhost/heldso:old9 SHA=old9" in log


def test_up_failure_with_no_previous_just_fails(tmp_path: Path):
    code, out, _ = _run(tmp_path, "up", FAKE_FAIL="up")
    assert code == 1
    assert "== revert" not in out


def test_build_stops_after_the_stamp(tmp_path: Path):
    code, out, _ = _run(tmp_path, "build")
    assert code == 0
    assert "== build localhost/heldso:abc" in out
    assert re.search(r"^built \d{4}-", out, re.MULTILINE)
    assert "== start" not in out


def test_up_probes_the_slot_name_url(tmp_path: Path):
    code, out, _ = _run(tmp_path, "up")
    assert code == 0
    assert "== wait http://heldso:8000/" in out


def test_up_health_timeout_reverts(tmp_path: Path):
    code, out, _ = _run(tmp_path, "up", WGET_CODE="4", PREVIOUS="localhost/heldso:old9")
    assert code == 1
    assert "== ready" not in out
    assert "== revert" in out


def test_an_answered_error_status_counts_as_up(tmp_path: Path):
    # wget exit 8: the server returned an error status, so it is running.
    code, out, _ = _run(tmp_path, "up", WGET_CODE="8")
    assert code == 0
    assert "== ready" in out


def test_job_runs_the_app_and_keeps_its_exit_code(tmp_path: Path):
    code, out, _ = _run(tmp_path, "job")
    assert code == 0
    assert "== run localhost/heldso:abc" in out
    assert "run --rm -T --no-deps app" in out
    code, _, _ = _run(tmp_path, "job", DC_CODE="5")
    assert code == 5


def test_down_and_purge(tmp_path: Path):
    code, _, log = _run(tmp_path, "down")
    assert code == 0
    assert "dc:down\n" in log
    code, _, log = _run(tmp_path, "purge")
    assert code == 0
    assert "dc:down -v" in log


def test_start_restarts_the_current_image(tmp_path: Path):
    code, out, _ = _run(tmp_path, "start")
    assert code == 0
    assert "== start localhost/heldso:abc" in out
