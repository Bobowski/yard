import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import app.cli.main as cli
from app.cli.main import main
from tests.conftest import stamp, status_report


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")


def test_yard_prints_help(capsys: pytest.CaptureFixture):
    main([])
    out = capsys.readouterr().out
    assert "usage: yard" in out
    assert "status" in out


def test_resource_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    bodies = {
        "/api/v1/apps": (
            200,
            json.dumps(
                [{"name": "heldso", "repo": "https://origin.cursor.com/org/app", "ref": "main", "trigger": "push"}]
            ),
        ),
        "/api/v1/tokens": (200, json.dumps([{"name": "ada", "created": stamp(days=1)}])),
        "/api/v1/binds": (
            200,
            json.dumps([{"host": "heldso.example.com", "dial": "heldso:8000", "state": "up"}]),
        ),
    }

    def fake_api_try(client, method, path, body, timeout=None):
        return bodies[path]

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(["app", "ls", "--text"])
    assert "heldso" in capsys.readouterr().out
    main(["token", "ls", "--text"])
    assert "ada" in capsys.readouterr().out
    main(["bind", "ls", "--text"])
    assert "heldso.example.com" in capsys.readouterr().out


def test_main_end_to_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    bodies = {
        "/api/v1/status": (200, json.dumps(status_report())),
        "/api/v1/apps/heldso": (
            200,
            json.dumps(
                {
                    "app": {
                        "name": "heldso",
                        "repo": "https://origin.cursor.com/org/heldso",
                        "ref": "main",
                        "trigger": "push",
                        "image": "localhost/heldso:a1b2c3d",
                        "sha": "a1b2c3d",
                        "created": stamp(days=2),
                    },
                    "env_file": "",
                    "rows": [],
                    "binds": [],
                    "deploys": [],
                    "log": "",
                }
            ),
        ),
    }

    def fake_api_try(client, method, path, body, timeout=None):
        return bodies[path.split("?")[0]]

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(["status", "--text"])
    out = capsys.readouterr().out
    assert "heldso-app" in out
    assert "host:" in out

    main(["app", "status", "--app", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "heldso" in out

    main(["--version"])
    out = capsys.readouterr().out
    assert out.strip()


def test_app_status_and_doctor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    report = status_report()
    page = {
        "app": {
            "name": "heldso",
            "repo": "https://origin.cursor.com/org/heldso",
            "ref": "main",
            "trigger": "push",
            "image": "localhost/heldso:a1b2c3d",
            "sha": "a1b2c3d",
            "created": stamp(days=2),
        },
        "env_file": "/srv/yard/apps/heldso/env",
        "rows": [report["rows"][0]],
        "binds": [{"host": "heldso.example.com", "dial": "heldso:8000"}],
        "deploys": [],
        "log": "one\ntwo\n",
    }
    bodies = {
        "/api/v1/status": (200, json.dumps(report)),
        "/api/v1/apps/heldso": (200, json.dumps(page)),
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

    def fake_api_try(client, method, path, body, timeout=None):
        return bodies[path.split("?")[0]]

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(["app", "status", "--app", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "heldso.example.com  →  heldso:8000" in out
    assert "containers" in out
    assert "no deploys yet" in out
    assert "  two" in out

    with pytest.raises(SystemExit) as exited:
        main(["status", "--full", "--text"])
    assert exited.value.code == 1
    out = capsys.readouterr().out
    assert "ok   podman" in out
    assert "FAIL caddy routes  missing: shop.example.com" in out


def test_app_deploy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if path.endswith("/deploy"):
            return 200, json.dumps(
                {"app": {"name": "nightly"}, "deploy": {"status": "ok", "image": "localhost/app:abc", "log": "ready\n"}}
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(["app", "deploy", "--app", "nightly", "--text"])
    assert seen[("POST", "/api/v1/apps/nightly/deploy")] == {}
    assert "ready" in capsys.readouterr().out


def test_app_deploy_a_sha_warns_on_a_push_trigger(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        return 200, json.dumps(
            {
                "app": {"name": "nightly", "trigger": "push"},
                "deploy": {"status": "ok", "image": "localhost/app:abc", "log": "ok\n"},
            }
        )

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    main(["app", "deploy", "--app", "nightly", "--sha", "abc1234", "--text"])
    assert seen[("POST", "/api/v1/apps/nightly/deploy")] == {"sha": "abc1234"}
    out = capsys.readouterr().out
    assert "The trigger is push: the next check deploys the tip again." in out
    assert "yard app set --trigger manual" in out


class _TTY:
    """sys.stdin for the confirmation tests."""

    def isatty(self) -> bool:
        return True


def _rm_api(page: dict, deleted: dict):
    def fake_api_try(client, method, path, body, timeout=None):
        base = path.split("?")[0]
        if method == "GET" and base == "/api/v1/apps/heldso":
            return 200, json.dumps(page)
        if method == "DELETE" and base == "/api/v1/apps/heldso":
            return 200, json.dumps(deleted)
        raise AssertionError((method, path))

    return fake_api_try


_RM_PAGE = {
    "app": {"name": "heldso"},
    "env_file": "/srv/yard/apps/heldso/env",
    "rows": [{"container": "heldso-app-1", "data": 1_200_000_000}],
    "binds": [{"host": "heldso.example.com", "dial": "heldso-app-1:8000"}],
    "deploys": [],
    "log": "",
}


def test_app_rm_refuses_a_non_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "rm", "--app", "heldso", "--text"])
    err = capsys.readouterr().err
    assert "needs a confirmation" in err
    assert "--yes" in err


def test_app_rm_confirms_and_lists_the_binds_left(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    deleted = {
        "name": "heldso",
        "binds_removed": [],
        "binds_left": [{"host": "heldso.example.com", "dial": "heldso-app-1:8000"}],
    }
    monkeypatch.setattr(cli, "_api_try", _rm_api(_RM_PAGE, deleted))
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "heldso")
    _env(monkeypatch)
    main(["app", "rm", "--app", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "This deletes the app heldso:" in out
    assert "heldso-app-1" in out
    assert "/srv/yard/apps/heldso/data (1.2GB)" in out
    assert "These binds stay and will point at nothing:" in out
    assert "heldso.example.com → heldso-app-1:8000" in out
    assert "removed heldso" in out
    assert "yard bind rm heldso.example.com" in out
    assert "yard app rm --with-binds" in out


def test_app_rm_a_wrong_answer_removes_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path))
        return 200, json.dumps(_RM_PAGE)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "other")
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "rm", "--app", "heldso", "--text"])
    assert "Not removed: you typed 'other', the app is 'heldso'" in capsys.readouterr().err
    assert [call for call in calls if call[0] == "DELETE"] == []


def test_app_rm_yes_with_binds(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    deleted = {
        "name": "heldso",
        "binds_removed": [{"host": "heldso.example.com", "dial": "heldso-app-1:8000"}],
        "binds_left": [],
    }
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path))
        return 200, json.dumps(deleted)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    main(["app", "rm", "--app", "heldso", "--yes", "--with-binds", "--text"])
    assert calls == [("DELETE", "/api/v1/apps/heldso?binds=1")]
    out = capsys.readouterr().out
    assert "removed heldso" in out
    assert "removed bind heldso.example.com → heldso-app-1:8000" in out


def test_app_rm_warns_when_the_binds_are_unreadable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    deleted = {"name": "heldso", "binds_removed": [], "binds_left": [], "binds_error": "caddy GET: no socket"}

    def fake_api_try(client, method, path, body, timeout=None):
        return 200, json.dumps(deleted)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    main(["app", "rm", "--app", "heldso", "--yes", "--text"])
    out = capsys.readouterr().out
    assert "Could not read the binds: caddy GET: no socket" in out
    assert "yard bind ls" in out


def test_not_logged_in_points_at_login(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    monkeypatch.delenv("YARD_URL", raising=False)
    monkeypatch.delenv("YARD_TOKEN", raising=False)
    monkeypatch.setattr(cli, "config_path", lambda: Path("/nonexistent/yard/config.json"))
    with pytest.raises(SystemExit):
        main(["app", "ls", "--text"])
    err = capsys.readouterr().err
    assert "not logged in" in err
    assert "yard login --url https://<your yard host> --token <token>" in err
    assert "/srv/yard/tokens/admin.token" in err


def test_api_unreachable_names_the_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "ls", "--text"])
    err = capsys.readouterr().err
    assert "the Yard API at http://127.0.0.1:8000 did not answer: " in err
    assert "yard login" in err
    assert "sudo -u yard podman ps" in err


