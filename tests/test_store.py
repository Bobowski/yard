"""The SQLite store: rows, the schema upgrade, and the admin token."""

import sqlite3

import pytest

from app.errors import YardError
from app.store import Store, ensure_admin


def test_store_rows(tmp_path):
    store = Store.open(tmp_path / "db")
    try:
        store.create_repo("app")
        slot = store.create_slot("heldso", "app", "", "", "", "")
        assert slot.ref == "main"
        slot = store.update_slot("heldso", ref="release", dir="ops", trigger="every 5m")
        assert slot.ref == "release"
        assert slot.directory == "ops"
        assert slot.trigger == "every 5m"
        assert slot.job
        slot = store.update_slot("heldso", domain="", ref="")
        assert slot.ref == "main"
        assert store.slots_for_repo("app")[0].name == "heldso"

        plain = store.create_token("ada")
        assert store.token_ok(plain) is not None
        assert store.token_ok("nope") is None
        store.delete_token("ada")
        assert store.token_ok(plain) is None

        deploy_id = store.begin_deploy("heldso", "abc", "localhost/app:abc")
        store.mark_built(deploy_id, "2026-10-05T10:00:00Z")
        store.finish_deploy(deploy_id, "ok", "log\n")
        row = store.last_deploy("heldso")
        assert row is not None
        assert row.built
        assert row.ended
        assert row.log == "log\n"
        assert store.deploy(deploy_id).image == "localhost/app:abc"
    finally:
        store.close()


def test_schema_upgrades_an_old_database(tmp_path):
    directory = tmp_path / "db"
    directory.mkdir()
    db = sqlite3.connect(directory / "yard.sqlite3")
    db.executescript(
        """
        CREATE TABLE repos (name TEXT PRIMARY KEY, created TEXT NOT NULL);
        CREATE TABLE tokens (
            name TEXT PRIMARY KEY,
            hash TEXT NOT NULL,
            created TEXT NOT NULL,
            parent TEXT NOT NULL DEFAULT '',
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE deploys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot TEXT NOT NULL,
            sha TEXT NOT NULL,
            status TEXT NOT NULL,
            started TEXT NOT NULL,
            built TEXT NOT NULL DEFAULT '',
            ended TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO deploys (slot, sha, status, started) VALUES ('heldso', 'abc', 'ok', 'then');
        """
    )
    db.commit()
    db.close()

    store = Store.open(directory)
    try:
        old = store.last_deploy("heldso")
        assert old is not None
        assert old.image == ""
        assert old.log == ""
        deploy_id = store.begin_deploy("heldso", "def", "localhost/app:def")
        store.finish_deploy(deploy_id, "ok", "done\n")

        # A flat token still fits the table with the parent and revoked columns.
        plain = store.create_token("ada")
        assert store.token_ok(plain) is not None
        assert [token.name for token in store.list_tokens()] == ["ada"]
        store.delete_token("ada")
    finally:
        store.close()


def test_ensure_admin_writes_the_token_once(tmp_path):
    store = Store.open(tmp_path / "db")
    try:
        plain = ensure_admin(store, tmp_path / "data")
        assert plain
        assert (tmp_path / "data" / "admin.token").read_text().strip() == plain
        assert ensure_admin(store, tmp_path / "data") == ""
    finally:
        store.close()


def test_trigger_makes_a_job(tmp_path):
    store = Store.open(tmp_path / "db")
    try:
        store.create_repo("app")
        with pytest.raises(YardError, match="trigger must be"):
            store.create_slot("nightly", "app", "main", "", "", "1h")
        slot = store.create_slot("nightly", "app", "main", "", "", "every 1h")
        assert slot.job
        assert slot.every_seconds == 3600
        slot = store.update_slot("nightly", trigger="")
        assert not slot.job
        assert slot.every_seconds is None
    finally:
        store.close()


def test_the_trigger_column_migrates(tmp_path):
    # An old host has no trigger column; every becomes an `every` trigger once.
    directory = tmp_path / "db"
    directory.mkdir()
    db = sqlite3.connect(directory / "yard.sqlite3")
    db.execute(
        """
        CREATE TABLE slots (
            name TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            ref TEXT NOT NULL,
            dir TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            live_image TEXT NOT NULL DEFAULT '',
            every TEXT NOT NULL DEFAULT '',
            env_json TEXT NOT NULL DEFAULT '{}',
            created TEXT NOT NULL
        )
        """
    )
    db.execute(
        "INSERT INTO slots (name, repo, ref, dir, domain, every, created)"
        " VALUES ('nightly', 'app', 'main', '', '', '5m', 'x'),"
        "        ('heldso', 'app', 'main', '', '', '', 'x')"
    )
    db.commit()
    db.close()
    store = Store.open(directory)
    try:
        assert store.slot("nightly").trigger == "every 5m"
        assert store.slot("nightly").job
        assert store.slot("heldso").trigger == ""
        assert not store.slot("heldso").job
    finally:
        store.close()
