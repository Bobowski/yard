"""The file store: projects, tokens, and the admin token."""

import pytest

from app.errors import YardError
from app.store import Store, ensure_admin


def test_store_rows(tmp_path):
    store = Store.open(tmp_path)
    store.create_repo("app")
    slot = store.create_slot("heldso", "app")
    assert slot.ref == "main"
    slot = store.update_slot("heldso", ref="release", dir="ops", trigger="every 5m")
    assert slot.ref == "release"
    assert slot.directory == "ops"
    assert slot.trigger == "every 5m"
    assert slot.job
    text = (tmp_path / "projects" / "heldso" / "project").read_text()
    assert "repo app\n" in text
    assert "trigger every 5m\n" in text
    slot = store.update_slot("heldso", ref="")
    assert slot.ref == "main"
    assert store.slots_for_repo("app")[0].name == "heldso"

    plain = store.create_token("ada")
    assert store.token_ok(plain) is not None
    assert store.token_ok("nope") is None
    store.delete_token("ada")
    assert store.token_ok(plain) is None


def test_a_hand_edit_reloads(tmp_path):
    store = Store.open(tmp_path)
    store.create_repo("app")
    store.create_slot("heldso", "app", trigger="every 1h")
    path = tmp_path / "projects" / "heldso" / "project"
    path.write_text(path.read_text().replace("trigger every 1h", "trigger every 5m"))
    assert store.slot("heldso").trigger == "every 5m"
    assert store.slot("heldso").every_seconds == 300


def test_ensure_admin_writes_the_token_once(tmp_path):
    store = Store.open(tmp_path)
    plain = ensure_admin(store)
    assert plain
    assert (tmp_path / "tokens" / "admin.token").read_text().strip() == plain
    assert (tmp_path / "tokens" / "admin").is_file()
    assert ensure_admin(store) == ""


def test_trigger_makes_a_job(tmp_path):
    store = Store.open(tmp_path)
    store.create_repo("app")
    with pytest.raises(YardError, match="trigger must be"):
        store.create_slot("nightly", "app", "main", "", "1h")
    slot = store.create_slot("nightly", "app", "main", "", "every 1h")
    assert slot.job
    assert slot.every_seconds == 3600
    slot = store.update_slot("nightly", trigger="")
    assert not slot.job
    assert slot.every_seconds is None


def test_repos_are_directories(tmp_path):
    store = Store.open(tmp_path)
    with pytest.raises(YardError) as caught:
        store.repo("app")
    assert caught.value.code == "not_found"
    store.create_repo("app")
    assert store.repo("app").name == "app"
    assert [repo.name for repo in store.list_repos()] == ["app"]
    store.create_slot("heldso", "app")
    with pytest.raises(YardError) as caught:
        store.delete_repo("app")
    assert caught.value.code == "repo_in_use"
    store.delete_slot("heldso")
    store.delete_repo("app")
    assert (tmp_path / "repos" / "app.git").is_dir()
