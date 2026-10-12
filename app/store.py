"""Yard's SQLite state. One file, one process."""

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from app.errors import YardError
from app.values import (
    check_domain,
    check_name,
    check_ref,
    check_slot_name,
    clean_dir,
    parse_duration,
    parse_trigger,
)


def now() -> str:
    stamp = datetime.now(UTC)
    if stamp.microsecond == 0:
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    frac = f"{stamp.microsecond:06d}".rstrip("0")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + frac + "Z"


def token_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def _domain_taken(domain: str) -> NoReturn:
    raise YardError(
        409,
        "domain_taken",
        "domain already in use",
        context={"domain": domain},
        help_text="One domain belongs to one slot.",
    )


@dataclass(slots=True)
class Repo:
    name: str
    created: str


@dataclass(slots=True)
class Slot:
    name: str
    repo: str
    ref: str
    directory: str
    domain: str
    live_image: str
    trigger: str
    created: str

    @property
    def job(self) -> bool:
        return bool(self.trigger)

    @property
    def every_seconds(self) -> float | None:
        if not self.trigger.startswith("every "):
            return None
        return parse_duration(self.trigger[6:])


@dataclass(slots=True)
class Token:
    name: str
    created: str


@dataclass(slots=True)
class Deploy:
    id: int
    slot: str
    sha: str
    image: str
    status: str
    started: str
    built: str
    ended: str
    log: str


