import json
from pathlib import Path

import pytest

from app import cli
from app.cli import main
from tests.conftest import stamp, status_report


def test_yard_prints_help(capsys: pytest.CaptureFixture):
    main([])
    out = capsys.readouterr().out
    assert "usage: yard" in out
    assert "status" in out


def test_resource_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    bodies = {
        "/api/v1/projects": (
            200,
            json.dumps([{"name": "heldso", "repo": "app", "ref": "main", "trigger": ""}]),
        ),
        "/api/v1/repos": (200, json.dumps([{"name": "app", "created": stamp(days=1)}])),
        "/api/v1/tokens": (200, json.dumps([{"name": "ada", "created": stamp(days=1)}])),
        "/api/v1/binds": (
            200,
            json.dumps([{"host": "heldso.example.com", "dial": "heldso:8000", "state": "up"}]),
        ),
    }

    def fake_api_try(client, method, path, body):
        return bodies[path]

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["project", "ls", "--text"])
    assert "heldso" in capsys.readouterr().out
    main(["repo", "ls", "--text"])
    assert "app" in capsys.readouterr().out
    main(["token", "ls", "--text"])
    assert "ada" in capsys.readouterr().out
    main(["bind", "ls", "--text"])
    assert "heldso.example.com" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        main(["project", "list", "--text"])


