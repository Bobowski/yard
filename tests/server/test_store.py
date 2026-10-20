"""The file store: apps, tokens, and the admin token."""

import pytest

from app.server.store import Store, ensure_admin
from app.shared import YardError


def test_store_rows(tmp_path):
    store = Store.open(tmp_path)
    app = store.create_app("heldso", "https://origin.cursor.com/org/app")
    assert app.ref == "main"
    assert app.trigger == "push"
    app = store.update_app("heldso", ref="release", trigger="manual")
    assert app.ref == "release"
    assert app.trigger == "manual"
    text = (tmp_path / "apps" / "heldso" / "app").read_text()
    assert "repo https://origin.cursor.com/org/app\n" in text
    assert "trigger manual\n" in text
    app = store.update_app("heldso", ref="")
    assert app.ref == "main"
    assert [app.name for app in store.list_apps() if app.repo == "https://origin.cursor.com/org/app"] == ["heldso"]

    plain = store.create_token("ada")
    assert store.token_ok(plain) is not None
    assert store.token_ok("nope") is None
    store.delete_token("ada")
    assert store.token_ok(plain) is None


def test_the_record_has_four_keys(tmp_path):
    store = Store.open(tmp_path)
    store.create_app("heldso", "https://origin.cursor.com/org/app")
    fields = {}
    for line in (tmp_path / "apps" / "heldso" / "app").read_text().splitlines():
        key, _, value = line.partition(" ")
        fields[key] = value
    assert sorted(fields) == ["created", "ref", "repo", "trigger"]


def test_a_hand_edit_reloads(tmp_path):
    store = Store.open(tmp_path)
    store.create_app("heldso", "https://origin.cursor.com/org/app", trigger="manual")
    path = tmp_path / "apps" / "heldso" / "app"
    path.write_text(path.read_text().replace("trigger manual", "trigger push"))
    assert store.app("heldso").trigger == "push"


def test_bootstrap_repo_may_be_empty(tmp_path):
    store = Store.open(tmp_path)
    app = store.create_app("yard", "")
    assert app.repo == ""
    with pytest.raises(YardError) as caught:
        store.update_app("yard", repo="noslash")
    assert caught.value.code == "bad_repo"


def test_ensure_admin_writes_the_token_once(tmp_path):
    store = Store.open(tmp_path)
    plain = ensure_admin(store)
    assert plain
    assert (tmp_path / "tokens" / "admin.token").read_text().strip() == plain
    assert (tmp_path / "tokens" / "admin").is_file()
    assert ensure_admin(store) == ""


def test_trigger_is_push_or_manual(tmp_path):
    store = Store.open(tmp_path)
    with pytest.raises(YardError, match="trigger must be"):
        store.create_app("nightly", "https://origin.cursor.com/org/app", "main", "1h")
    app = store.create_app("nightly", "https://origin.cursor.com/org/app", "main", "manual")
    assert app.trigger == "manual"
    app = store.update_app("nightly", trigger="")
    assert app.trigger == "push"


def test_env_files_round_trip(tmp_path):
    store = Store.open(tmp_path)
    store.create_app("heldso", "https://origin.cursor.com/org/app")
    assert store.write_env("heldso", {"B": "2", "A": "1"}) == ["A", "B"]
    assert store.read_env("heldso") == {"A": "1", "B": "2"}
    assert store.patch_env("heldso", {"B": None, "C": "3"}) == ["A", "C"]
    assert store.read_env("heldso") == {"A": "1", "C": "3"}
    path = tmp_path / "apps" / "heldso" / "env"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
