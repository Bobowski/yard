"""Yard's SQLite state. One file, one writer lock."""

import hashlib
import json
import posixpath
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from app.config import YARD_IMAGE
from app.errors import YardError

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DIR = re.compile(r"^[A-Za-z0-9._/-]+$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_nano() -> str:
    stamp = datetime.now(UTC)
    if stamp.microsecond == 0:
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    frac = f"{stamp.microsecond:06d}".rstrip("0")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + frac + "Z"


def token_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def check_name(name: str) -> None:
    if not _NAME.fullmatch(name) or name == "yard" or name.startswith("yard-"):
        raise YardError(400, "bad_name", "name must be a short lowercase word")


def check_slot_name(name: str) -> None:
    check_name(name)
    if name == "caddy":
        raise YardError(400, "bad_name", "name must be a short lowercase word")


def check_ref(ref: str) -> None:
    if not ref or ".." in ref or ref.startswith("/") or any(ch in ref for ch in " \t\\"):
        raise YardError(400, "bad_ref", "ref must be a branch name")


def check_domain(domain: str) -> None:
    if not domain:
        return
    if ".." in domain or not _DOMAIN.fullmatch(domain):
        raise YardError(400, "bad_domain", "domain must be a hostname")


def check_image(image: str) -> None:
    if not image:
        return
    if image != YARD_IMAGE:
        raise YardError(400, "bad_image", f"image must be {YARD_IMAGE}")


def clean_dir(directory: str) -> str:
    directory = directory.strip()
    if directory in {"", ".", "./"}:
        return ""
    if directory.startswith("/") or not _DIR.fullmatch(directory):
        raise YardError(400, "bad_dir", "directory must be a relative path inside the repo, such as projects/mono")
    clean = posixpath.normpath(directory)
    if clean in {".", ".."} or clean.startswith("../") or "/../" in f"/{clean}/":
        raise YardError(400, "bad_dir", "directory must be a relative path inside the repo, such as projects/mono")
    return clean


def parse_duration(text: str) -> float:
    if not text:
        raise ValueError("empty")
    total = 0.0
    i = 0
    size = len(text)
    while i < size:
        start = i
        if text[i] == ".":
            i += 1
        if i >= size or not text[i].isdigit():
            raise ValueError(text)
        while i < size and (text[i].isdigit() or text[i] == "."):
            i += 1
        number = float(text[start:i])
        unit_at = i
        while i < size and (text[i].isalpha() or text[i] in "µμ"):
            i += 1
        unit = text[unit_at:i]
        if unit not in _UNITS:
            raise ValueError(unit)
        total += number * _UNITS[unit]
    return total


def parse_every(text: str) -> str:
    text = text.strip().lower().removeprefix("every ")
    if text in {"", "off", "-"}:
        return ""
    try:
        seconds = parse_duration(text)
    except ValueError as exc:
        raise YardError(400, "bad_every", "every must be a duration such as 5m, or empty") from exc
    if seconds < 1:
        raise YardError(400, "bad_every", "every must be a duration such as 5m, or empty")
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return text


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
    every: str
    env: dict[str, str]
    created: str


@dataclass(slots=True)
class Token:
    name: str
    parent: str
    revoked: bool
    created: str


@dataclass(slots=True)
class Deploy:
    id: int
    slot: str
    repo: str
    sha: str
    image: str
    status: str
    log: str
    started: str
    ended: str


@dataclass(slots=True)
class Settings:
    domain: str
    email: str
    self_repo: str
    self_ref: str
    self_image: str


@dataclass(slots=True)
class Label:
    key: str
    value: str


@dataclass(slots=True)
class Experiment:
    id: int
    title: str
    body: str
    labels: list[Label] = field(default_factory=list)
    created: str = ""
    updated: str = ""


class Store:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._lock = threading.Lock()

    @classmethod
    def open(cls, directory: Path) -> Store:
        directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(directory / "yard.sqlite3", check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        store = cls(db)
        store._migrate()
        return store

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
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
                every TEXT NOT NULL DEFAULT '',
                env_json TEXT NOT NULL DEFAULT '{}',
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                name TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                parent TEXT NOT NULL DEFAULT '',
                revoked INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deploys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot TEXT NOT NULL,
                repo TEXT NOT NULL DEFAULT '',
                sha TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL,
                status TEXT NOT NULL,
                log TEXT NOT NULL DEFAULT '',
                started TEXT NOT NULL,
                ended TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exp_labels (
                exp_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS exp_labels_exp ON exp_labels(exp_id);
            """
        )
        try:
            self._db.execute("ALTER TABLE slots ADD COLUMN every TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        self._db.execute("DROP INDEX IF EXISTS slots_port")
        self._db.execute("DROP INDEX IF EXISTS deploy_steps_deploy")
        self._db.execute("DROP TABLE IF EXISTS deploy_steps")
        self._db.execute("DROP TABLE IF EXISTS events")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(slots)")}
        if "host_port" in columns:
            self._db.execute("ALTER TABLE slots DROP COLUMN host_port")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS slots_domain ON slots(domain) WHERE domain != ''")
        self._db.commit()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, args))

    def _write(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            try:
                cur = self._db.execute(sql, args)
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                _raise_integrity(exc)
            return cur

    def meta(self, key: str) -> str:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return "" if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def list_repos(self) -> list[Repo]:
        rows = self._all("SELECT name, created FROM repos ORDER BY name")
        return [Repo(str(row["name"]), str(row["created"])) for row in rows]

    def repo(self, name: str) -> Repo:
        row = self._one("SELECT name, created FROM repos WHERE name = ?", (name,))
        if row is None:
            raise YardError(404, "not_found", "repo does not exist")
        return Repo(str(row["name"]), str(row["created"]))

    def create_repo(self, name: str) -> None:
        check_name(name)
        self._write("INSERT INTO repos (name, created) VALUES (?, ?)", (name, now()))

    def delete_repo(self, name: str) -> None:
        count = self._one("SELECT COUNT(*) AS n FROM slots WHERE repo = ?", (name,))
        if count and int(count["n"]) > 0:
            raise YardError(409, "repo_in_use", "repo still has slots")
        cur = self._write("DELETE FROM repos WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise YardError(404, "not_found", "repo does not exist")

    def list_slots(self) -> list[Slot]:
        return [self._slot(row) for row in self._all(_SLOT_SQL + " ORDER BY name")]

    def slot(self, name: str) -> Slot:
        row = self._one(_SLOT_SQL + " WHERE name = ?", (name,))
        if row is None:
            raise YardError(404, "not_found", "slot does not exist")
        return self._slot(row)

    def slots_for_repo(self, repo: str) -> list[Slot]:
        return [slot for slot in self.list_slots() if slot.repo == repo]

    def create_slot(self, name: str, repo: str, ref: str, domain: str, directory: str, every: str) -> Slot:
        check_slot_name(name)
        check_name(repo)
        ref = ref or "main"
        check_ref(ref)
        directory = clean_dir(directory)
        self._domain_free(domain, "")
        if self._one("SELECT 1 FROM repos WHERE name = ?", (repo,)) is None:
            raise YardError(404, "not_found", "repo does not exist")
        every = parse_every(every)
        self._write(
            """
            INSERT INTO slots (name, repo, ref, dir, domain, every, env_json, created)
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (name, repo, ref, directory, domain, every, now()),
        )
        return self.slot(name)

    def delete_slot(self, name: str) -> None:
        cur = self._write("DELETE FROM slots WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise YardError(404, "not_found", "slot does not exist")

    def patch_slot(
        self,
        name: str,
        ref: str | None,
        domain: str | None,
        directory: str | None,
        every: str | None,
        env: dict[str, str],
    ) -> Slot:
        slot = self.slot(name)
        if ref is not None:
            ref = ref or "main"
            check_ref(ref)
            slot.ref = ref
        if directory is not None:
            slot.directory = clean_dir(directory)
        if domain is not None:
            self._domain_free(domain, name)
            slot.domain = domain
        if every is not None:
            slot.every = parse_every(every)
        slot.env = env
        self._write(
            "UPDATE slots SET ref = ?, dir = ?, domain = ?, every = ?, env_json = ? WHERE name = ?",
            (slot.ref, slot.directory, slot.domain, slot.every, json.dumps(env, sort_keys=True), name),
        )
        return self.slot(name)

    def set_image(self, name: str, live: str) -> None:
        self._write("UPDATE slots SET live_image = ? WHERE name = ?", (live, name))

    def _domain_free(self, domain: str, except_name: str) -> None:
        check_domain(domain)
        if not domain:
            return
        if domain == self.meta("domain"):
            raise YardError(409, "domain_taken", "domain already in use")
        row = self._one("SELECT name FROM slots WHERE domain = ? AND name != ?", (domain, except_name))
        if row is not None:
            raise YardError(409, "domain_taken", "domain already in use")

    def create_token(self, name: str, parent: str) -> str:
        check_name(name)
        if parent:
            check_name(parent)
            row = self._one("SELECT COUNT(*) AS n FROM tokens WHERE name = ? AND revoked = 0", (parent,))
            if row is None or int(row["n"]) == 0:
                raise YardError(404, "not_found", "unknown token")
        plain = secrets.token_hex(24)
        self._write(
            "INSERT INTO tokens (name, hash, parent, created) VALUES (?, ?, ?, ?)",
            (name, token_hash(plain), parent, now()),
        )
        return plain

    def list_tokens(self) -> list[Token]:
        rows = self._all("SELECT name, parent, revoked, created FROM tokens ORDER BY name")
        return [self._token(row) for row in rows]

    def revoke_token(self, actor: str, name: str) -> None:
        if not actor or actor == name:
            raise YardError(403, "forbidden", "that token is outside your tree")
        if self._one("SELECT 1 FROM tokens WHERE name = ?", (name,)) is None:
            raise YardError(404, "not_found", "unknown token")
        if not self._descendant(actor, name):
            raise YardError(403, "forbidden", "that token is outside your tree")
        self._write(
            """
            WITH RECURSIVE sub(name) AS (
                SELECT name FROM tokens WHERE name = ?
                UNION
                SELECT t.name FROM tokens t JOIN sub ON t.parent = sub.name
            )
            UPDATE tokens SET revoked = 1 WHERE name IN (SELECT name FROM sub)
            """,
            (name,),
        )

    def token_ok(self, plain: str) -> Token | None:
        if not plain:
            return None
        row = self._one(
            "SELECT name, parent, revoked, created FROM tokens WHERE hash = ?",
            (token_hash(plain),),
        )
        if row is None:
            return None
        token = self._token(row)
        if token.revoked or not self._ancestors_live(token.name):
            return None
        return token

    def _descendant(self, actor: str, name: str) -> bool:
        row = self._one(
            """
            WITH RECURSIVE sub(name) AS (
                SELECT name FROM tokens WHERE parent = ?
                UNION
                SELECT t.name FROM tokens t JOIN sub ON t.parent = sub.name
            )
            SELECT COUNT(*) AS n FROM sub WHERE name = ?
            """,
            (actor, name),
        )
        return row is not None and int(row["n"]) > 0

    def _ancestors_live(self, name: str) -> bool:
        seen: set[str] = set()
        while name:
            if name in seen:
                return False
            seen.add(name)
            row = self._one("SELECT parent, revoked FROM tokens WHERE name = ?", (name,))
            if row is None or int(row["revoked"]):
                return False
            name = str(row["parent"])
        return True

    def begin_deploy(self, slot: str, repo: str, sha: str) -> int:
        cur = self._write(
            """
            INSERT INTO deploys (slot, repo, sha, image, status, log, started, ended)
            VALUES (?, ?, ?, '', 'running', '', ?, '')
            """,
            (slot, repo, sha, now_nano()),
        )
        return int(cur.lastrowid or 0)

    def finish_deploy(self, deploy_id: int, status: str, image: str, log: str) -> None:
        self._write(
            "UPDATE deploys SET status = ?, image = ?, log = ?, ended = ? WHERE id = ?",
            (status, image, log, now_nano(), deploy_id),
        )

    def interrupt_deploys(self) -> None:
        self._write("UPDATE deploys SET status = 'interrupted', ended = ? WHERE status = 'running'", (now_nano(),))

    def deploy(self, deploy_id: int) -> Deploy:
        row = self._one(
            "SELECT id, slot, repo, sha, image, status, log, started, ended FROM deploys WHERE id = ?",
            (deploy_id,),
        )
        if row is None:
            raise YardError(404, "not_found", "deploy does not exist")
        return self._deploy(row)

    def list_deploys(self, slot: str, limit: int) -> list[Deploy]:
        if limit <= 0 or limit > 100:
            limit = 20
        rows = self._all(
            """
            SELECT id, slot, repo, sha, image, status, log, started, ended FROM deploys
            WHERE slot = ?
            ORDER BY id DESC LIMIT ?
            """,
            (slot, limit),
        )
        return [self._deploy(row) for row in rows]

    def last_deploy(self, slot: str) -> Deploy | None:
        row = self._one(
            """
            SELECT id, slot, repo, sha, image, status, log, started, ended
            FROM deploys WHERE slot = ? ORDER BY id DESC LIMIT 1
            """,
            (slot,),
        )
        return None if row is None else self._deploy(row)

    def settings(self) -> Settings:
        ref = self.meta("self_ref") or "main"
        return Settings(self.meta("domain"), self.meta("email"), self.meta("self_repo"), ref, self.meta("self_image"))

    def save_settings(self, incoming: Settings) -> None:
        if incoming.domain:
            check_domain(incoming.domain)
            if self._one("SELECT name FROM slots WHERE domain = ?", (incoming.domain,)) is not None:
                raise YardError(409, "domain_taken", "domain already in use")
        if incoming.self_image and incoming.self_repo:
            raise YardError(400, "one_source", "set self_image or self_repo")
        if incoming.self_image:
            check_image(incoming.self_image)
        if incoming.self_repo:
            check_name(incoming.self_repo)
            self.repo(incoming.self_repo)
        ref = incoming.self_ref or "main"
        check_ref(ref)
        pairs = (
            ("domain", incoming.domain),
            ("email", incoming.email),
            ("self_repo", incoming.self_repo),
            ("self_ref", ref),
            ("self_image", incoming.self_image),
        )
        sql = "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        with self._lock:
            try:
                for key, value in pairs:
                    self._db.execute(sql, (key, value))
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                _raise_integrity(exc)

    def list_experiments(self) -> list[Experiment]:
        rows = self._all("SELECT id, title, body, created, updated FROM experiments ORDER BY id DESC")
        found = [
            Experiment(
                int(row["id"]),
                str(row["title"]),
                str(row["body"]),
                [],
                str(row["created"]),
                str(row["updated"]),
            )
            for row in rows
        ]
        self._attach_labels(found)
        return found

    def save_experiments(
        self,
        blocks: list[tuple[bool, int, str, str, list[Label]]],
        drop: list[int],
    ) -> list[Experiment]:
        """blocks are (has_id, id, title, body, labels). Unknown ids stop the save."""

        def run(db: sqlite3.Connection) -> list[Experiment]:
            for has_id, exp_id, _title, _body, _labels in blocks:
                if not has_id:
                    continue
                row = db.execute("SELECT COUNT(*) AS n FROM experiments WHERE id = ?", (exp_id,)).fetchone()
                if row is None or int(row["n"]) == 0:
                    raise YardError(404, "not_found", f"experiment does not exist: {exp_id}")
            stamp = now()
            saved: list[Experiment] = []
            for has_id, exp_id, title, body, labels in blocks:
                if has_id:
                    created_row = db.execute("SELECT created FROM experiments WHERE id = ?", (exp_id,)).fetchone()
                    created = str(created_row["created"]) if created_row else stamp
                    db.execute(
                        "UPDATE experiments SET title = ?, body = ?, updated = ? WHERE id = ?",
                        (title, body, stamp, exp_id),
                    )
                else:
                    cur = db.execute(
                        "INSERT INTO experiments (title, body, created, updated) VALUES (?, ?, ?, ?)",
                        (title, body, stamp, stamp),
                    )
                    exp_id = int(cur.lastrowid or 0)
                    created = stamp
                db.execute("DELETE FROM exp_labels WHERE exp_id = ?", (exp_id,))
                for label in labels:
                    db.execute(
                        "INSERT INTO exp_labels (exp_id, key, value) VALUES (?, ?, ?)",
                        (exp_id, label.key, label.value),
                    )
                saved.append(Experiment(exp_id, title, body, list(labels), created, stamp))
            keep = {exp_id for has_id, exp_id, *_rest in blocks if has_id}
            for exp_id in drop:
                if exp_id in keep:
                    continue
                db.execute("DELETE FROM exp_labels WHERE exp_id = ?", (exp_id,))
                db.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
            return saved

        with self._lock:
            try:
                saved = run(self._db)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return saved

    def _attach_labels(self, experiments: list[Experiment]) -> None:
        if not experiments:
            return
        by_id: dict[int, list[Label]] = {}
        for row in self._all("SELECT exp_id, key, value FROM exp_labels ORDER BY exp_id, rowid"):
            by_id.setdefault(int(row["exp_id"]), []).append(Label(str(row["key"]), str(row["value"])))
        for exp in experiments:
            exp.labels = by_id.get(exp.id, [])

    def _slot(self, row: sqlite3.Row) -> Slot:
        raw = str(row["env_json"] or "{}")
        env = json.loads(raw)
        if not isinstance(env, dict):
            env = {}
        return Slot(
            name=str(row["name"]),
            repo=str(row["repo"]),
            ref=str(row["ref"]),
            directory=str(row["dir"]),
            domain=str(row["domain"]),
            live_image=str(row["live_image"]),
            every=str(row["every"]),
            env={str(key): str(value) for key, value in env.items()},
            created=str(row["created"]),
        )

    def _token(self, row: sqlite3.Row) -> Token:
        return Token(str(row["name"]), str(row["parent"]), bool(row["revoked"]), str(row["created"]))

    def _deploy(self, row: sqlite3.Row) -> Deploy:
        return Deploy(
            int(row["id"]),
            str(row["slot"]),
            str(row["repo"]),
            str(row["sha"]),
            str(row["image"]),
            str(row["status"]),
            str(row["log"]),
            str(row["started"]),
            str(row["ended"]),
        )


_SLOT_SQL = "SELECT name, repo, ref, dir, domain, live_image, every, env_json, created FROM slots"


def _raise_integrity(exc: sqlite3.IntegrityError) -> NoReturn:
    text = str(exc)
    if "slots_domain" in text or "slots.domain" in text:
        raise YardError(409, "domain_taken", "domain already in use") from exc
    if "UNIQUE" in text:
        raise YardError(409, "exists", "already exists") from exc
    raise exc


def ensure_admin(store: Store, directory: Path) -> str:
    if store.list_tokens():
        return ""
    plain = store.create_token("admin", "")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "admin.token"
    path.write_text(plain + "\n")
    path.chmod(0o600)
    return plain