def test_main_end_to_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    bodies = {
        "/api/v1/status": (200, json.dumps(status_report())),
        "/api/v1/projects/heldso": (
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

    main(["project", "show", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "# project heldso" in out
    assert "API_TOKEN=secret" in out

    main(["--version"])
    out = capsys.readouterr().out
    assert out.strip()


def test_status_name_and_doctor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    report = status_report()
    page = {
        "project": {
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
        "/api/v1/status": (200, json.dumps(report)),
        "/api/v1/projects/heldso/status": (200, json.dumps(page)),
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
        main(["status", "--full", "--text"])
    assert exited.value.code == 1
    out = capsys.readouterr().out
    assert "ok   podman" in out
    assert "FAIL caddy routes  missing: shop.example.com" in out


def test_up_and_slot_new_trigger(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body):
        seen[(method, path)] = body
        if method == "GET":
            trigger = "every 1h" if path.endswith("/nightly") else ""
            return 200, json.dumps({"name": path.rsplit("/", 1)[-1], "repo": "app", "ref": "main", "trigger": trigger})
        if path.endswith("/run"):
            return 200, json.dumps({"output": "ran\n"})
        if path.endswith("/deploy"):
            return 200, json.dumps({"deploy": {"status": "ok", "image": "localhost/app:abc", "log": "ready\n"}})
        return 200, json.dumps({"project": {"name": "nightly", "repo": "app", "ref": "main", "trigger": "every 1h"}})

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["up", "nightly", "--text"])
    assert "ran" in capsys.readouterr().out
    assert seen[("POST", "/api/v1/projects/nightly/run")] is None

    main(["up", "heldso", "--text"])
    assert seen[("POST", "/api/v1/projects/heldso/deploy")] == {}
    assert "ready" in capsys.readouterr().out

    main(["project", "new", "nightly", "--repo", "app", "--trigger", "every 1h", "--text"])
    body = seen[("POST", "/api/v1/projects")]
    assert body["trigger"] == "every 1h"


def test_prune(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen = {}

    def fake_api_try(client, method, path, body):
        seen[(method, path)] = body
        return 200, json.dumps(
            {
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
    assert "removed 1 images, 3kB" in out
    assert "anything newer than 7 days" in out
    assert "kept localhost/busy:t4g (image is in use by heldso-app-1)" in out

    main(["prune", "--days", "1", "--dry-run", "--text"])
    assert seen[("POST", "/api/v1/prune")] == {"days": 1, "dry": True}
    out = capsys.readouterr().out
    assert "would remove" in out


def test_bind_and_tool_commands(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body):
        seen[(method, path)] = body
        if path == "/api/v1/binds" and method == "GET":
            row = {"host": "heldso.example.com", "dial": "heldso-app:8000", "state": "up"}
            return 200, json.dumps([row])
        if path == "/api/v1/binds" and method == "PUT":
            saved = {"host": body["host"], "dial": "heldso-app:9000", "state": "up"}
            return 200, json.dumps(saved)
        if path.startswith("/api/v1/binds/") and method == "DELETE":
            return 200, json.dumps({"host": "heldso.example.com", "dial": "heldso-app:8000"})
        if path == "/api/v1/caddy":
            return 200, json.dumps({"output": "v2.10.0\n"})
        return 200, json.dumps({"output": "ok\n"})

    def fake_stream(client, path):
        seen[("STREAM", path)] = None

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setattr(cli, "_stream", fake_stream)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["bind", "new", "--text", "--public", "heldso.example.com", "heldso-app:9000", "--try", "30s"])
    assert seen[("PUT", "/api/v1/binds")] == {
        "host": "heldso.example.com",
        "dial": "heldso-app:9000",
        "try": "30s",
    }
    assert "heldso.example.com  heldso-app:9000  up" in capsys.readouterr().out

    main(["bind", "ls", "--text"])
    assert "heldso.example.com  heldso-app:8000  up" in capsys.readouterr().out

    main(["bind", "rm", "heldso-app", "--text"])
    assert seen[("DELETE", "/api/v1/binds/heldso-app")] is None
    assert "removed heldso-app:8000" in capsys.readouterr().out

    before = set(seen)
    with pytest.raises(SystemExit):
        main(["bind", "new", "--text", "--internal", "heldso.example.com", "heldso-app"])
    assert "not available yet" in capsys.readouterr().err
    assert set(seen) == before

    main(["caddy", "--text", "version"])
    assert seen[("POST", "/api/v1/caddy")] == {"args": ["version"]}
    assert capsys.readouterr().out == "v2.10.0\n"

    main(["podman", "--text", "ps"])
    assert seen[("POST", "/api/v1/podman")] == {"args": ["ps"]}

    main(["podman", "--text", "logs", "-f", "--tail", "20", "heldso-app-1"])
    streamed = next(path for kind, path in seen if kind == "STREAM")
    assert streamed.startswith("/api/v1/containers/heldso-app-1/logs?")
    assert "follow=1" in streamed
    assert "tail=20" in streamed


def test_init_can_bind_a_public_name(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: list[tuple] = []

    def fake_api_try(client, method, path, body):
        seen.append((method, path, body))
        if path == "/api/v1/repos":
            return 200, json.dumps({"name": "heldso", "ref": "main"})
        if path == "/api/v1/projects":
            return 200, json.dumps(
                {"project": {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "dir": "", "trigger": ""}}
            )
        if path == "/api/v1/binds":
            return 200, json.dumps({"host": body["host"], "dial": "web:8000", "state": "absent"})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(
        [
            "init",
            "--text",
            "--remote",
            "https://github.com/org/heldso",
            "--project",
            "web",
            "--public",
            "heldso.example.com",
            "--try",
            "30s",
        ]
    )
    assert seen[0][0:2] == ("POST", "/api/v1/repos")
    assert seen[0][2]["name"] == "heldso"
    assert seen[1][0:2] == ("POST", "/api/v1/projects")
    assert seen[1][2]["name"] == "web"
    assert seen[2] == (
        "PUT",
        "/api/v1/binds",
        {"host": "heldso.example.com", "dial": "web", "try": "30s"},
    )
    out = capsys.readouterr().out
    assert "heldso.example.com" in out
    assert "web:8000" in out

    seen.clear()
    with pytest.raises(SystemExit):
        main(["init", "--text", "--internal", "heldso.internal", "--remote", "https://github.com/org/heldso"])
    assert seen == []
    assert "not available yet" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["init", "--text", "--try", "30s"])
    assert "--public" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["init", "--name", "heldso"])
    assert "--name" in capsys.readouterr().err


def test_clone_can_follow_a_branch(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: list[tuple] = []
    git: list[tuple] = []

    def fake_api_try(client, method, path, body):
        seen.append((method, path, body))
        if path == "/api/v1/projects":
            project = {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "dir": body.get("dir", "")}
            return 200, json.dumps({"project": project})
        if path == "/api/v1/binds":
            return 200, json.dumps({"host": body["host"], "dial": "heldso-login:8000", "state": "absent"})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setattr(cli, "_git", lambda prefix, args: git.append((prefix, args)))
    monkeypatch.setattr(cli, "_link_dir", lambda directory, remote, token: None)
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")

    main(["clone", "heldso", "--text"])
    assert seen == []
    assert capsys.readouterr().out.strip() == "heldso"
    assert any("clone" in args for _prefix, args in git)

    git.clear()
    main(
        [
            "clone",
            "heldso",
            "--text",
            "--ref",
            "login",
            "--dir",
            "ops",
            "--public",
            "login.heldso.example.com",
            "--try",
            "30s",
        ]
    )
    assert ("checkout", "-b", "login") in [tuple(args[:3]) for _prefix, args in git if len(args) >= 3]
    assert seen[0][0:2] == ("POST", "/api/v1/projects")
    assert seen[0][2] == {"name": "heldso-login", "repo": "heldso", "ref": "login", "dir": "ops"}
    assert seen[1] == (
        "PUT",
        "/api/v1/binds",
        {"host": "login.heldso.example.com", "dial": "heldso-login", "try": "30s"},
    )
    out = capsys.readouterr().out
    assert "git push -u yard login" in out
    assert "yard main" not in out
    assert "heldso-login" in out

    git.clear()
    seen.clear()
    with pytest.raises(SystemExit):
        main(["clone", "heldso", "--text", "--ref", "main"])
    assert "feature branch" in capsys.readouterr().err
    assert git == []
    assert seen == []

    with pytest.raises(SystemExit):
        main(["clone", "heldso", "--text", "--project", "web"])
    assert "--ref" in capsys.readouterr().err


def test_init_does_not_need_a_domain(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("app.cli.config_path", lambda: tmp_path / "missing.json")
    with pytest.raises(SystemExit):
        main(["init", "--text", "--trigger", "soon"])
    assert "trigger" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["init", "--text"])
    err = capsys.readouterr().err
    assert "login" in err
    assert "domain" not in err
