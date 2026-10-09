"""Git commands that are safe in a bare repo.

fetch updates origin. The reference hook deploys a slot that follows that tip.
"""

import re
from dataclasses import dataclass
from typing import NoReturn

from app.errors import YardError
from app.store import check_origin, check_ref

_COMMANDS = (
    "log",
    "show",
    "rev-parse",
    "ls-tree",
    "cat-file",
    "for-each-ref",
    "branch",
    "remote",
    "fetch",
    "gc",
)
_FETCH = ["fetch", "--depth", "1", "origin", "+refs/heads/*:refs/remotes/origin/*"]
_LOG_FLAGS = {
    "--oneline",
    "--decorate",
    "--graph",
    "--all",
    "--branches",
    "--remotes",
    "--tags",
    "--stat",
    "--name-only",
    "--name-status",
    "--abbrev-commit",
}
_PARSE_FLAGS = {"--verify", "--short", "--abbrev-ref", "--symbolic-full-name", "--quiet"}
_TREE_FLAGS = {"-r", "-d", "-l", "-t", "--name-only", "--long"}
_OBJECT_FLAGS = {"-t", "-s", "-p", "-e"}
_BRANCH_FLAGS = {"-v", "-vv", "-a", "-r", "--list"}
_REV = re.compile(r"[A-Za-z0-9._/@^{}~^+-]+")
_PATH = re.compile(r"[A-Za-z0-9._-]+")
_FORMAT = re.compile(r"[%()A-Za-z0-9: _.-]+")
_COUNT = re.compile(r"\d{1,3}")


@dataclass(frozen=True, slots=True)
class GitCall:
    argv: list[str]
    delete_branch: str = ""


def prepare_git(args: list[str]) -> GitCall:
    if not args or len(args) > 16 or any(_bad_token(arg) for arg in args):
        _unknown()
    command, *rest = args
    if command not in _COMMANDS:
        _unknown()
    if command in {"log", "show"}:
        _walk_log(rest)
        return GitCall(args)
    if command == "rev-parse":
        _walk_parse(rest)
        return GitCall(args)
    if command == "ls-tree":
        _walk_tree(rest)
        return GitCall(args)
    if command == "cat-file":
        _walk_object(rest)
        return GitCall(args)
    if command == "for-each-ref":
        _walk_refs(rest)
        return GitCall(args)
    if command == "branch":
        return _branch(rest)
    if command == "remote":
        return _remote(rest)
    if command == "fetch":
        return _fetch(rest)
    return _gc(rest)


def _bad_token(arg: str) -> bool:
    return not arg or any(ch in arg for ch in "\n\r\x00")


def _unknown() -> NoReturn:
    names = ", ".join(_COMMANDS)
    raise YardError(
        400,
        "bad_git",
        "that git command is not available on a bare repo",
        help_text=f"The commands are {names}.",
        example="yard git heldso log -1",
    )


def _bad_args(example: str) -> NoReturn:
    raise YardError(
        400,
        "bad_git",
        "that git command does not take those arguments",
        help_text="Use the arguments from the example.",
        example=example,
    )


def _rev(token: str) -> bool:
    if token.startswith(("-", "/")) or "\\" in token:
        return False
    if any(part == ".." for part in token.split("/")):
        return False
    return _REV.fullmatch(token) is not None


def _count(token: str) -> bool:
    return _COUNT.fullmatch(token) is not None and int(token) <= 100


def _walk_log(rest: list[str]) -> None:
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in _LOG_FLAGS or re.fullmatch(r"-\d{1,3}", token):
            if token.startswith("-") and token[1:].isdigit() and int(token[1:]) > 100:
                _bad_args("yard git heldso log -1")
            index += 1
            continue
        if token in {"-n", "--max-count"}:
            index += 1
            if index >= len(rest) or not _count(rest[index]):
                _bad_args("yard git heldso log -1")
            index += 1
            continue
        if token.startswith("--max-count=") and _count(token.split("=", 1)[1]):
            index += 1
            continue
        if _rev(token):
            index += 1
            continue
        _bad_args("yard git heldso log -1")


def _walk_parse(rest: list[str]) -> None:
    found = False
    for token in rest:
        if token in _PARSE_FLAGS or re.fullmatch(r"--short=\d{1,2}", token):
            continue
        if not _rev(token):
            _bad_args("yard git heldso rev-parse HEAD")
        found = True
    if not found:
        _bad_args("yard git heldso rev-parse HEAD")


def _walk_tree(rest: list[str]) -> None:
    revs = [token for token in rest if token not in _TREE_FLAGS]
    if not revs or not _rev(revs[0]):
        _bad_args("yard git heldso ls-tree HEAD")
    for token in revs[1:]:
        parts = token.split("/")
        if token.startswith("/") or any(not _PATH.fullmatch(part) for part in parts):
            _bad_args("yard git heldso ls-tree HEAD")


def _walk_object(rest: list[str]) -> None:
    if len(rest) != 2 or rest[0] not in _OBJECT_FLAGS or not _rev(rest[1]):
        _bad_args("yard git heldso cat-file -t HEAD")


def _walk_refs(rest: list[str]) -> None:
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--count":
            index += 1
            if index >= len(rest) or not _count(rest[index]):
                _bad_args("yard git heldso for-each-ref refs/heads")
            index += 1
            continue
        if token.startswith("--count=") and _count(token.split("=", 1)[1]):
            index += 1
            continue
        if token.startswith("--format=") and _FORMAT.fullmatch(token.split("=", 1)[1] or ""):
            index += 1
            continue
        if token.startswith("refs/") and _rev(token):
            index += 1
            continue
        _bad_args("yard git heldso for-each-ref refs/heads")


def _branch(rest: list[str]) -> GitCall:
    if not rest:
        return GitCall(["branch"])
    if rest[0] in {"-d", "-D"}:
        if len(rest) != 2:
            _bad_args("yard git heldso branch -D feature")
        check_ref(rest[1])
        return GitCall(["branch", rest[0], rest[1]], delete_branch=rest[1])
    if any(token not in _BRANCH_FLAGS for token in rest):
        _bad_args("yard git heldso branch -a")
    return GitCall(["branch", *rest])


def _remote(rest: list[str]) -> GitCall:
    if not rest or rest == ["-v"]:
        return GitCall(["remote", *rest])
    if len(rest) == 3 and rest[0] == "set-url" and rest[1] == "origin":
        return GitCall(["remote", "set-url", "origin", check_origin(rest[2])])
    _bad_args("yard git heldso remote set-url origin https://github.com/org/heldso")


def _fetch(rest: list[str]) -> GitCall:
    if rest not in ([], ["origin"]):
        _bad_args("yard git heldso fetch")
    return GitCall(list(_FETCH))


def _gc(rest: list[str]) -> GitCall:
    if not rest or rest == ["--prune=now"]:
        return GitCall(["gc", *rest])
    _bad_args("yard git heldso gc")