class Store:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    @classmethod
    def open(cls, directory: Path) -> Store:
        directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(directory / "yard.sqlite3", check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        store = cls(db)
        store._schema()
        return store

    def close(self) -> None:
        self._db.close()

    def _schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS repos (
                name TEXT PRIMARY KEY,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS slots (
                name TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                ref TEXT NOT NULL,
                dir TEXT NOT NULL DEFAULT '',
                domain TEXT NOT NULL,
                live_image TEXT NOT NULL DEFAULT '',
                trigger TEXT NOT NULL DEFAULT '',
                env_json TEXT NOT NULL DEFAULT '{}',
                created TEXT NOT NULL
            );
            -- An existing host has the old tokens table with parent and revoked columns.
            -- Both default, so inserts under this flat shape still work there.
            CREATE TABLE IF NOT EXISTS tokens (
                name TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deploys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot TEXT NOT NULL,
                sha TEXT NOT NULL,
                image TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                started TEXT NOT NULL,
                built TEXT NOT NULL DEFAULT '',
                ended TEXT NOT NULL DEFAULT '',
                log TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS slots_domain ON slots(domain) WHERE domain != ''")

        # An existing deploys table gains image, log, and built.
        # 0.1.1 has image and log. It has no built column.
        columns = {str(row["name"]) for row in self._db.execute("PRAGMA table_info(deploys)")}
        for column in ("image", "log", "built"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE deploys ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

        # An existing slots table gains trigger. Old hosts used every as the
        # schedule, so a row with every set becomes an `every` trigger once.
        slot_columns = {str(row["name"]) for row in self._db.execute("PRAGMA table_info(slots)")}
        if "trigger" not in slot_columns:
            self._db.execute("ALTER TABLE slots ADD COLUMN trigger TEXT NOT NULL DEFAULT ''")
            self._db.execute("UPDATE slots SET trigger = 'every ' || every WHERE every != ''")

        # A host from before flat tokens still has the revoked column. Drop the
        # revoked rows once: under the flat shape nothing could set revoked back,
        # so a revived row would wrongly become a live token again.
        token_columns = {str(row["name"]) for row in self._db.execute("PRAGMA table_info(tokens)")}
        if "revoked" in token_columns:
            self._db.execute("DELETE FROM tokens WHERE revoked = 1")
        self._db.commit()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self._db.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return list(self._db.execute(sql, args))

    def _write(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        try:
            cur = self._db.execute(sql, args)
            self._db.commit()
        except sqlite3.IntegrityError as exc:
            self._db.rollback()
            _raise_integrity(exc)
        return cur

    def list_repos(self) -> list[Repo]:
        rows = self._all("SELECT name, created FROM repos ORDER BY name")
        return [Repo(str(row["name"]), str(row["created"])) for row in rows]

    def repo(self, name: str) -> Repo:
        row = self._one("SELECT name, created FROM repos WHERE name = ?", (name,))
        if row is None:
            raise YardError(
                404,
                "not_found",
                "repo does not exist",
                context={"name": name},
                help_text="Run yard repo list to see the repos on this host.",
            )
        return Repo(str(row["name"]), str(row["created"]))

    def create_repo(self, name: str) -> None:
        check_name(name)
        self._write("INSERT INTO repos (name, created) VALUES (?, ?)", (name, now()))

    def delete_repo(self, name: str) -> None:
        count = self._one("SELECT COUNT(*) AS n FROM slots WHERE repo = ?", (name,))
        if count and int(count["n"]) > 0:
            raise YardError(
                409,
                "repo_in_use",
                "repo still has slots",
                context={"name": name},
                help_text="Remove each slot that uses this repo, then delete the repo.",
                example="yard slot rm heldso",
            )
        cur = self._write("DELETE FROM repos WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise YardError(
                404,
                "not_found",
                "repo does not exist",
                context={"name": name},
                help_text="Run yard repo list to see the repos on this host.",
            )

    def list_slots(self) -> list[Slot]:
        return [self._slot(row) for row in self._all(_SLOT_SQL + " ORDER BY name")]

    def slot(self, name: str) -> Slot:
        row = self._one(_SLOT_SQL + " WHERE name = ?", (name,))
        if row is None:
            raise YardError(
                404,
                "not_found",
                "slot does not exist",
                context={"name": name},
                help_text="Run yard slot list to see the apps on this host.",
            )
        return self._slot(row)

    def slots_for_repo(self, repo: str) -> list[Slot]:
        return [slot for slot in self.list_slots() if slot.repo == repo]

    def create_slot(
        self,
        name: str,
        repo: str,
        ref: str = "",
        directory: str = "",
        domain: str = "",
        trigger: str = "",
    ) -> Slot:
        check_slot_name(name)
        check_name(repo)
        ref = ref or "main"
        check_ref(ref)
        directory = clean_dir(directory)
        self._domain_free(domain, "")
        if self._one("SELECT 1 FROM repos WHERE name = ?", (repo,)) is None:
            raise YardError(
                404,
                "not_found",
                "repo does not exist",
                context={"repo": repo},
                help_text="Create the repo first.",
                example=f"yard repo new {repo}",
            )
        trigger = parse_trigger(trigger)
        self._write(
            """
            INSERT INTO slots (name, repo, ref, dir, domain, trigger, created)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, repo, ref, directory, domain, trigger, now()),
        )
        return self.slot(name)

    def update_slot(
        self,
        name: str,
        *,
        ref: str | None = None,
        dir: str | None = None,
        domain: str | None = None,
        trigger: str | None = None,
    ) -> Slot:
        slot = self.slot(name)
        if ref is not None:
            ref = ref or "main"
            check_ref(ref)
            slot.ref = ref
        if dir is not None:
            slot.directory = clean_dir(dir)
        if domain is not None:
            self._domain_free(domain, name)
            slot.domain = domain
        if trigger is not None:
            slot.trigger = parse_trigger(trigger)

        self._write(
            "UPDATE slots SET ref = ?, dir = ?, domain = ?, trigger = ? WHERE name = ?",
            (slot.ref, slot.directory, slot.domain, slot.trigger, name),
        )
        return self.slot(name)

    def set_image(self, name: str, image: str) -> None:
        self._write("UPDATE slots SET live_image = ? WHERE name = ?", (image, name))

    def delete_slot(self, name: str) -> None:
        cur = self._write("DELETE FROM slots WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise YardError(
                404,
                "not_found",
                "slot does not exist",
                context={"name": name},
                help_text="Run yard slot list to see the apps on this host.",
            )

    def _domain_free(self, domain: str, except_name: str) -> None:
        check_domain(domain)
        if not domain:
            return
        row = self._one("SELECT name FROM slots WHERE domain = ? AND name != ?", (domain, except_name))
        if row is not None:
            _domain_taken(domain)

    def create_token(self, name: str) -> str:
        check_name(name)
        plain = secrets.token_hex(24)
        self._write("INSERT INTO tokens (name, hash, created) VALUES (?, ?, ?)", (name, token_hash(plain), now()))
        return plain

    def list_tokens(self) -> list[Token]:
        rows = self._all("SELECT name, created FROM tokens ORDER BY name")
        return [Token(str(row["name"]), str(row["created"])) for row in rows]

    def delete_token(self, name: str, actor: str = "") -> None:
        if name == actor:
            raise YardError(
                403,
                "forbidden",
                "you cannot delete your own token",
                context={"name": name},
                help_text="Ask another token to remove it.",
            )
        cur = self._write("DELETE FROM tokens WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise YardError(
                404,
                "not_found",
                "unknown token",
                context={"name": name},
                help_text="Run yard token list.",
            )

    def token_ok(self, plain: str) -> Token | None:
        if not plain:
            return None
        row = self._one("SELECT name, created FROM tokens WHERE hash = ?", (token_hash(plain),))
        if row is None:
            return None
        return Token(str(row["name"]), str(row["created"]))

    def begin_deploy(self, slot: str, sha: str, image: str) -> int:
        cur = self._write(
            """
            INSERT INTO deploys (slot, sha, image, status, started)
            VALUES (?, ?, ?, 'running', ?)
            """,
            (slot, sha, image, now()),
        )
        return int(cur.lastrowid or 0)

    def mark_built(self, deploy_id: int, stamp: str) -> None:
        self._write("UPDATE deploys SET built = ? WHERE id = ?", (stamp, deploy_id))

    def finish_deploy(self, deploy_id: int, status: str, log: str) -> None:
        self._write(
            "UPDATE deploys SET status = ?, log = ?, ended = ? WHERE id = ?",
            (status, log, now(), deploy_id),
        )

    def interrupt_deploys(self) -> None:
        self._write("UPDATE deploys SET status = 'interrupted', ended = ? WHERE status = 'running'", (now(),))

    def deploy(self, deploy_id: int) -> Deploy:
        row = self._one(_DEPLOY_SQL + " WHERE id = ?", (deploy_id,))
        if row is None:
            raise YardError(
                404,
                "not_found",
                "deploy does not exist",
                context={"id": deploy_id},
                help_text="Run yard deploys <name> to see the ids.",
            )
        return self._deploy(row)

    def list_deploys(self, slot: str, limit: int) -> list[Deploy]:
        if limit <= 0 or limit > 100:
            limit = 20
        rows = self._all(_DEPLOY_SQL + " WHERE slot = ? ORDER BY id DESC LIMIT ?", (slot, limit))
        return [self._deploy(row) for row in rows]

    def legacy_env(self, name: str) -> dict[str, str]:
        """Env values stored on the slot row by a 0.1 host. Empty when the column is absent or blank."""
        columns = {str(row["name"]) for row in self._db.execute("PRAGMA table_info(slots)")}
        if "env_json" not in columns:
            return {}
        row = self._one("SELECT env_json FROM slots WHERE name = ?", (name,))
        if row is None:
            return {}
        try:
            raw = json.loads(str(row["env_json"] or "{}"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}

    def last_deploy(self, slot: str) -> Deploy | None:
        row = self._one(_DEPLOY_SQL + " WHERE slot = ? ORDER BY id DESC LIMIT 1", (slot,))
        return None if row is None else self._deploy(row)

    def _slot(self, row: sqlite3.Row) -> Slot:
        return Slot(
            name=str(row["name"]),
            repo=str(row["repo"]),
            ref=str(row["ref"]),
            directory=str(row["dir"]),
            domain=str(row["domain"]),
            live_image=str(row["live_image"]),
            trigger=str(row["trigger"]),
            created=str(row["created"]),
        )

    def _deploy(self, row: sqlite3.Row) -> Deploy:
        return Deploy(
            int(row["id"]),
            str(row["slot"]),
            str(row["sha"]),
            str(row["image"]),
            str(row["status"]),
            str(row["started"]),
            str(row["built"] or ""),
            str(row["ended"]),
            str(row["log"] or ""),
        )


# env_json stays in the table. A deploy copies it into the env file once, when that file is empty.
_SLOT_SQL = "SELECT name, repo, ref, dir, domain, live_image, trigger, created FROM slots"
_DEPLOY_SQL = "SELECT id, slot, sha, image, status, started, built, ended, log FROM deploys"


def _raise_integrity(exc: sqlite3.IntegrityError) -> NoReturn:
    text = str(exc)
    if "slots_domain" in text or "slots.domain" in text:
        raise YardError(
            409,
            "domain_taken",
            "domain already in use",
            help_text="One domain belongs to one slot.",
        ) from exc
    if "UNIQUE" in text:
        raise YardError(409, "exists", "already exists", help_text="Pick another name.") from exc
    raise exc


def ensure_admin(store: Store, directory: Path) -> str:
    if store.list_tokens():
        return ""
    plain = store.create_token("admin")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "admin.token"
    path.write_text(plain + "\n")
    path.chmod(0o600)
    return plain
