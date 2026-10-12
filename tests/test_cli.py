import json

import pytest

from app import cli
from app.cli import main
from tests.conftest import stamp, status_report


def test_main_end_to_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    bodies = {
        "/api/v1/status": (200, json.dumps(status_report())),
        "/api/v1/slots/heldso": (
            200,
            json.dumps(
                {
                    "name": "heldso",
                    "repo": "heldso",
                    "ref": "main",
                    "dir": "",
                    "domain": "heldso.example.com",
                    "trigger": "",
                    "image": "localhost/heldso:a1b2c3d",
                    "sha": "a1b2c3d",
                    "created": stamp(days=2),
                    "env": {"API_TOKEN": "secret"},
                }
            ),
        ),
    }

    def fake_api_try(client, method, path, body):
        return bodies[path.split("?")[0]]

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["status", "--text"])
    out = capsys.readouterr().out
    assert "heldso-app" in out
    assert "host:" in out

    main(["slot", "show", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "# slot heldso" in out
    assert "API_TOKEN=secret" in out

    main(["--version"])
    out = capsys.readouterr().out
    assert out.strip()


def test_status_name_and_doctor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    report = status_report()
    page = {
        "slot": {
            "name": "heldso",
            "repo": "heldso",
            "ref": "main",
            "dir": "",
            "domain": "heldso.example.com",
            "trigger": "",
            "image": "localhost/heldso:a1b2c3d",
            "sha": "a1b2c3d",
            "created": stamp(days=2),
            "env": {"A": "1"},
        },
        "env_file": "/srv/yard/env/heldso.env",
        "rows": [report["rows"][0]],
        "route": {"host": "heldso.example.com", "dial": "heldso:8000"},
        "deploys": [],
        "log": "one\ntwo\n",
    }
    bodies = {
        "/api/v1/slots/heldso/status": (200, json.dumps(page)),
        "/api/v1/doctor": (
            200,
            json.dumps(
                [
                    {"name": "podman", "ok": True, "detail": "podman 5.3.1"},
                    {"name": "caddy routes", "ok": False, "detail": "missing: shop.example.com"},
                ]
            ),
        ),
    }

    def fake_api_try(client, method, path, body):
        return bodies[path.split("?")[0]]

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["status", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "heldso  heldso.example.com → heldso:8000" in out
    assert "containers" in out
    assert "no deploys yet" in out
    assert "  two" in out

    with pytest.raises(SystemExit) as exited:
        main(["doctor", "--text"])
    assert exited.value.code == 1
    out = capsys.readouterr().out
    assert "ok   podman" in out
    assert "FAIL caddy routes  missing: shop.example.com" in out


def test_run_and_slot_new_trigger(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body):
        seen[(method, path)] = body
        if path.endswith("/run"):
            return 200, json.dumps({"output": "ran\n"})
        return 200, json.dumps({"slot": {"name": "nightly", "repo": "app", "ref": "main", "trigger": "every 1h"}})

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["run", "nightly", "--text"])
    assert "ran" in capsys.readouterr().out
    assert seen[("POST", "/api/v1/slots/nightly/run")] is None

    main(["slot", "new", "nightly", "--repo", "app", "--trigger", "every 1h", "--text"])
    body = seen[("POST", "/api/v1/slots")]
    assert body["trigger"] == "every 1h"


def test_prune(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen = {}

    def fake_api_try(client, method, path, body):
        seen[(method, path)] = body
        return 200, json.dumps(
            {
                "checkouts": [{"path": "/srv/yard/checkouts/app/0ld", "bytes": 1000}],
                "images": [
                    {"tag": "localhost/app:st4le", "bytes": 2000, "kept": ""},
                    {"tag": "localhost/busy:t4g", "bytes": 3000, "kept": "image is in use by heldso-app-1"},
                ],
                "freed": 3000,
                "days": body["days"],
                "dry": body["dry"],
            }
        )

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["prune", "--text"])
    assert seen[("POST", "/api/v1/prune")] == {"days": 7, "dry": False}
    out = capsys.readouterr().out
    assert "removed 1 checkouts and 1 images, 3kB" in out
    assert "anything newer than 7 days" in out
    assert "kept localhost/busy:t4g (image is in use by heldso-app-1)" in out

    main(["prune", "--days", "1", "--dry-run", "--text"])
    assert seen[("POST", "/api/v1/prune")] == {"days": 1, "dry": True}
    out = capsys.readouterr().out
    assert "would remove" in out


def test_init_needs_domain_or_trigger(capsys: pytest.CaptureFixture):
    with pytest.raises(SystemExit):
        main(["init", "--text"])
    assert "needs --domain or --trigger" in capsys.readouterr().err