def test_a_deploy_timeout_points_at_the_deploy_log(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    def boom(request, timeout=None):
        raise TimeoutError

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "deploy", "--app", "heldso", "--text"])
    err = capsys.readouterr().err
    assert "did not answer" in err
    assert "The deploy may still be running" in err
    assert "yard app logs --deploy --app heldso" in err


def test_env_set_needs_key_value(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["env", "set", "notakey", "--app", "heldso", "--text"])
    err = capsys.readouterr().err
    assert "need KEY=VALUE, got notakey" in err
    assert "Example: yard env set DATABASE_PATH=/data/app.sqlite3" in err


def test_env_edit_an_editor_failure_saves_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    class _Proc:
        returncode = 3

    monkeypatch.setattr(cli, "_api_try", lambda *a, **k: (200, "{}"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(sys, "stdin", _TTY())
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["env", "edit", "--app", "heldso", "--text"])
    err = capsys.readouterr().err
    assert "the editor exited with 3; nothing was saved" in err


def test_app_deploy_failed_reads_the_log_hint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    wrap = {
        "app": {"name": "heldso", "trigger": "manual"},
        "deploy": {"status": "failed", "image": "localhost/heldso:abc", "log": "== boom\n", "error": "apply exited 1"},
    }
    monkeypatch.setattr(cli, "_api_try", lambda *a, **k: (200, json.dumps(wrap)))
    _env(monkeypatch)
    main(["app", "deploy", "--app", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "failed" in out
    assert "Read the full log: yard app logs --deploy --app heldso" in out


def test_app_deploy_reverted_explains_the_old_image(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    wrap = {
        "app": {"name": "heldso", "trigger": "manual"},
        "deploy": {
            "status": "reverted",
            "image": "localhost/heldso:abc",
            "log": "",
            "previous": "localhost/heldso:old",
        },
    }
    monkeypatch.setattr(cli, "_api_try", lambda *a, **k: (200, json.dumps(wrap)))
    _env(monkeypatch)
    main(["app", "deploy", "--app", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "The new container never got healthy, so the previous image still runs." in out
    assert "yard app logs --deploy --app heldso, then yard app logs --app heldso" in out


def test_app_deploy_a_server_500_keeps_the_log_hint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    body = {
        "error": {
            "code": "deploy_failed",
            "message": "apply exited 1",
            "help": "Read the full log: yard app logs --deploy --app heldso",
        }
    }
    monkeypatch.setattr(cli, "_api_try", lambda *a, **k: (500, json.dumps(body)))
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "deploy", "--app", "heldso", "--text"])
    assert "yard app logs --deploy --app heldso" in capsys.readouterr().err


def test_app_new(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: list[tuple] = []

    def fake_api_try(client, method, path, body, timeout=None):
        seen.append((method, path, body))
        if path == "/api/v1/apps":
            return 200, json.dumps(
                {"app": {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "trigger": "push"}}
            )
        if path.startswith("/api/v1/binds/"):
            return 200, json.dumps({"host": "heldso.example.com", "dial": "web-app-1:8000", "state": "absent"})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(
        [
            "app",
            "new",
            "--text",
            "--repo",
            "https://origin.cursor.com/org/heldso",
            "--ref",
            "main",
            "--app",
            "web",
            "--public",
            "heldso.example.com",
            "--try",
            "30s",
        ]
    )
    assert seen[0] == (
        "POST",
        "/api/v1/apps",
        {"name": "web", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"},
    )
    assert seen[1] == (
        "PUT",
        "/api/v1/binds/heldso.example.com",
        {"dial": "web-app-1", "try": "30s"},
    )
    out = capsys.readouterr().out
    assert "https://origin.cursor.com/org/heldso" in out


def test_app_new_trigger(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        return 200, json.dumps(
            {
                "app": {
                    "name": body["name"],
                    "repo": body["repo"],
                    "ref": body["ref"],
                    "trigger": body.get("trigger", "push"),
                }
            }
        )

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(["app", "new", "--text", "--repo", "https://origin.cursor.com/org/app", "--ref", "dev", "--trigger", "manual"])
    body = seen[("POST", "/api/v1/apps")]
    assert body["trigger"] == "manual"
    assert body["name"] == "app-dev"


def test_app_new_ref_follows_the_checkout_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = tmp_path / "work"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "feature/x")
    monkeypatch.chdir(repo)
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        return 200, json.dumps(
            {"app": {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "trigger": "push"}}
        )

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    # A repo that is not the checkout's gets main, not the checkout branch.
    main(["app", "new", "--text", "--repo", "https://origin.cursor.com/org/other"])
    assert seen[("POST", "/api/v1/apps")] == {
        "name": "other",
        "repo": "https://origin.cursor.com/org/other",
        "ref": "main",
    }

    # The checkout's own repo, spelled with other case, takes the branch.
    main(["app", "new", "--text", "--repo", "https://origin.cursor.com/ORG/HELDSO"])
    assert seen[("POST", "/api/v1/apps")] == {
        "name": "heldso-feature-x",
        "repo": "https://origin.cursor.com/ORG/HELDSO",
        "ref": "feature/x",
    }


def test_app_set_message(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    state = {
        "name": "heldso",
        "repo": "https://origin.cursor.com/org/app",
        "ref": "main",
        "trigger": "push",
        "created": "",
    }

    def fake_api_try(client, method, path, body, timeout=None):
        if method == "GET":
            return 200, json.dumps({"app": dict(state)})
        if method == "PATCH":
            state.update(body)
            return 200, json.dumps({"app": dict(state)})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    # A ref move on a push app points at the next check.
    main(["app", "set", "--app", "heldso", "--ref", "dev", "--text"])
    out = capsys.readouterr().out
    assert "next check deploys it within 5 minutes" in out

    # A trigger change does not move the code.
    main(["app", "set", "--app", "heldso", "--trigger", "manual", "--text"])
    assert capsys.readouterr().out == "Saved.\n"

    # The same ref is not a move.
    main(["app", "set", "--app", "heldso", "--ref", "dev", "--text"])
    assert capsys.readouterr().out == "Saved.\n"

    # A ref move on a manual app stays quiet too.
    main(["app", "set", "--app", "heldso", "--ref", "main", "--text"])
    assert capsys.readouterr().out == "Saved.\n"


def test_env_commands(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}
    env = {"A": "1"}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if method == "GET":
            return 200, json.dumps(dict(env))
        if body is not None:
            if method == "PUT":
                env.clear()
                env.update(body)
            else:
                for key, value in body.items():
                    if value is None:
                        env.pop(key, None)
                    else:
                        env[key] = value
            return 200, json.dumps({"keys": sorted(env)})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(["env", "set", "B=2", "C=3", "--app", "heldso", "--text"])
    assert seen[("PATCH", "/api/v1/apps/heldso/env")] == {"B": "2", "C": "3"}
    assert capsys.readouterr().out == "Saved. Run yard app deploy to apply.\n"

    main(["env", "unset", "A", "--app", "heldso", "--text"])
    assert seen[("PATCH", "/api/v1/apps/heldso/env")] == {"A": None}

    main(["env", "show", "--app", "heldso", "--text"])
    out = capsys.readouterr().out
    assert "B=2" in out


def test_bind_commands(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if path == "/api/v1/binds" and method == "GET":
            row = {"host": "heldso.example.com", "dial": "heldso-app:8000", "state": "up"}
            return 200, json.dumps([row])
        if path.startswith("/api/v1/binds/") and method == "PUT":
            saved = {"host": "heldso.example.com", "dial": "heldso-app:9000", "state": "up"}
            return 200, json.dumps(saved)
        if path.startswith("/api/v1/binds/") and method == "DELETE":
            return 200, json.dumps({"host": "heldso.example.com", "dial": "heldso-app:8000"})
        if path == "/api/v1/apps":
            return 200, json.dumps([{"name": "heldso", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"}])
        return 200, json.dumps({"output": "ok\n"})

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)

    main(["bind", "set", "heldso.example.com", "heldso-app:9000", "--try", "30s", "--text"])
    assert seen[("PUT", "/api/v1/binds/heldso.example.com")] == {
        "dial": "heldso-app:9000",
        "try": "30s",
    }
    assert "heldso.example.com" in capsys.readouterr().out

    # No container: the deduced app names <app>-app-1.
    main(["bind", "set", "heldso.example.com", "--app", "heldso", "--text"])
    assert seen[("PUT", "/api/v1/binds/heldso.example.com")] == {
        "dial": "heldso-app-1",
        "try": "10s",
    }

    main(["bind", "ls", "--text"])
    assert "heldso.example.com" in capsys.readouterr().out

    main(["bind", "rm", "heldso.example.com", "--text"])
    assert seen[("DELETE", "/api/v1/binds/heldso.example.com")] is None
    assert "removed heldso.example.com" in capsys.readouterr().out


def test_podman_command_is_gone(capsys: pytest.CaptureFixture):
    with pytest.raises(SystemExit) as caught:
        main(["podman", "ps"])
    assert caught.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_prune(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen = {}

    def fake_api_try(client, method, path, body, timeout=None):
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
    _env(monkeypatch)

    main(["prune", "--text"])
    assert seen[("POST", "/api/v1/prune")] == {"days": 7, "dry": False}
    out = capsys.readouterr().out
    assert "removed 1 images, 3kB" in out
    assert "kept localhost/busy:t4g (image is in use by heldso-app-1)" in out

    main(["prune", "--days", "1", "--dry-run", "--text"])
    assert seen[("POST", "/api/v1/prune")] == {"days": 1, "dry": True}
    assert "would remove" in capsys.readouterr().out


def test_login_warns_when_no_forge_is_connected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "cfg" / "config.json")

    def fake_api_try(client, method, path, body, timeout=None):
        report = status_report()
        report["host"]["forges"] = {"origin": False, "github": False}
        return 200, json.dumps(report)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    main(["login", "--url", "http://127.0.0.1:8000", "--token", "tok"])
    err = capsys.readouterr().err
    assert cli.FORGE_HINT in err


def _checkout(path: Path, remote: str, branch: str = "main") -> None:
    env = {
        "GIT_AUTHOR_NAME": "Yard",
        "GIT_AUTHOR_EMAIL": "yard@example.com",
        "GIT_COMMITTER_NAME": "Yard",
        "GIT_COMMITTER_EMAIL": "yard@example.com",
    }
    subprocess.run(["git", "init", "-b", branch, str(path)], check=True, capture_output=True, env={**env})
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True, capture_output=True)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "one"], check=True, capture_output=True, env=env)


def test_remote_repo_spellings():
    for host in ("origin.cursor.com", "github.com"):
        url = f"https://{host}/org/heldso"
        assert cli._remote_repo(f"https://{host}/org/heldso") == url
        assert cli._remote_repo(f"https://{host}/org/heldso.git") == url
        assert cli._remote_repo(f"ssh://git@{host}/org/heldso.git") == url
        assert cli._remote_repo(f"git@{host}:org/heldso.git") == url
    assert cli._remote_repo("https://gitlab.com/org/heldso.git") == ""
    assert cli._remote_repo("https://github.com/org/heldso/extra.git") == ""


def test_app_deduction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = tmp_path / "work"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "main")
    monkeypatch.chdir(repo)

    def fake_api_try(client, method, path, body, timeout=None):
        return 200, json.dumps([{"name": "heldso", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"}])

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    assert cli._app_name(cli.Client("http://x", "t"), argparse_ns()) == "heldso"


def test_app_deduction_ssh_forms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for index, remote in enumerate(
        ("ssh://git@origin.cursor.com/org/heldso.git", "git@origin.cursor.com:org/heldso.git")
    ):
        repo = tmp_path / f"work{index}"
        _checkout(repo, remote, "main")
        monkeypatch.chdir(repo)

        def fake_api_try(client, method, path, body, timeout=None):
            return 200, json.dumps([{"name": "heldso", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"}])

        monkeypatch.setattr(cli, "_api_try", fake_api_try)
        _env(monkeypatch)
        assert cli._app_name(cli.Client("http://x", "t"), argparse_ns()) == "heldso"


def argparse_ns(**kw):
    import argparse

    return argparse.Namespace(app=kw.get("app", ""))


def test_app_deduction_feature_branch_names_the_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    repo = tmp_path / "work"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "feature/x")
    monkeypatch.chdir(repo)
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if path == "/api/v1/apps":
            return 200, json.dumps(
                {"app": {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "trigger": "push"}}
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    main(["app", "new", "--text"])
    assert seen[("POST", "/api/v1/apps")] == {
        "name": "heldso-feature-x",
        "repo": "https://origin.cursor.com/org/heldso",
        "ref": "feature/x",
    }


def test_app_deduction_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture):
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_api_try", lambda client, method, path, body, timeout=None: (200, "[]"))

    # Not a git cli.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    assert "--app" in capsys.readouterr().err

    # A remote that is not on Origin or GitHub.
    repo = tmp_path / "gh"
    _checkout(repo, "https://gitlab.com/org/heldso.git", "main")
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    assert "--app" in capsys.readouterr().err

    # A detached HEAD has no branch to match.
    repo = tmp_path / "det"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "main")
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "checkout", sha], check=True, capture_output=True)
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    assert "--app" in capsys.readouterr().err

    # On a branch no app follows.
    repo = tmp_path / "miss"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "dev")
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    err = capsys.readouterr().err
    assert "--app" in err
    assert "no app follows https://origin.cursor.com/org/heldso dev" in err
    assert "Create one here: yard app new" in err


def test_forge_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    rows = [
        {
            "name": "origin",
            "host": "origin.cursor.com",
            "connected": False,
            "app_id": "",
            "installation_id": "",
            "last_delivery": "",
            "last_check": "",
        },
        {
            "name": "github",
            "host": "github.com",
            "connected": True,
            "app_id": "42",
            "installation_id": "101",
            "selection": "all",
            "last_delivery": "",
            "last_check": "",
        },
    ]
    monkeypatch.setattr(cli, "_api_try", lambda *a, **k: (200, json.dumps(rows)))
    _env(monkeypatch)
    main(["forge", "ls", "--text"])
    out = capsys.readouterr().out
    assert "origin" in out and "github" in out and "yes" in out
    assert "selection" in out and "all" in out


def _fake_setup_api(calls: list):
    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps(
                [
                    {
                        "name": "origin",
                        "public_key": "-----BEGIN PUBLIC KEY-----\nPUB\n-----END PUBLIC KEY-----\n",
                        "installation_id": "ins_1",
                        "selection": "all",
                    },
                    {"name": "github", "installation_id": "101", "selection": "all"},
                ]
            )
        if path == "/api/v1/forges/origin" and method == "PUT":
            return 200, json.dumps(
                {
                    "forge": {"name": "origin"},
                    "app": {
                        "name": "yard",
                        "webhook_url": f"{client.url}/hooks/origin",
                        "events": ["repository.pushed"],
                    },
                }
            )
        if path == "/api/v1/forges/github" and method == "PUT":
            return 200, json.dumps(
                {"forge": {"name": "github"}, "app": {"name": "yard-host", "slug": "yard-host", "events": ["push"]}}
            )
        raise AssertionError(path)

    return fake_api_try


def _answers(*answers: str):
    it = iter(answers)
    return lambda _prompt="": next(it)


def _eof_after(*answers: str):
    it = iter(answers)

    def fake(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return fake


PEM = "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----\n"


def _pem(directory: Path, name: str = "key.pem") -> Path:
    path = directory / name
    path.write_text(PEM)
    return path


def _config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg"
    monkeypatch.setattr(cli, "config_path", lambda: cfg / "config.json")
    return cfg


def test_forge_setup_origin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("app_123"))
    _env(monkeypatch)
    main(["forge", "setup", "origin", "--no-browser", "--text"])
    out = capsys.readouterr().out
    assert "PUB" in out
    assert "http://127.0.0.1:8000/hooks/origin" in out
    assert "repository.pushed" in out
    assert "install?client_id=app_123" in out
    assert "Done." in out
    assert ("PUT", "/api/v1/forges/origin", {"app_id": "app_123"}) in calls


def test_forge_setup_polls_and_warns(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """The poll waits for the installation; a partial selection and a summary error warn."""
    reads = []

    def fake_api_try(client, method, path, body, timeout=None):
        if path == "/api/v1/forges":
            reads.append(1)
            count = len(reads)
            return 200, json.dumps(
                [
                    {
                        "name": "origin",
                        "public_key": "PUB\n",
                        "installation_id": "ins_1" if count > 2 else "",
                        "selection": "selected" if count > 2 else "",
                        "error": "origin GET /app/installations: refused" if count == 2 else "",
                    }
                ]
            )
        if path == "/api/v1/forges/origin" and method == "PUT":
            return 200, json.dumps(
                {
                    "forge": {"name": "origin"},
                    "app": {
                        "name": "yard",
                        "webhook_url": f"{client.url}/hooks/origin",
                        "events": ["repository.pushed"],
                    },
                }
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("app_123"))
    _env(monkeypatch)
    main(["forge", "setup", "origin", "--no-browser", "--text"])
    captured = capsys.readouterr()
    assert "Done." in captured.out
    assert len(reads) >= 3
    assert "warning: origin GET /app/installations: refused" in captured.err
    assert "the installation covers selected repos only" in captured.err


def test_forge_setup_origin_reprompts_a_bad_app_id(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("app_a b", "app_123"))
    _env(monkeypatch)
    main(["forge", "setup", "origin", "--no-browser", "--text"])
    assert "not an app ID" in capsys.readouterr().err
    puts = [call for call in calls if call[0] == "PUT"]
    assert puts == [("PUT", "/api/v1/forges/origin", {"app_id": "app_123"})]


def test_forge_setup_github(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path):
    pem = _pem(tmp_path, "app.pem")
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    # App ID first, then the key path (the --key default).
    monkeypatch.setattr("builtins.input", _answers("42", ""))
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "s" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--key", str(pem), "--no-browser", "--text"])
    out = capsys.readouterr().out
    err = capsys.readouterr().err
    assert "s" * 64 in out
    assert "Paste this as the webhook secret" in out
    assert "https://github.com/settings/apps/new" in out
    assert "name=yard-127.0.0.1" in out
    assert "https://github.com/apps/yard-host/installations/new" in out
    assert "Done." in out
    # The key goes to the API but never to the terminal.
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [
        (
            "PUT",
            "/api/v1/forges/github",
            {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64},
        )
    ]
    assert "PRIVATE KEY" not in out
    assert "PRIVATE KEY" not in err


def test_forge_setup_github_refuses_http_to_a_remote(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    called: list = []
    monkeypatch.setattr(cli, "_api_try", lambda *a, **k: called.append(a) or (200, "{}"))
    monkeypatch.setenv("YARD_URL", "http://yard.example.com")
    monkeypatch.setenv("YARD_TOKEN", "token")
    with pytest.raises(SystemExit):
        main(["forge", "setup", "github", "--no-browser", "--text"])
    assert "refusing to send the private key over http" in capsys.readouterr().err
    assert called == []


def test_forge_setup_github_accepts_quoted_and_escaped_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pem = _pem(tmp_path)
    spaced = _pem(tmp_path, "my key.pem")
    _config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "s" * 64)
    _env(monkeypatch)
    for answer in (f"'{pem}'", str(spaced).replace(" ", "\\ ")):
        calls: list = []
        monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
        monkeypatch.setattr("builtins.input", _answers("42", answer))
        main(["forge", "setup", "github", "--no-browser", "--text"])
        put = [call for call in calls if call[0] == "PUT"]
        assert put == [
            (
                "PUT",
                "/api/v1/forges/github",
                {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64},
            )
        ]


def test_forge_setup_github_reprompts_a_missing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("42", str(tmp_path / "missing.pem"), str(pem)))
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "s" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser", "--text"])
    assert "cannot read the key" in capsys.readouterr().err
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [("PUT", "/api/v1/forges/github", {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64})]


def test_forge_setup_github_reprompts_a_bad_app_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("forty-two", "42", str(pem)))
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "s" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser", "--text"])
    assert "not an app ID" in capsys.readouterr().err
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [("PUT", "/api/v1/forges/github", {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64})]


def test_forge_setup_github_retries_after_a_client_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    attempts = 0

    def fake_api_try(client, method, path, body, timeout=None):
        nonlocal attempts
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps([{"name": "github", "installation_id": "101", "selection": "all"}])
        if path == "/api/v1/forges/github" and method == "PUT":
            attempts += 1
            if attempts == 1:
                return 400, json.dumps({"error": {"message": "the app id does not match the key"}})
            return 200, json.dumps({"forge": {"name": "github"}, "app": {"slug": "yard-host"}})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    # First App ID + key are refused by the API; the wizard asks both again.
    monkeypatch.setattr("builtins.input", _answers("41", str(pem), "42", str(pem)))
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "s" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser", "--text"])
    assert "the app id does not match the key" in capsys.readouterr().err
    put = [call for call in calls if call[0] == "PUT"]
    assert [call[2]["app_id"] for call in put] == ["41", "42"]


def test_forge_setup_github_resumes_a_stopped_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    cfg = _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    opened: list = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    _env(monkeypatch)

    # Run 1 stops at the key prompt; the pending setup stays on disk.
    monkeypatch.setattr("builtins.input", _eof_after("42"))
    with pytest.raises(SystemExit):
        main(["forge", "setup", "github", "--text"])
    pending = cfg / "github-setup.json"
    saved = json.loads(pending.read_text())
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert saved["url"] == "http://127.0.0.1:8000"
    assert saved["secret"] and saved["created"]
    assert "stopped" in capsys.readouterr().err

    # Run 2 reuses the secret; the app form prints but does not open again.
    monkeypatch.setattr("builtins.input", _answers("42", str(pem)))
    main(["forge", "setup", "github", "--text"])
    out = capsys.readouterr().out
    assert "Continuing the setup from" in out
    assert "settings/apps/new" in out
    assert sum("settings/apps/new" in url for url in opened) == 1
    assert opened[-1] == "https://github.com/apps/yard-host/installations/new"
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [
        (
            "PUT",
            "/api/v1/forges/github",
            {"app_id": "42", "private_key": PEM, "webhook_secret": saved["secret"]},
        )
    ]
    assert not pending.exists()


def test_forge_setup_github_new_mints_a_fresh_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    cfg = _config_dir(monkeypatch, tmp_path)
    pending = cfg / "github-setup.json"
    cfg.mkdir(parents=True)
    pending.write_text(
        json.dumps({"url": "http://127.0.0.1:8000", "secret": "old-secret", "created": "2026-01-01T00:00:00+00:00"})
    )
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _fake_setup_api(calls))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("42", str(pem)))
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "n" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--new", "--no-browser", "--text"])
    assert "Continuing" not in capsys.readouterr().out
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [("PUT", "/api/v1/forges/github", {"app_id": "42", "private_key": PEM, "webhook_secret": "n" * 64})]
    assert not pending.exists()


def test_forge_setup_github_continues_to_the_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    reads = 0

    def fake_api_try(client, method, path, body, timeout=None):
        nonlocal reads
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            reads += 1
            row = {
                "name": "github",
                "app_id": "42",
                "slug": "yard-host",
                "installation_id": "" if reads == 1 else "101",
                "selection": "all",
            }
            return 200, json.dumps([row])
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser", "--text"])
    out = capsys.readouterr().out
    assert "https://github.com/apps/yard-host/installations/new" in out
    assert "Done." in out
    assert not [call for call in calls if call[0] == "PUT"]


def test_forge_setup_github_already_connected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    _config_dir(monkeypatch, tmp_path)
    calls: list = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps([{"name": "github", "connected": True, "app_id": "42", "installation_id": "101"}])
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser", "--text"])
    out = capsys.readouterr().out
    assert "already connected" in out
    assert "--new" in out
    assert calls == [("GET", "/api/v1/forges", None)]


def test_forge_setup_github_new_ignores_a_connected_forge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    """--new runs the full form flow even when the forge row says connected."""
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps([{"name": "github", "connected": True, "app_id": "42", "installation_id": "101"}])
        if path == "/api/v1/forges/github" and method == "PUT":
            return 200, json.dumps({"forge": {"name": "github"}, "app": {"slug": "yard-host"}})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("42", str(pem)))
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "n" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--new", "--no-browser", "--text"])
    out = capsys.readouterr().out
    assert "Continuing" not in out
    assert "Done." in out
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [("PUT", "/api/v1/forges/github", {"app_id": "42", "private_key": PEM, "webhook_secret": "n" * 64})]


def test_app_logs_builds_the_query(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        return 200, "line one\n"

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    main(["app", "logs", "--app", "heldso", "--since", "-1h", "--deploy", "--tail", "50", "--text"])
    assert calls == [("GET", "/api/v1/logs?lines=50&container=deploy-heldso&since=-1h", None)]
    assert "line one" in capsys.readouterr().out


def test_app_logs_follow_streams(monkeypatch: pytest.MonkeyPatch):
    seen = []
    monkeypatch.setattr(cli, "_stream_get", lambda client, path: seen.append(path))
    _env(monkeypatch)
    main(["app", "logs", "-f", "--app", "heldso", "--container", "heldso-worker-1", "--text"])
    assert seen == ["/api/v1/logs?lines=100&container=heldso-worker-1&follow=1"]


def test_logs_by_name_or_id(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []
    monkeypatch.setattr(cli, "_api_try", lambda *a, **k: calls.append(a[2]) or (200, "x\n"))
    _env(monkeypatch)
    main(["logs", "yard-caddy-1", "--tail", "10", "--text"])
    main(["logs", "abc123def456", "--text"])
    assert calls == [
        "/api/v1/logs?lines=10&container=yard-caddy-1",
        "/api/v1/logs?lines=100&container=abc123def456",
    ]


def test_events_build_the_query(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        return 200, json.dumps(
            [
                {
                    "time": "2026-10-05T12:00:00Z",
                    "source": "podman",
                    "event": "died",
                    "app": "heldso",
                    "container": "heldso-app-1",
                    "container_id": "abc123def456" + "0" * 52,
                    "exit_code": "1",
                }
            ]
        )

    monkeypatch.setattr(cli, "_api_try", fake_api_try)
    _env(monkeypatch)
    main(["events", "--app", "heldso", "--source", "podman", "--since", "-1h", "--text"])
    assert calls[-1] == ("GET", "/api/v1/events?lines=200&since=-1h&app=heldso&source=podman", None)
    out = capsys.readouterr().out
    assert "died" in out and "heldso-app-1 abc123def456" in out and "exit 1" in out
