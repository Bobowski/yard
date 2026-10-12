from datetime import UTC, datetime, timedelta

from app.render import (
    _state_text,
    _when,
    format_bytes,
    format_deploys,
    format_slot,
    format_slot_status,
    format_status,
    parse_slot,
    slot_edit_patch,
    status_hint,
)
from tests.conftest import NOW, stamp, status_report


def test_format_status():
    text = format_status(status_report(), NOW)
    lines = text.split("\n")
    assert lines[0].startswith("name")
    header = lines[0].split()
    assert header == ["name", "state", "cpu", "mem", "data", "rev", "domain", "deploy"]
    assert "heldso (heldso-app)" in lines[1]
    assert "up 2h" in lines[1]
    assert "abc1234 → f00ba12" in lines[1]
    assert "heldso.example.com" in lines[1]
    assert "ok 2m0s" in lines[1]
    assert "shop (shop-app)" in lines[2]
    assert "failed 3h" in lines[2]
    assert "idle" in lines[3]
    assert "caddy" in lines[4]
    assert "up 24h" in lines[4]
    assert "host:" in lines[-2]
    assert "2% of 4 cores" in lines[-2]
    assert "30MB of 4GB" in lines[-2]
    assert "10MB of 200GB" in lines[-2]


def test_format_status_drops_empty_columns():
    report = status_report()
    for row in report["rows"]:
        row["data"] = 0
        row["sha"] = ""
        row["tip"] = ""
        row["domain"] = ""
        row["deploy"] = None
    text = format_status(report, NOW)
    assert text.split("\n")[0].split() == ["name", "state", "cpu", "mem"]


def test_format_status_deploy_failed_hint():
    report = status_report()
    assert status_hint(report) == "yard deploys shop"
    for row in report["rows"]:
        row["deploy"] = None
    assert status_hint(report) == "yard logs --slot shop"


def test_state_text_marks_no_answer_and_restarts():
    row = {"state": "running", "health": "", "started": stamp(hours=2), "answers": False, "restarts": 3}
    assert _state_text(row, NOW) == "up 2h · no answer · 3 restarts"
    row["restarts"] = 1
    assert _state_text(row, NOW) == "up 2h · no answer · 1 restart"
    row["answers"] = True
    assert _state_text(row, NOW) == "up 2h · 1 restart"
    row["health"] = "starting"
    row["restarts"] = 0
    assert _state_text(row, NOW) == "up 2h"
    row["answers"] = None
    assert _state_text(row, NOW) == "starting 2h"


def test_status_hint_points_a_dead_probe_at_logs():
    report = status_report()
    for row in report["rows"]:
        row["deploy"] = None
        row["state"] = "running"
        row["health"] = ""
        row["answers"] = False
    assert status_hint(report) == "yard logs --slot heldso"


def test_format_slot_status():
    report = status_report()
    row = report["rows"][0]
    page = {
        "slot": {
            "name": "heldso",
            "repo": "heldso",
            "ref": "main",
            "dir": "",
            "domain": "heldso.example.com",
            "trigger": "",
            "image": "localhost/heldso:abc1234",
            "sha": "abc1234",
            "created": stamp(days=2),
            "env": {"A": "1", "B": "2", "C": "3"},
        },
        "env_file": "/srv/yard/env/heldso.env",
        "rows": [row],
        "route": {"host": "heldso.example.com", "dial": "heldso:8000"},
        "deploys": [
            {
                "id": 12,
                "slot": "heldso",
                "sha": "abc1234",
                "status": "ok",
                "started": stamp(days=4, minutes=2),
                "built": "",
                "ended": stamp(days=4),
                "log": "",
            },
            {
                "id": 11,
                "slot": "heldso",
                "sha": "def5678",
                "status": "failed",
                "started": stamp(days=5, minutes=0, seconds=40),
                "built": "",
                "ended": stamp(days=5),
                "log": "",
            },
        ],
        "log": "one\ntwo\n",
    }
    assert format_slot_status(page, NOW) == (
        "heldso  heldso.example.com → heldso:8000\n"
        "rev abc1234 · tip f00ba12 · image localhost/heldso:abc1234\n"
        "data 10MB · env /srv/yard/env/heldso.env (3 keys)\n"
        "\n"
        "containers\n"
        "  heldso-app  up 2h  1.5%  20MB\n"
        "\n"
        "deploys\n"
        "  12  ok      2m0s  4d  abc1234\n"
        "  11  failed   40s  5d  def5678\n"
        "\n"
        "log\n"
        "  one\n"
        "  two\n"
    )


