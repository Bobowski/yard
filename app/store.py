"""Yard's files. A project directory is the record. Git and Podman hold the rest."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.errors import YardError
from app.values import check_name, check_ref, check_slot_name, clean_dir, parse_duration, parse_trigger


def now() -> str:
    stamp = datetime.now(UTC)
    if stamp.microsecond == 0:
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    frac = f"{stamp.microsecond:06d}".rstrip("0")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + frac + "Z"


def token_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


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


class Store:
    """Projects and tokens on disk. The copy in memory follows the files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._slots: dict[str, Slot] = {}
        self._mtime: dict[str, int] = {}
        self.refresh()

    @classmethod
    def open(cls, root: Path) -> Store:
        (root / "projects").mkdir(parents=True, exist_ok=True)
        tokens = root / "tokens"
        tokens.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / "repos").mkdir(parents=True, exist_ok=True)
        return cls(root)

    def close(self) -> None:
        return

    def refresh(self) -> None:
        """Reload a project file whose time changed. A removed directory drops out."""
        base = self.root / "projects"
        if not base.is_dir():
            self._slots.clear()
            self._mtime.clear()
            return
        seen: set[str] = set()
        for path in base.iterdir():
            file = path / "project"
            if not path.is_dir() or not file.is_file():
                continue
            seen.add(path.name)
            mtime = file.stat().st_mtime_ns
            if self._mtime.get(path.name) == mtime and path.name in self._slots:
                continue
            self._slots[path.name] = _read_project(path.name, file.read_text())
            self._mtime[path.name] = mtime
        for name in list(self._slots):
            if name not in seen:
                self._slots.pop(name, None)
                self._mtime.pop(name, None)

    def list_repos(self) -> list[Repo]:
        base = self.root / "repos"
        if not base.is_dir():
            return []
        found = [self._repo(path) for path in base.iterdir() if path.is_dir() and path.name.endswith(".git")]
        return sorted(found, key=lambda repo: repo.name)

    def repo(self, name: str) -> Repo:
        path = self.root / "repos" / f"{name}.git"
        if not path.is_dir():
            raise YardError(
                404,
                "not_found",
                "repo does not exist",
                context={"name": name},
                help_text="Run yard repo ls to see the repos on this host.",
            )
        return self._repo(path)

    def create_repo(self, name: str) -> None:
        """Mark a bare repo directory. git init is the real create."""
        check_name(name)
        path = self.root / "repos" / f"{name}.git"
        path.mkdir(parents=True, exist_ok=True)

    def delete_repo(self, name: str) -> None:
        if self.slots_for_repo(name):
            raise YardError(
                409,
                "repo_in_use",
                "repo still has projects",
                context={"name": name},
                help_text="Remove each project that uses this repo, then delete the repo.",
                example="yard project rm heldso",
            )
        path = self.root / "repos" / f"{name}.git"
        if not path.is_dir():
            raise YardError(
                404,
                "not_found",
                "repo does not exist",
                context={"name": name},
                help_text="Run yard repo ls to see the repos on this host.",
            )

    def list_slots(self) -> list[Slot]:
        self.refresh()
        return [self._slots[name] for name in sorted(self._slots)]

    def slot(self, name: str) -> Slot:
        self.refresh()
        found = self._slots.get(name)
        if found is None:
            raise YardError(
                404,
                "not_found",
                "project does not exist",
                context={"name": name},
                help_text="Run yard project ls to see the apps on this host.",
            )
        return found

    def slots_for_repo(self, repo: str) -> list[Slot]:
        return [slot for slot in self.list_slots() if slot.repo == repo]

    def create_slot(
        self,
        name: str,
        repo: str,
        ref: str = "",
        directory: str = "",
        trigger: str = "",
    ) -> Slot:
        check_slot_name(name)
        check_name(repo)
        ref = ref or "main"
        check_ref(ref)
        directory = clean_dir(directory)
        self.repo(repo)
        if name in self._slots or (self.root / "projects" / name / "project").is_file():
            raise YardError(409, "exists", "already exists", help_text="Pick another name.")
        trigger = parse_trigger(trigger)
        slot = Slot(name, repo, ref, directory, trigger, now())
        self._write(slot)
        return slot

    def update_slot(
        self,
        name: str,
        *,
        ref: str | None = None,
        dir: str | None = None,
        trigger: str | None = None,
    ) -> Slot:
        slot = self.slot(name)
        if ref is not None:
            ref = ref or "main"
            check_ref(ref)
            slot.ref = ref
        if dir is not None:
            slot.directory = clean_dir(dir)
        if trigger is not None:
            slot.trigger = parse_trigger(trigger)
        self._write(slot)
        return self.slot(name)

    def delete_slot(self, name: str) -> None:
        if name not in self._slots and not (self.root / "projects" / name / "project").is_file():
            raise YardError(
                404,
                "not_found",
                "project does not exist",
                context={"name": name},
                help_text="Run yard project ls to see the apps on this host.",
            )
        file = self.root / "projects" / name / "project"
        file.unlink(missing_ok=True)
        self._slots.pop(name, None)
        self._mtime.pop(name, None)

    def create_token(self, name: str) -> str:
        check_name(name)
        path = self.root / "tokens" / name
        if path.exists():
            raise YardError(409, "exists", "already exists", help_text="Pick another name.")
        plain = secrets.token_hex(24)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(f"{now()}\n{token_hash(plain)}\n")
        path.chmod(0o600)
        return plain

    def list_tokens(self) -> list[Token]:
        base = self.root / "tokens"
        if not base.is_dir():
            return []
        found: list[Token] = []
        for path in sorted(base.iterdir(), key=lambda item: item.name):
            token = self._token(path)
            if token is not None:
                found.append(token)
        return found

    def delete_token(self, name: str, actor: str = "") -> None:
        if name == actor:
            raise YardError(
                403,
                "forbidden",
                "you cannot delete your own token",
                context={"name": name},
                help_text="Ask another token to remove it.",
            )
        path = self.root / "tokens" / name
        if not path.is_file():
            raise YardError(
                404,
                "not_found",
                "unknown token",
                context={"name": name},
                help_text="Run yard token ls.",
            )
        path.unlink()

    def token_ok(self, plain: str) -> Token | None:
        if not plain:
            return None
        digest = token_hash(plain)
        base = self.root / "tokens"
        if not base.is_dir():
            return None
        for path in base.iterdir():
            token = self._token(path)
            if token is None:
                continue
            lines = path.read_text().splitlines()
            if len(lines) >= 2 and lines[1] == digest:
                return token
        return None

    def _write(self, slot: Slot) -> None:
        directory = self.root / "projects" / slot.name
        directory.mkdir(parents=True, exist_ok=True)
        text = (
            f"repo {slot.repo}\nref {slot.ref}\ndir {slot.directory}\ntrigger {slot.trigger}\ncreated {slot.created}\n"
        )
        path = directory / "project"
        temp = directory / "project.tmp"
        temp.write_text(text)
        temp.replace(path)
        self._slots[slot.name] = slot
        self._mtime[slot.name] = path.stat().st_mtime_ns

    def _repo(self, path: Path) -> Repo:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return Repo(path.name.removesuffix(".git"), stamp)

    def _token(self, path: Path) -> Token | None:
        if not path.is_file() or path.suffix == ".token" or not check_name_ok(path.name):
            return None
        lines = path.read_text().splitlines()
        if len(lines) < 2:
            return None
        return Token(path.name, lines[0])


def check_name_ok(name: str) -> bool:
    try:
        check_name(name)
    except YardError:
        return False
    return True


def _read_project(name: str, text: str) -> Slot:
    fields = {"repo": "", "ref": "main", "dir": "", "trigger": "", "created": ""}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in fields:
            fields[key] = value.strip()
    return Slot(name, fields["repo"], fields["ref"] or "main", fields["dir"], fields["trigger"], fields["created"])


def ensure_admin(store: Store) -> str:
    if any(token.name == "admin" for token in store.list_tokens()):
        return ""
    plain = store.create_token("admin")
    path = store.root / "tokens" / "admin.token"
    path.write_text(plain + "\n")
    path.chmod(0o600)
    return plain
