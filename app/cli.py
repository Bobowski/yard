"""`yard` talks to a Yard API. A terminal prints text. Any other stdout prints JSON."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, TypedDict
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from app import package_version
from app.config import YARD_IMAGE
from app.errors import YardError, explain
from app.rows import format_span, mem_used, parse_time, slot_label
from app.store import check_domain, check_name, check_ref, check_slot_name, clean_dir, parse_every
from app.text import Block, ExpLabel, format_blocks, format_values, name_ok, parse_blocks, parse_env

_SUMMARY = "Push a repo. Yard builds the image and serves the app."
_HELP_FLAGS = {"-h", "--help"}


@dataclass(frozen=True, slots=True)
class _Help:
    use: str
    summary: str
    flags: dict[str, str] = field(default_factory=dict)


class _Group(TypedDict):
    summary: str
    commands: dict[str, _Help]


def _h(use: str, summary: str, flags: dict[str, str] | None = None) -> _Help:
    return _Help(use, summary, flags or {})


_ROOT: dict[str, dict[str, _Help]] = {
    "start": {
        "login": _h(
            "login --url <url> --token <token>",
            "Save the host and the token on this computer",
            {"--url <url>": "Yard host", "--token <token>": "Yard token"},
        ),
        "init": _h(
            "init --domain <host>",
            "Create the repo and the slot. One of --domain or --every is required.",
            {
                "--domain <host>": "Public host for the app",
                "--every <interval>": "Run the slot on a timer, for example 5m",
                "--name <name>": "Repo name and slot name",
                "--repo <name>": "Repo name",
                "--slot <name>": "Slot name",
                "--ref <branch>": "Branch. The default is main",
                "--dir <path>": "Directory inside the repo",
            },
        ),
    },
    "daily": {
        "slot": _h("slot <command>", "Show, log, and edit apps"),
        "update": _h("update", "Pull the image and replace this Yard after the new one answers"),
        "rollback": _h("rollback", "Start the previous image and wait until it answers"),
    },
    "more": {
        "repo": _h("repo <command>", "Create, clone, and link repos"),
        "token": _h("token <command>", "Create and revoke tokens"),
        "exp": _h("exp <command>", "Keep experiment notes"),
        "settings": _h(
            "settings",
            "Show or set the public host, the email, and the image",
            {
                "--domain <host>": "Public host for Yard",
                "--email <addr>": "Address for the certificate",
                "--self-repo <name>": "Build Yard from a repo on this host",
                "--self-ref <branch>": "Branch for that repo",
                "--self-image": "Return to ghcr.io/bobowski/yard:latest",
            },
        ),
    },
}

_GROUPS: dict[str, _Group] = {
    "repo": {
        "summary": "Create, clone, and link repos.",
        "commands": {
            "list": _h("list", "List repos"),
            "new": _h("new <name>", "Create a repo", {"--force": "Replace a leftover directory"}),
            "rm": _h("rm <name>", "Remove a repo and its files"),
            "clone": _h("clone <repo>", "Clone a repo into a new directory"),
            "link": _h("link <repo>", "Add the yard remote to this directory"),
        },
    },
    "slot": {
        "summary": "Show, log, and edit apps.",
        "commands": {
            "list": _h(
                "list",
                "Show each app, how long it has been up, and the revision",
                {"--running": "Hide stopped apps"},
            ),
            "logs": _h(
                "logs <name>",
                "Read the container log",
                {
                    "--tail <n>": "Lines to read. The default is 200",
                    "--follow": "Keep reading",
                    "--container <name>": "Read one run",
                    "--cursor <value>": "Continue from an earlier page",
                },
            ),
            "new": _h(
                "new <name> --repo <repo>",
                "Create a slot for one repo and one branch",
                {
                    "--repo <repo>": "Repo name",
                    "--ref <branch>": "Branch. The default is main",
                    "--domain <host>": "Public host for the app",
                    "--every <interval>": "Run the slot on a timer, for example 5m",
                    "--dir <path>": "Directory inside the repo",
                },
            ),
            "show": _h("show <name>", "Print the slot as NAME=value lines"),
            "edit": _h(
                "edit <name>",
                "Change the slot file, or set one key",
                {"--set KEY VALUE": "Set one key. An empty value removes it"},
            ),
            "rm": _h("rm <name>", "Remove a slot"),
            "deploys": _h("deploys <name> [<id>]", "Show builds for one slot. An id shows one build and its log"),
            "inspect": _h("inspect <container>", "Show the state and the image of one container"),
        },
    },
    "token": {
        "summary": "Create and revoke tokens.",
        "commands": {
            "list": _h("list", "List tokens"),
            "new": _h("new --name <name>", "Create a token", {"--name <name>": "Name of the token"}),
            "revoke": _h("revoke <name>", "Revoke a token"),
        },
    },
    "exp": {
        "summary": "Keep experiment notes.",
        "commands": {
            "list": _h("list [key=value]", "List experiments. A filter is key=value"),
            "new": _h("new", "Write an experiment"),
            "show": _h("show [<id>] [key=value]", "Print experiments"),
            "edit": _h("edit [<id>] [key=value]", "Change experiments"),
        },
    },
}

_GLOBAL = {
    "--json": "Print JSON",
    "--text": "Print text",
    "--no-color": "Turn color off",
    "--url <url>": "Yard host for this command",
    "--token <token>": "Yard token for this command",
}


@dataclass(slots=True)
class Opt:
    json: bool = False
    text: bool = False
    no_color: bool = False
    url: str = ""
    token: str = ""
    rest: list[str] = field(default_factory=list)


_no_color = False
_json = False


@dataclass(slots=True)
class Client:
    url: str
    token: str


def main(argv: list[str] | None = None) -> None:
    global _no_color, _json
    opt = split_args(sys.argv[1:] if argv is None else argv)
    _no_color = opt.no_color
    _json = as_json(opt)
    if opt.rest in (["--version"], ["-V"], ["version"]):
        print(package_version())
        return
    if opt.rest[:1] in (["--help"], ["-h"], ["help"]):
        _route_help(opt.rest[1:])
    if not opt.rest:
        _emit(_root_text(full=False), error=False)
    command, *rest = opt.rest
    if command in {"login", "init", "update", "rollback", "settings"} and _asked(rest):
        _show_cmd(command)
    if command == "exp":
        _exp(opt, rest)
    elif command == "login":
        _login(opt, rest)
    elif command == "init":
        _init(opt, rest)
    elif command == "repo":
        _repo(opt, rest)
    elif command == "slot":
        _slot(opt, rest)
    elif command == "token":
        _token(opt, rest)
    elif command == "settings":
        _settings(opt, rest)
    elif command == "rollback":
        _update(opt, "rollback")
    elif command == "update":
        _update(opt)
    else:
        _emit(_root_text(full=False), error=True)


def split_args(args: list[str]) -> Opt:
    opt = Opt()
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--json":
            opt.json = True
        elif arg == "--text":
            opt.text = True
        elif arg == "--no-color":
            opt.no_color = True
        elif arg == "--url" and index + 1 < len(args):
            index += 1
            opt.url = args[index]
        elif arg.startswith("--url="):
            opt.url = arg.removeprefix("--url=")
        elif arg == "--token" and index + 1 < len(args):
            index += 1
            opt.token = args[index]
        elif arg.startswith("--token="):
            opt.token = arg.removeprefix("--token=")
        else:
            opt.rest.append(arg)
        index += 1
    return opt


def config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return base / "yard" / "config.json"


def _load(opt: Opt) -> Client:
    raw: dict = {}
    path = config_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            raw = loaded

    # A flag replaces the environment, and the environment replaces the file.
    url = str(raw.get("url") or "")
    token = str(raw.get("token") or "")
    if os.environ.get("YARD_URL"):
        url = os.environ["YARD_URL"]
    if os.environ.get("YARD_TOKEN"):
        token = os.environ["YARD_TOKEN"]
    if opt.url:
        url = opt.url
    if opt.token:
        token = opt.token
    url = _http_url(url)
    if not url or not token:
        _die("run yard login, or set --url and --token")
    return Client(url, token)


def _http_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        return "https://" + url
    return url


def _login(opt: Opt, args: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False, prog="yard login")
    parser.add_argument("--url", default="")
    parser.add_argument("--token", default="")
    parsed = parser.parse_args(args)
    url = _http_url(parsed.url or opt.url)
    token = (parsed.token or opt.token).strip()
    if not url or not token:
        _show_cmd("login", error=True)

    # The token file stays private to this user.
    path = config_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"url": url, "token": token}, indent=2) + "\n")
    path.chmod(0o600)
    print(url)
    print(path)


def _init(opt: Opt, args: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False, prog="yard init")
    parser.add_argument("--name", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--slot", default="")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--domain", default="")
    parser.add_argument("--dir", default="")
    parser.add_argument("--every", default="")
    parsed = parser.parse_args(args)
    domain = parsed.domain.strip()
    every = parsed.every.strip()
    if not domain and not every:
        _show_cmd("init", error=True)
    try:
        ref = parsed.ref.strip()
        check_ref(ref)
        if domain:
            check_domain(domain)
        directory_key = clean_dir(parsed.dir) if parsed.dir.strip() else ""
        interval = parse_every(every) if every else ""
        if every and not interval:
            raise YardError(
                400,
                "bad_every",
                "yard init needs --domain or --every",
                help_text="A site needs a domain. A job needs an interval.",
                example="yard init --domain heldso.example.com",
            )
    except YardError as exc:
        _fail(exc)

    client = _load(opt)
    top = _git_top()
    folder = Path(top or Path.cwd()).name
    shared = parsed.name.strip()
    repo = _checked(parsed.repo.strip() or shared or folder, bool(parsed.repo.strip() or shared), check_name)
    slot_name = _checked(parsed.slot.strip() or shared or folder, bool(parsed.slot.strip() or shared), check_slot_name)
    directory = top or _make_git(ref)
    created = _ensure_repo(client, repo)
    remote = f"{client.url}/git/{repo}.git"
    _link_dir(directory, remote, client.token)

    # The remote is already in git. The slot is what the next push builds.
    body: dict[str, str] = {"SLOT_NAME": slot_name, "SLOT_REPO": repo, "SLOT_BRANCH": ref}
    if domain:
        body["SLOT_DOMAIN"] = domain
    if directory_key:
        body["SLOT_DIRECTORY"] = directory_key
    if interval:
        body["SLOT_INTERVAL"] = interval
    slot_text = _api(client, "POST", "/api/v1/slots", body)
    try:
        slot = json.loads(slot_text)
    except json.JSONDecodeError:
        slot = {"raw": slot_text}

    push = f"git push -u yard {ref}"
    if as_json(opt):
        print(
            json.dumps(
                {
                    "name": slot_name,
                    "repo": repo,
                    "remote": remote,
                    "created": created,
                    "slot": slot,
                    "push": push,
                },
            )
        )
        return
    print(f"{'repo'.ljust(6)}  {repo}")
    print(f"{'slot'.ljust(6)}  {slot_name}")
    print(f"{'remote'.ljust(6)}  {remote}")
    print()
    if not _head_ok(directory):
        print("commit, then")
    print(push)


def _checked(name: str, explicit: bool, check: Callable[[str], None]) -> str:
    try:
        check(name)
    except YardError as exc:
        if not explicit:
            exc.help_text = "Pass --name, --repo, or --slot."
        _fail(exc)
    return name


def _git_top() -> str:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if proc.returncode:
        return ""
    return proc.stdout.strip()


def _make_git(ref: str) -> str:
    _git([], ["init", "-b", ref])
    top = _git_top()
    if not top:
        _die("git init did not create a repo")
    return top


def _head_ok(directory: str) -> bool:
    proc = subprocess.run(["git", "-C", directory, "rev-parse", "--verify", "HEAD"], capture_output=True)
    return proc.returncode == 0


def _ensure_repo(client: Client, name: str) -> bool:
    status, text = _api_try(client, "POST", "/api/v1/repos", {"name": name, "force": False})
    if status < 300:
        return True
    if status == 409 and _error_code(text) == "exists":
        return False
    _die(text if _json else explain(text))


def _error_code(text: str) -> str:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or "")


def _repo(opt: Opt, args: list[str]) -> None:
    args = _take_help("repo", args)
    client = _load(opt)
    if args[0] == "list":
        _show(opt, _api(client, "GET", "/api/v1/repos", None), "repos")
    elif args[0] == "new":
        if len(args) < 2:
            _show_cmd("new", "repo", error=True)
        parser = argparse.ArgumentParser(add_help=False, prog="yard repo new")
        parser.add_argument("--force", action="store_true")
        parsed = parser.parse_args(args[2:])
        _show(opt, _api(client, "POST", "/api/v1/repos", {"name": args[1], "force": parsed.force}), "raw")
    elif args[0] == "rm":
        if len(args) < 2:
            _show_cmd("rm", "repo", error=True)
        _show(opt, _api(client, "DELETE", "/api/v1/repos/" + args[1], None), "raw")
    elif args[0] == "clone":
        _clone(opt, args[1:])
    elif args[0] == "link":
        _link(opt, args[1:])
    else:
        _emit(_group_text("repo", args[0]), error=True)


def _slot(opt: Opt, args: list[str]) -> None:
    args = _take_help("slot", args)
    client = _load(opt)
    command = args[0]
    if command == "list":
        parser = argparse.ArgumentParser(add_help=False, prog="yard slot list")
        parser.add_argument("--running", action="store_true")
        parsed = parser.parse_args(args[1:])
        path = "/api/v1/slots" + ("?running=1" if parsed.running else "")
        _show(opt, _api(client, "GET", path, None), "rows")
    elif command == "show":
        if len(args) < 2:
            _show_cmd("show", "slot", error=True)
        _slot_show(opt, client, args[1])
    elif command == "logs":
        _logs(opt, args[1:])
    elif command == "deploys":
        _deploys(opt, args[1:])
    elif command == "inspect":
        if len(args) != 2 or args[1].startswith("-"):
            _show_cmd("inspect", "slot", error=True)
        body = _api(client, "GET", "/api/v1/containers/" + quote(args[1]), None)
        if as_json(opt):
            print(body)
        else:
            print(format_inspect(body, color=_color_on()), end="")
    elif command == "edit":
        _slot_edit(opt, client, args[1:])
    elif command == "new":
        _slot_new(opt, client, args)
    elif command == "rm":
        if len(args) < 2:
            _show_cmd("rm", "slot", error=True)
        _show(opt, _api(client, "DELETE", "/api/v1/slots/" + quote(args[1]), None), "raw")
    else:
        _emit(_group_text("slot", command), error=True)


def _slot_show(opt: Opt, client: Client, name: str) -> None:
    body = _api(client, "GET", "/api/v1/slots/" + quote(name), None)
    if as_json(opt):
        print(body)
        return
    print(format_values(_object(body)), end="")


def _slot_edit(opt: Opt, client: Client, args: list[str]) -> None:
    if not args:
        _show_cmd("edit", "slot", error=True)
    name = args[0]
    if len(args) == 1:
        before = _object(_api(client, "GET", "/api/v1/slots/" + quote(name), None))
        text = _edit_text(format_values(before)) if _stdin_tty() else _read_stdin()
        try:
            parsed = parse_env(text)
        except YardError as exc:
            _fail(exc)
        _slot_put(opt, client, name, slot_patch(before, parsed))
        return

    try:
        sets = parse_slot_sets(args[1:])
    except ValueError as exc:
        _die(str(exc))
    _slot_put(opt, client, name, {key: value for key, value in sets})


def _slot_new(opt: Opt, client: Client, args: list[str]) -> None:
    if len(args) < 2:
        _show_cmd("new", "slot", error=True)
    parser = argparse.ArgumentParser(add_help=False, prog="yard slot new")
    parser.add_argument("--repo", default="")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--domain", default="")
    parser.add_argument("--dir", default="")
    parser.add_argument("--every", default="")
    parsed = parser.parse_args(args[2:])
    if not parsed.repo:
        _show_cmd("new", "slot", error=True)
    body = {"SLOT_NAME": args[1], "SLOT_REPO": parsed.repo, "SLOT_BRANCH": parsed.ref}
    if parsed.domain:
        body["SLOT_DOMAIN"] = parsed.domain
    if parsed.dir:
        body["SLOT_DIRECTORY"] = parsed.dir
    if parsed.every:
        body["SLOT_INTERVAL"] = parsed.every
    _show(opt, _api(client, "POST", "/api/v1/slots", body), "raw")


def parse_slot_sets(args: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    index = 0
    while index < len(args):
        if args[index] != "--set":
            raise ValueError("slot edit uses --set KEY VALUE")
        index += 1
        if index >= len(args):
            raise ValueError("slot edit --set needs a key")
        token = args[index]
        if "=" in token:
            key, value = token.split("=", 1)
        else:
            key = token
            value = ""
            if index + 1 < len(args) and args[index + 1] != "--set":
                index += 1
                value = args[index]

        if not name_ok(key):
            raise ValueError(f"bad key {key}")
        found.append((key, value))
        index += 1
    if not found:
        raise ValueError("slot edit --set needs a key")
    return found


def slot_patch(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    patch = {key: value for key, value in after.items() if before.get(key) != value}
    for key in before:
        if key not in after:
            patch[key] = ""
    return patch


def _slot_put(opt: Opt, client: Client, name: str, patch: dict[str, str]) -> None:
    body = _api(client, "PUT", "/api/v1/slots/" + quote(name), patch)
    try:
        wrap = json.loads(body)
    except json.JSONDecodeError:
        _die(body)
    if not isinstance(wrap, dict):
        _die(body)
    for warning in wrap.get("warnings") or []:
        text = f"warning: {warning}"
        if _color_on(sys.stderr):
            text = _ansi(text, "33")
        print(text, file=sys.stderr)

    if as_json(opt):
        print(body)
        return
    values = wrap.get("values")
    if not isinstance(values, dict):
        _die(body)
    print(format_values({str(key): str(value) for key, value in values.items()}), end="")


def _token(opt: Opt, args: list[str]) -> None:
    args = _take_help("token", args)
    client = _load(opt)
    if args[0] == "list":
        _show(opt, _api(client, "GET", "/api/v1/tokens", None), "tokens")
    elif args[0] == "new":
        parser = argparse.ArgumentParser(add_help=False, prog="yard token new")
        parser.add_argument("--name", default="")
        parsed = parser.parse_args(args[1:])
        if not parsed.name:
            _show_cmd("new", "token", error=True)
        _show(opt, _api(client, "POST", "/api/v1/tokens", {"name": parsed.name}), "raw")
    elif args[0] == "revoke":
        if len(args) < 2:
            _show_cmd("revoke", "token", error=True)
        _show(opt, _api(client, "DELETE", "/api/v1/tokens/" + quote(args[1]), None), "raw")
    else:
        _emit(_group_text("token", args[0]), error=True)


def _deploys(opt: Opt, args: list[str]) -> None:
    if not args or args[0].startswith("-"):
        _show_cmd("deploys", "slot", error=True)
    name = quote(args[0])
    client = _load(opt)
    if len(args) >= 2 and not args[1].startswith("-"):
        _show(opt, _api(client, "GET", f"/api/v1/slots/{name}/deploys/{quote(args[1])}", None), "deploy")
        return
    _show(opt, _api(client, "GET", f"/api/v1/slots/{name}/deploys", None), "deploys")


def _logs(opt: Opt, args: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False, prog="yard slot logs")
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--cursor", default="")
    parser.add_argument("--container", default="")
    parser.add_argument("name", nargs="?")
    if args and not args[0].startswith("-"):
        name = args[0]
        parsed = parser.parse_args(args[1:])
        if parsed.name:
            _show_cmd("logs", "slot", error=True)
    else:
        parsed = parser.parse_args(args)
        name = parsed.name or ""
    if not name:
        _show_cmd("logs", "slot", error=True)
    query = {"tail": str(parsed.tail)}
    if parsed.cursor:
        query["cursor"] = parsed.cursor
    if parsed.container:
        query["container"] = parsed.container
    path = "/api/v1/slots/" + quote(name) + "/logs?" + urlencode(query)
    client = _load(opt)
    if parsed.follow:
        query["follow"] = "1"
        _stream(client, "/api/v1/slots/" + quote(name) + "/logs?" + urlencode(query))
        return
    _show(opt, _api(client, "GET", path, None), "logs")


def _settings(opt: Opt, args: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False, prog="yard settings")
    parser.add_argument("--domain", default=argparse.SUPPRESS)
    parser.add_argument("--email", default=argparse.SUPPRESS)
    parser.add_argument("--self-repo", default=argparse.SUPPRESS)
    parser.add_argument("--self-ref", default=argparse.SUPPRESS)
    parser.add_argument("--self-image", action="store_true")
    parsed = vars(parser.parse_args(args))
    body = {}
    if "domain" in parsed:
        body["domain"] = parsed["domain"]
    if "email" in parsed:
        body["email"] = parsed["email"]
    if "self-repo" in parsed:
        body["self_repo"] = parsed["self-repo"]
    if "self-ref" in parsed:
        body["self_ref"] = parsed["self-ref"]
    if parsed.get("self-image"):
        body["self_image"] = YARD_IMAGE
    client = _load(opt)
    if not body:
        _show(opt, _api(client, "GET", "/api/v1/settings", None), "raw")
        return
    _show(opt, _api(client, "PUT", "/api/v1/settings", body), "raw")


def _clone(opt: Opt, args: list[str]) -> None:
    if not args:
        _show_cmd("clone", "repo", error=True)
    client = _load(opt)
    remote = f"{client.url}/git/{args[0]}.git"
    with tempfile.TemporaryDirectory(prefix="yard-cred") as tmp:
        cred = Path(tmp) / "cred"
        cred.write_text(_with_token(remote, client.token) + "\n")
        cred.chmod(0o600)
        _git([], ["-c", f"credential.helper=store --file {cred}", "clone", remote, args[0]])
    _git([args[0]], ["remote", "set-url", "origin", remote])
    _link_dir(args[0], remote, client.token)
    print(args[0])


def _link(opt: Opt, args: list[str]) -> None:
    if not args:
        _show_cmd("link", "repo", error=True)
    client = _load(opt)
    remote = f"{client.url}/git/{args[0]}.git"
    _link_dir(".", remote, client.token)
    print(f"{'remote'.ljust(6)}  {remote}")


def _link_dir(directory: str, remote: str, token: str) -> None:
    subprocess.run(["git", "-C", directory, "remote", "remove", "yard"], check=False, capture_output=True)
    _git([directory], ["remote", "add", "yard", remote])
    cred = Path(_git_path(directory, "yard-cred"))
    cred.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    cred.write_text(_with_token(remote, token) + "\n")
    cred.chmod(0o600)
    _git([directory], ["config", "--local", "credential.helper", f"store --file {cred}"])


def _git_path(directory: str, name: str) -> str:
    proc = subprocess.run(["git", "-C", directory, "rev-parse", "--git-path", name], capture_output=True, text=True)
    if proc.returncode:
        return str(Path(directory) / ".git" / name)
    path = proc.stdout.strip()
    if not Path(path).is_absolute():
        path = str(Path(directory) / path)
    return path


def _with_token(raw: str, token: str) -> str:
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"token:{quote(token, safe='')}@{host}", parts.path, parts.query, parts.fragment))


def _git(prefix: list[str], args: list[str]) -> None:
    command = ["git"]
    if prefix:
        command.extend(["-C", prefix[0]])
    command.extend(args)
    proc = subprocess.run(command)
    if proc.returncode:
        _die(f"git failed ({proc.returncode})")


def _exp(opt: Opt, args: list[str]) -> None:
    if args and args[0] in _HELP_FLAGS:
        _emit(_group_text("exp"), error=False)
    if args and args[0] in {"list", "new", "show", "edit"} and _asked(args[1:]):
        _show_cmd(args[0], "exp")
    if not args or args[0] == "list" or "=" in args[0]:
        rest = args[1:] if args and args[0] == "list" else args
        _exp_list(opt, rest)
        return
    if args[0] == "new":
        _exp_new(opt, args[1:])
    elif args[0] == "show":
        _exp_show(opt, args[1:])
    elif args[0] == "edit":
        _exp_edit(opt, args[1:])
    else:
        _emit(_group_text("exp", args[0]), error=True)


def _exp_new(opt: Opt, args: list[str]) -> None:
    if args:
        _die("exp new")
    text = _edit_text("# \n\n") if _stdin_tty() else _read_stdin()
    try:
        blocks = parse_blocks(text)
    except YardError as exc:
        _fail(exc)
    if not blocks:
        _die("a title line is required")
    client = _load(opt)
    _exp_print(opt, [_exp_send(client, "POST", "/api/v1/experiments", block) for block in blocks], [])


def _exp_list(opt: Opt, args: list[str]) -> None:
    ids, filters = _exp_pick(args)
    if ids:
        _die("exp list uses key=value")
    path = "/api/v1/experiments"
    if filters:
        path += "?" + urlencode(filters, doseq=True)
    _show(opt, _api(_load(opt), "GET", path, None), "exps")


def _exp_show(opt: Opt, args: list[str]) -> None:
    ids, filters = _exp_pick(args)
    rows = _exp_load(_load(opt), ids, filters)
    if as_json(opt):
        print(json.dumps(rows[0] if len(ids) == 1 and len(rows) == 1 else rows))
        return
    print(format_blocks(_blocks(rows)), end="")


def _exp_edit(opt: Opt, args: list[str]) -> None:
    ids, filters = _exp_pick(args)
    client = _load(opt)
    if not _stdin_tty() and not ids and not filters:
        try:
            blocks = parse_blocks(_read_stdin())
        except YardError as exc:
            _fail(exc)
        _exp_print(opt, [_exp_send(client, "POST", "/api/v1/experiments", block) for block in blocks], [])
        return
    session = _exp_load(client, ids, filters)
    initial = format_blocks(_blocks(session))
    if _stdin_tty():
        text = _edit_text(initial or "# \n\n")
    else:
        text = _read_stdin()
    try:
        blocks = parse_blocks(text)
    except YardError as exc:
        _fail(exc)
    present = {block.id for block in blocks if block.has_id}
    saved = []
    for block in blocks:
        if block.has_id:
            saved.append(_exp_send(client, "PUT", f"/api/v1/experiments/{block.id}", block))
        else:
            saved.append(_exp_send(client, "POST", "/api/v1/experiments", block))
    deleted = [int(row["id"]) for row in session if int(row["id"]) not in present]
    for exp_id in deleted:
        _api(client, "DELETE", f"/api/v1/experiments/{exp_id}", None)
    _exp_print(opt, saved, deleted)


def _exp_pick(args: list[str]) -> tuple[list[int], list[tuple[str, str]]]:
    filters: list[tuple[str, str]] = []
    ids: list[int] = []
    for arg in args:
        if arg.startswith("-"):
            _die("unknown exp flag")
        if "=" in arg:
            key, value = arg.split("=", 1)
            if not key or not value:
                _die("a filter is key=value")
            filters.append((key, value))
            continue
        if not arg.isdigit() or int(arg) <= 0:
            _die("pass an experiment id or key=value")
        ids.append(int(arg))
    return ids, filters


def _exp_load(client: Client, ids: list[int], filters: list[tuple[str, str]]) -> list[dict]:
    if not ids:
        path = "/api/v1/experiments"
        if filters:
            path += "?" + urlencode(filters, doseq=True)
        rows = json.loads(_api(client, "GET", path, None))
        if not isinstance(rows, list):
            _die("the experiment list is not a list")
        return rows
    rows = []
    for exp_id in ids:
        row = json.loads(_api(client, "GET", f"/api/v1/experiments/{exp_id}", None))
        if not isinstance(row, dict):
            _die("the experiment is not an object")
        if _exp_match(row, filters):
            rows.append(row)
    return rows


def _exp_match(row: dict, filters: list[tuple[str, str]]) -> bool:
    labels = row.get("labels") or []
    for key, value in filters:
        matched = any(
            isinstance(label, dict) and label.get("key") == key and label.get("value") == value for label in labels
        )
        if not matched:
            return False
    return True


def _exp_send(client: Client, method: str, path: str, block: Block) -> dict:
    body = _api(
        client,
        method,
        path,
        {
            "title": block.title,
            "body": block.body,
            "labels": [{"key": label.key, "value": label.value} for label in block.labels],
        },
    )
    row = json.loads(body)
    if not isinstance(row, dict):
        _die(body)
    return row


def _blocks(rows: list[dict]) -> list[Block]:
    blocks = []
    for row in rows:
        labels = [ExpLabel(str(label["key"]), str(label["value"])) for label in row.get("labels") or []]
        exp_id = int(row.get("id") or 0)
        blocks.append(Block(str(row.get("title") or ""), str(row.get("body") or ""), labels, exp_id, exp_id > 0))
    return blocks


def _exp_print(opt: Opt, rows: list[dict], deleted: list[int]) -> None:
    if deleted and not as_json(opt):
        print("deleted " + " ".join(str(exp_id) for exp_id in deleted), file=sys.stderr)
    if as_json(opt):
        print(json.dumps(rows))
        return
    print(format_blocks(_blocks(rows)), end="")


def _show(opt: Opt, body: str, kind: str) -> None:
    if as_json(opt):
        print(body)
        return
    if kind == "raw":
        text = format_record(body)
        if text:
            print(text, end="")
        return
    if kind == "logs":
        _show_logs(body)
        return
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return
    if rows is None:
        return
    if kind == "exps":
        print(format_exps(rows, color=_color_on()), end="")
    elif kind == "repos":
        print(format_repos(rows, color=_color_on()), end="")
    elif kind == "rows":
        _print_rows(rows)
    elif kind == "deploys":
        print(format_deploys(rows, color=_color_on()), end="")
    elif kind == "deploy":
        _print_deploy(rows)
    elif kind == "tokens":
        print(format_tokens(rows, color=_color_on()), end="")
    else:
        print(body)


def _show_logs(body: str) -> None:
    try:
        wrap = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return
    if not isinstance(wrap, dict):
        print(body)
        return
    if wrap.get("log"):
        print(wrap["log"])
    if wrap.get("cursor"):
        print(f"cursor: {wrap['cursor']}", file=sys.stderr)
    if wrap.get("more"):
        print("more: true", file=sys.stderr)


def format_repos(rows: list, *, color: bool = False) -> str:
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        body.append([str(row.get("name") or ""), _when(str(row.get("created") or ""))])
    return _render(["repo", "when"], body, {1}, color=color)


def format_tokens(rows: list, *, color: bool = False) -> str:
    body: list[list[str]] = []
    show_state = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = "revoked" if row.get("revoked") else ""
        show_state = show_state or bool(state)
        body.append([str(row.get("name") or ""), str(row.get("parent") or ""), state])
    headers = ["name", "parent"]
    indexes = [0, 1]
    if show_state:
        headers.append("state")
        indexes.append(2)
    return _render(
        headers,
        [[row[index] for index in indexes] for row in body],
        color=color,
        paint=_paint_revoked,
    )


def format_exps(rows: list, *, color: bool = False) -> str:
    body: list[list[str]] = []
    show_labels = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        labels = row.get("labels") or []
        parts = [f"{label.get('key')}={label.get('value')}" for label in labels if isinstance(label, dict)]
        text = ", ".join(parts)
        show_labels = show_labels or bool(text)
        body.append([str(row.get("id", "")), str(row.get("title", "")), text])
    headers = ["id", "title"]
    indexes = [0, 1]
    if show_labels:
        headers.append("labels")
        indexes.append(2)
    return _render(headers, [[row[index] for index in indexes] for row in body], {0}, color=color)


def format_inspect(body: str, *, color: bool = False) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body if body.endswith("\n") or not body else body + "\n"
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    table: list[list[str]] = []
    show_image = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        found = row.get("State")
        state = found if isinstance(found, dict) else {}
        image = str(row.get("ImageName") or "")
        if not image and isinstance(row.get("Config"), dict):
            image = str(row["Config"].get("Image") or "")
        show_image = show_image or bool(image)
        name = str(row.get("Name") or "").removeprefix("/") or str(row.get("Id") or "")[:12]
        table.append([name, _inspect_state(state), image])
    if not table:
        return body if body.endswith("\n") or not body else body + "\n"
    headers = ["name", "state"]
    indexes = [0, 1]
    if show_image:
        headers.append("image")
        indexes.append(2)
    return _render(
        headers,
        [[row[index] for index in indexes] for row in table],
        color=color,
        paint=_paint_state,
    )


def _inspect_state(state: dict) -> str:
    status = str(state.get("Status") or "")
    started = _when(str(state.get("StartedAt") or ""))
    code = state.get("ExitCode")
    if status == "running":
        return f"up {started}" if started else "up"
    if status == "exited":
        if code not in (0, None, "0"):
            return "failed"
        return f"down {started}" if started else "down"
    return status


def _paint_revoked(_index: int, text: str) -> str:
    if text.strip() != "revoked":
        return text
    return _ansi(text, "31")


_UNIT = {"second": "s", "minute": "m", "hour": "h", "day": "d", "week": "w"}


def format_slot_table(rows: list, *, color: bool = False) -> str:
    body: list[list[str]] = []
    host = ""
    show_data = False
    show_rev = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("slot") or "") == "total":
            host = _host_line(row)
            continue
        cells = _slot_cells(row)
        show_data = show_data or bool(cells[4])
        show_rev = show_rev or bool(cells[5])
        body.append(cells)

    # Data and the revision stay off the table until a row has them.
    headers = ["name", "state", "cpu", "mem"]
    indexes = [0, 1, 2, 3]
    if show_data:
        headers.append("data")
        indexes.append(4)
    if show_rev:
        headers.append("rev")
        indexes.append(5)
    right = {2, 3}
    if show_data:
        right.add(4)
    text = _render(
        headers,
        [[row[index] for index in indexes] for row in body],
        right,
        color=color,
        paint=_paint_state,
    )
    if host:
        return text + "\n" + host + "\n"
    return text


def _slot_cells(row: dict) -> list[str]:
    state = _slot_state(row)
    mark = _build_mark(str(row.get("build") or ""))
    if mark:
        state = f"{state} · {mark}" if state else mark
    mem = mem_used(str(row.get("mem") or ""))
    return [
        slot_label(str(row.get("slot") or ""), str(row.get("container") or "")),
        state,
        _measure(str(row.get("cpu") or "")),
        _measure(mem),
        _measure(str(row.get("data") or "")),
        _rev(str(row.get("sha") or ""), str(row.get("expected") or "")),
    ]


def _host_line(row: dict) -> str:
    data = _of(str(row.get("data") or ""))
    if data.startswith("0 "):
        data = ""
    parts = [_cpu_of(str(row.get("cpu") or "")), _of(str(row.get("mem") or "")), data]
    return " · ".join(part for part in parts if part)


def _slot_state(row: dict) -> str:
    health = str(row.get("health") or "")
    if health in {"unhealthy", "failed", "down", "idle", "starting"}:
        return health
    age = _age(str(row.get("status") or ""))
    if age:
        return age
    if health == "healthy" or str(row.get("state") or "") == "running":
        return "up"
    if str(row.get("state") or "") in {"exited", "dead", "stopped", "absent"}:
        return "down"
    return str(row.get("state") or "")


def _age(status: str) -> str:
    text = re.sub(r"\s+\([^)]*\)$", "", status.strip())
    about = re.fullmatch(r"(?i)up about an? (second|minute|hour|day|week)", text)
    if about:
        return f"up 1{_UNIT[about.group(1).lower()]}"
    up = re.fullmatch(r"(?i)up (?:about )?(\d+) (second|minute|hour|day|week)s?", text)
    if up:
        return f"up {int(up.group(1))}{_UNIT[up.group(2).lower()]}"
    gone = re.fullmatch(
        r"(?i)exited(?: \(\d+\))? (?:about )?(?:an? )?(\d+)? ?(second|minute|hour|day|week)?s? ago",
        text,
    )
    if not gone:
        return ""
    number = gone.group(1) or "1"
    unit = gone.group(2) or "minute"
    return f"down {int(number)}{_UNIT[unit.lower()]}"


def _build_mark(build: str) -> str:
    text = build.strip()
    if not text or text == "-" or text == "ok" or text.startswith("ok "):
        return ""
    return text


def _rev(sha: str, expected: str) -> str:
    short = sha[:7]
    want = expected[:7]
    if not short and not want:
        return ""
    if not want or short == want:
        return short
    if not short:
        return f"→ {want}"
    return f"{short} → {want}"


def _cpu_of(text: str) -> str:
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if len(parts) == 2:
        cores = _cores(parts[1]) if parts[1].endswith("%") else 0
        if cores:
            return f"{_measure(parts[0])} of {cores} cores"
    return _of(text)


def _of(text: str) -> str:
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if len(parts) == 2:
        return f"{_measure(parts[0])} of {_measure(parts[1])}"
    return _measure(parts[0]) if parts else ""


def _cores(percent: str) -> int:
    try:
        cores = float(percent[:-1]) / 100
    except ValueError:
        return 0
    if cores >= 1 and cores == int(cores):
        return int(cores)
    return 0


def _measure(text: str) -> str:
    percent = re.fullmatch(r"([0-9.]+)%", text.strip())
    if percent:
        return f"{percent.group(1)}%"
    match = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]+)", text.strip())
    if not match:
        return text.strip()
    try:
        amount = float(match.group(1))
    except ValueError:
        return text.strip()
    if amount == 0:
        return ""
    return f"{match.group(1)} {match.group(2)}"


def slot_hint(rows: list) -> str:
    for row in rows:
        if not isinstance(row, dict) or str(row.get("slot") or "") == "total":
            continue
        if _needs_logs(_slot_state(row)):
            return "yard slot logs " + slot_label(str(row.get("slot") or ""), str(row.get("container") or ""))
    return ""


def _print_rows(rows: list) -> None:
    print(format_slot_table(rows, color=_color_on()), end="")
    hint = slot_hint(rows)
    if hint and sys.stderr.isatty():
        if _color_on(sys.stderr):
            hint = _ansi(hint, "2")
        print(hint, file=sys.stderr)


def format_deploys(rows: list, *, color: bool = False) -> str:
    body: list[list[str]] = []
    show_took = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        took = format_span(str(row.get("started") or ""), str(row.get("ended") or ""))
        show_took = show_took or bool(took)
        body.append(
            [
                str(row.get("id", "")),
                str(row.get("status") or ""),
                str(row.get("sha") or "")[:7],
                took,
                _when(str(row.get("started") or "")),
            ]
        )

    headers = ["id", "status", "rev"]
    indexes = [0, 1, 2]
    if show_took:
        headers.append("took")
        indexes.append(3)
    headers.append("when")
    indexes.append(4)
    picked = [[row[index] for index in indexes] for row in body]
    right = {0, len(headers) - 1}
    if show_took:
        right.add(headers.index("took"))
    return _render(headers, picked, right, color=color, paint=_paint_deploy)


def _print_deploy(row: dict) -> None:
    took = format_span(str(row.get("started") or ""), str(row.get("ended") or ""))
    sha = str(row.get("sha") or "")[:7]
    status = str(row.get("status") or "")
    if _color_on():
        status = _ansi(status, _deploy_attention(status))
    parts = [f"deploy {row.get('id')}", str(row.get("slot") or ""), sha, status, _when(str(row.get("started") or ""))]
    if took:
        parts.append(took)
    print("  ".join(part for part in parts if part))
    log_text = str(row.get("log") or "").strip()
    if log_text:
        print(f"\n{log_text}")


def _object(body: str) -> dict[str, str]:
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        _die(body)
    if not isinstance(loaded, dict):
        _die(body)
    return {str(key): str(value) for key, value in loaded.items()}


def _render(
    headers: list[str],
    rows: list[list[str]],
    right: set[int] | None = None,
    *,
    color: bool = False,
    paint: Callable[[int, str], str] | None = None,
) -> str:
    numeric = right or set()
    width = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(width):
                width[index] = max(width[index], len(cell))

    def line(cells: list[str], *, header: bool = False) -> str:
        shown = []
        for index, cell in enumerate(cells):
            pad = width[index] if index < len(width) else 0
            text = cell.rjust(pad) if index in numeric else cell.ljust(pad)
            if color and not header and paint is not None:
                text = paint(index, text)
            shown.append(text)
        text = "  ".join(shown).rstrip()
        if color and header:
            return _ansi(text, "2")
        return text

    lines = [line(headers, header=True)]
    if not rows:
        lines.append("(none)")
    lines.extend(line(row) for row in rows)
    return "\n".join(lines) + "\n"


def _paint_state(index: int, text: str) -> str:
    if index != 1:
        return text
    return _ansi(text, _attention(text.strip())) or text


def _attention(state: str) -> str:
    head = state.split(" · ", 1)[0]
    if head == "failed" or head.startswith("down"):
        return "31"
    if head in {"unhealthy", "starting"}:
        return "33"
    return ""


def _needs_logs(state: str) -> bool:
    head = state.split(" · ", 1)[0]
    return head in {"failed", "unhealthy"} or head.startswith("down")


def _ansi(text: str, code: str) -> str:
    if not text or not code:
        return text
    return f"\033[{code}m{text}\033[0m"


def _color_on(stream: object | None = None) -> bool:
    if _no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def format_record(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body if body.endswith("\n") or not body else body + "\n"
    if not isinstance(data, dict):
        return body if body.endswith("\n") else body + "\n"
    if "token" in data:
        name = str(data.get("name") or "")
        secret = str(data.get("token") or "")
        return f"{name}\n{secret}\n" if name else f"{secret}\n"
    if "domain" in data and "self_image" in data:
        return format_settings(data)
    if "name" in data and set(data) <= {"name", "created"}:
        return f"{data['name']}\n"

    # The swap answer is for the wait, not for this screen.
    if "image" in data and "status" in data:
        return ""
    lines = [f"{key}  {value}" for key, value in data.items() if value not in ("", None, [], {})]
    return ("\n".join(lines) + "\n") if lines else ""


def format_settings(data: dict) -> str:
    repo = str(data.get("self_repo") or "")
    pairs = [("domain", str(data.get("domain") or "")), ("email", str(data.get("email") or ""))]
    if repo:
        pairs.append(("repo", repo))
        pairs.append(("ref", str(data.get("self_ref") or "main")))
    else:
        pairs.append(("image", str(data.get("self_image") or "") or YARD_IMAGE))
    width = max(len(key) for key, _value in pairs)
    return "".join(f"{key.ljust(width)}  {value}".rstrip() + "\n" for key, value in pairs)


def _when(stamp: str) -> str:
    moment = parse_time(stamp)
    if moment is None or moment.year < 2000:
        return ""
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 5:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    minutes, _seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, _minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h"
    days, _hours = divmod(hours, 24)
    return f"{days}d"


def _paint_deploy(index: int, text: str) -> str:
    if index != 1:
        return text
    return _ansi(text, _deploy_attention(text.strip()))


def _deploy_attention(status: str) -> str:
    if status in {"failed", "interrupted"}:
        return "31"
    if status == "running":
        return "33"
    return ""


def _asked(args: list[str]) -> bool:
    return any(arg in _HELP_FLAGS for arg in args)


def _lookup(group: str | None, key: str) -> _Help | None:
    if group is None:
        for rows in _ROOT.values():
            if key in rows:
                return rows[key]
        return None
    found = _GROUPS.get(group)
    if found is None:
        return None
    return found["commands"].get(key)


def _column(rows: list[tuple[str, str]]) -> str:
    width = max(len(name) for name, _summary in rows)
    return "\n".join(f"  {name.ljust(width)}  {summary}" for name, summary in rows)


def _root_text(*, full: bool) -> str:
    lines = [
        f"yard {package_version()}",
        _SUMMARY,
        "",
        "  yard init --domain <host>",
        "  git push -u yard main",
        "",
    ]
    for title, rows in _ROOT.items():
        lines.append(title)
        lines.append(_column([(name, cmd.summary) for name, cmd in rows.items()]))
        lines.append("")
    lines.append("yard <command> --help")
    if full:
        lines.extend(
            [
                "",
                "flags",
                _column(list(_GLOBAL.items())),
                "",
                "A terminal prints text. A pipe prints JSON.",
                "https://github.com/Bobowski/yard",
            ]
        )
    return "\n".join(lines) + "\n"


def _group_text(name: str, unknown: str = "") -> str:
    group = _GROUPS[name]
    lines: list[str] = []
    if unknown:
        lines.extend([f"unknown command: {unknown}", ""])
    lines.extend(
        [
            f"yard {name}",
            group["summary"],
            "",
            _column([(cmd.use, cmd.summary) for cmd in group["commands"].values()]),
            "",
            f"yard {name} <command> --help",
        ]
    )
    return "\n".join(lines) + "\n"


def _cmd_text(key: str, group: str | None) -> str:
    cmd = _lookup(group, key)
    if cmd is None:
        return _group_text(group) if group else _root_text(full=True)
    prefix = f"yard {group} " if group else "yard "
    lines = [prefix + cmd.use, "", cmd.summary]
    if cmd.flags:
        lines.extend(["", _column(list(cmd.flags.items()))])
    return "\n".join(lines) + "\n"


def _emit(text: str, *, error: bool) -> NoReturn:
    if error:
        print(text, file=sys.stderr, end="")
        raise SystemExit(2)
    print(text, end="")
    raise SystemExit(0)


def _show_cmd(key: str, group: str | None = None, *, error: bool = False) -> NoReturn:
    if group and _lookup(group, key) is None:
        _emit(_group_text(group, key), error=True)
    _emit(_cmd_text(key, group), error=error)


def _take_help(group: str, args: list[str]) -> list[str]:
    if not args or args[0] in _HELP_FLAGS:
        _emit(_group_text(group), error=False)
    verb, *rest = args
    if _asked(rest):
        _show_cmd(verb, group)
    if _lookup(group, verb) is None:
        _emit(_group_text(group, verb), error=True)
    return args


def _route_help(args: list[str]) -> NoReturn:
    rest = [arg for arg in args if arg not in _HELP_FLAGS]
    if not rest:
        _emit(_root_text(full=True), error=False)
    group = rest[0]
    if group in _GROUPS:
        if len(rest) == 1:
            _emit(_group_text(group), error=False)
        _show_cmd(rest[1], group)
    if _lookup(None, group):
        _show_cmd(group)
    _emit(_root_text(full=True), error=True)


def _settings_image(client: Client) -> bool:
    try:
        status, body = _api_try(client, "GET", "/api/v1/settings", None)
    except OSError:
        return True
    if status >= 300:
        return True
    try:
        settings = json.loads(body)
    except json.JSONDecodeError:
        return False
    return not settings.get("self_repo")


def _update(opt: Opt, action: str = "update") -> None:
    client = _load(opt)
    if action == "rollback":
        print("starting the previous image", file=sys.stderr)
    elif _settings_image(client):
        print("pulling the image from the registry", file=sys.stderr)
    else:
        print("building the image from the repo this Yard holds", file=sys.stderr)

    # The process can exit while the swap container takes its place.
    try:
        status, body = _api_try(client, "POST", "/api/v1/yard", {"action": action})
    except OSError as exc:
        print(f"the connection closed ({exc}). Checking the swap.", file=sys.stderr)
        status, body = 0, ""
    else:
        if status >= 300:
            _die(body if _json else explain(body))
        if as_json(opt):
            print(body)
        print("swap started. Waiting until Yard answers.", file=sys.stderr)
    _wait_swap(client, 180)


def _wait_swap(client: Client, limit: float) -> None:
    import time

    deadline = time.monotonic() + limit
    last = ""
    while time.monotonic() < deadline:
        health = _health_ok(client)
        try:
            status, body = _api_try(client, "GET", "/api/v1/yard", None)
        except OSError:
            status, body = 0, ""
        if status and status < 300:
            try:
                swap = json.loads(body)
            except json.JSONDecodeError:
                swap = {}
            current = str(swap.get("status") or "")
            if current and current != last:
                print(f"swap status: {current}", file=sys.stderr)
                last = current
            done, ok, message = judge_swap(current, health)
            if done:
                if ok:
                    print(f"Yard is up on {swap.get('image')}.")
                    return
                _die(message)
        time.sleep(1)

    # The last status says how far the swap got before this wait gave up.
    if last in {"building", "pulling"}:
        _die("Yard is still on the previous process. The update stopped before the swap. Run yard update again.")
    if last == "swapping":
        _die("The swap started but Yard did not report ready. Read /srv/yard/data/yard/swap.log on the host.")
    _die(f"Yard did not confirm the swap (last status {last}). Read /srv/yard/data/yard/swap.log on the host.")


def judge_swap(status: str, health_ok: bool) -> tuple[bool, bool, str]:
    if status == "ready":
        if not health_ok:
            return False, False, ""
        return True, True, ""
    if status == "rolled_back":
        return True, False, "The swap failed and Yard is running the previous image."
    if status == "failed":
        return True, False, "The update failed before the swap finished. Run yard update again."
    return False, False, ""


def _health_ok(client: Client) -> bool:
    request = urllib.request.Request(client.url + "/health")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()
            return response.status == 200
    except Exception:
        return False


def _api(client: Client, method: str, path: str, body: object) -> str:
    status, text = _api_try(client, method, path, body)
    if status >= 300:
        _die(text if _json else explain(text))
    return text


def _api_try(client: Client, method: str, path: str, body: object) -> tuple[int, str]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"authorization": f"Bearer {client.token}"}
    if data is not None:
        headers["content-type"] = "application/json"
    request = urllib.request.Request(client.url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode().strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode().strip()


def _stream(client: Client, path: str) -> None:
    request = urllib.request.Request(client.url + path, headers={"authorization": f"Bearer {client.token}"})
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode().strip()
        _die(raw if _json else explain(raw))
    with response:
        if response.status >= 300:
            raw = response.read().decode().strip()
            _die(raw if _json else explain(raw))
        shutil.copyfileobj(response, sys.stdout.buffer)


def as_json(opt: Opt) -> bool:
    if opt.json:
        return True
    if opt.text:
        return False
    return not sys.stdout.isatty()


def _stdin_tty() -> bool:
    return sys.stdin.isatty()


def _read_stdin() -> str:
    raw = sys.stdin.buffer.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        _die("the file is too large")
    return raw.decode()


def _edit_text(initial: str) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="yard-exp-", delete=False)
    path = Path(handle.name)
    try:
        path.write_text(initial)
        editor = os.environ.get("EDITOR") or "vi"
        fields = editor.split()
        proc = subprocess.run([*fields, str(path)])
        if proc.returncode:
            _die(f"editor: exit {proc.returncode}")
        return path.read_text()
    finally:
        path.unlink(missing_ok=True)


def _fail(exc: YardError) -> NoReturn:
    if _json:
        _die(json.dumps({"error": exc.payload()}))
    _die(str(exc))


def _die(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)