def test_format_slot_status_minimal():
    page = {
        "slot": {"name": "heldso", "sha": "", "image": "", "env": {}},
        "env_file": "/srv/yard/env/heldso.env",
        "rows": [{"slot": "heldso", "state": "absent", "health": "down"}],
        "route": None,
        "deploys": [],
        "log": "",
    }
    assert format_slot_status(page, NOW) == (
        "heldso\n"
        "data 0B · env /srv/yard/env/heldso.env (0 keys)\n"
        "\n"
        "containers\n"
        "  heldso  down\n"
        "\n"
        "deploys\n"
        "  no deploys yet\n"
        "\n"
        "log\n"
        "  no log\n"
    )


def test_format_slot_and_parse_slot_round_trip():
    slot = {
        "name": "heldso",
        "repo": "heldso",
        "ref": "main",
        "dir": "",
        "domain": "heldso.example.com",
        "trigger": "",
        "image": "localhost/heldso:a1b2c3d",
        "env": {"API_TOKEN": "secret", "GREETING": "hi there"},
    }
    text = format_slot(slot)
    assert text.startswith("# slot heldso\n# image localhost/heldso:a1b2c3d\n")
    assert "# Yard sets SLOT_NAME SLOT_DATA_DIR SHA PORT in the container.\n" in text
    assert "# The file is /srv/yard/env/heldso.env on the host.\n" in text
    assert "STARIO" not in text
    assert 'GREETING="hi there"' in text
    assert "REPO=heldso" in text
    assert "REF=main" in text
    assert "DOMAIN=heldso.example.com" in text
    fields, env = parse_slot(text)
    assert fields == {
        "repo": "heldso",
        "ref": "main",
        "dir": "",
        "domain": "heldso.example.com",
        "trigger": "",
    }
    assert env == {"API_TOKEN": "secret", "GREETING": "hi there"}


def test_slot_edit_patch():
    current = {"env": {"API_TOKEN": "secret", "OLD": "x"}, "repo": "heldso"}
    patch = slot_edit_patch(current, ["REF=dev", "API_TOKEN=rotated", "OLD="])
    assert patch == {"ref": "dev", "env": {"API_TOKEN": "rotated"}}
    assert slot_edit_patch(current, ["DOMAIN=heldso.example.com"]) == {"domain": "heldso.example.com"}
    assert slot_edit_patch(current, ["KEEP"]) == {"env": {"API_TOKEN": "secret", "OLD": "x"}}


def test_format_deploys():
    rows = [
        {
            "id": 3,
            "slot": "heldso",
            "sha": "abc1234567",
            "image": "localhost/heldso:abc1234",
            "status": "ok",
            "started": "2026-10-05T10:00:00Z",
            "built": "2026-10-05T10:01:00Z",
            "ended": "2026-10-05T10:02:00Z",
            "log": "",
        }
    ]
    text = format_deploys(rows)
    lines = text.split("\n")
    assert lines[0].split() == ["id", "status", "rev", "build", "start", "took", "when"]
    assert "abc1234" in lines[1]
    assert "1m0s" in lines[1]
    assert "2m0s" in lines[1]


def test_format_bytes():
    assert format_bytes(0) == "0B"
    assert format_bytes(10) == "10B"
    assert format_bytes(1500) == "1.5kB"
    assert format_bytes(4_000_000_000) == "4GB"


def test_when():
    assert _when("") == ""
    now = datetime.now(UTC)
    assert _when((now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")) == "now"
    assert _when((now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")) == "3h"
    assert _when("1999-01-01T00:00:00Z") == ""


def test_format_slot_marks_the_trigger():
    slot = {
        "name": "nightly",
        "repo": "app",
        "ref": "main",
        "dir": "",
        "domain": "",
        "trigger": "every 5m",
        "image": "localhost/app:abc",
        "env": {},
    }
    text = format_slot(slot)
    assert "TRIGGER=every 5m" in text
    fields, _env = parse_slot(text)
    assert fields["trigger"] == "every 5m"
