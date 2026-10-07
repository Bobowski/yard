"""`yard` talks to a Yard API. A terminal prints text. Any other stdout prints JSON."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from app.config import YARD_IMAGE
from app.errors import YardError
from app.text import Block, ExpLabel, format_blocks, format_values, name_ok, parse_blocks, parse_env

USAGE = """yard commands:
  yard login --url <url> --token <token>
  yard repo new <name> [--force]
  yard repo list
  yard repo rm <name>
  yard repo clone <repo>
  yard repo link <repo>
  yard slot new <name> --repo <repo> [--ref main] [--dir path] [--domain host] [--every 5m]
    A domain is optional. The slot is removed when its branch is merged or deleted.
    --every 5m makes a job. One routine watches that slot, sleeps, and starts the next run.
  yard slot list [--running]
    The container name is the slot name. A slot with no container still gets a row. --running hides stopped rows.
    HEALTH is healthy, unhealthy, failed, down, or idle. A plain running container leaves HEALTH empty.
    STATUS is the Podman line. It says how long a container has been up, or when the last run exited.
    SHA is the revision the slot runs. WANT is the branch revision. BUILD is the latest build.
  yard slot show <name>
    Prints the slot as NAME=value lines. The API stores those lines as keys.
  yard slot edit <name>
    Opens that file in the editor. A pipe replaces the file from stdin. The save sends changed keys.
  yard slot edit <name> --set KEY VALUE [--set KEY=VALUE]
    Sets that key. An empty value removes the key.
  yard slot rm <name>
  yard slot logs <name> [--container name] [--tail 200] [--follow] [--cursor value]
    A slot reads the newest container. --container reads one run of that slot.
  yard slot deploys <name> [<id>]
  yard slot inspect <container>
  yard exp new
  yard exp
  yard exp list [key=value]
  yard exp show [<id> ...] [key=value]
  yard exp edit [<id> ...] [key=value]
  yard token new --name <name>
  yard token list
  yard token revoke <name>
  yard settings [--domain host] [--email addr] [--self-repo name] [--self-ref branch] [--self-image]
    The settings domain is the public host for Yard. A slot domain is the public app.
    --self-repo uses a repo on this host. --self-image returns to ghcr.io/bobowski/yard:latest.
  yard rollback
  yard update

  A terminal prints text. Any other stdout prints JSON. --json and --text force one or the other.
  --url and --token override YARD_URL and YARD_TOKEN.
"""


@dataclass(slots=True)
class Opt:
    json: bool = False
    text: bool = False
    url: str = ""
    token: str = ""
    rest: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Client:
    url: str
    token: str


def main(argv: list[str] | None = None) -> None:
    opt = split_args(sys.argv[1:] if argv is None else argv)
    if not opt.rest:
        _usage()
    command, *rest = opt.rest
    if command == "exp":
        _exp(opt, rest)
    elif command == "login":
        _login(opt, rest)
    elif command == "repo":
        _repo(opt, rest)
    elif command == "slot":
        _slot(opt, rest)
    elif command == "token":
        _token(opt, rest)
    elif command == "settings":
        _settings(opt, rest)
    elif command == "rollback":
        _show(opt, _api(_load(opt), "POST", "/api/v1/yard", {"action": "rollback"}), "raw")
    elif command == "update":
        _update(opt)
    else:
        _usage()


def split_args(args: list[str]) -> Opt:
    opt = Opt()
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--json":
            opt.json = True
        elif arg == "--text":
            opt.text = True
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
    parser = argparse.ArgumentParser(prog="yard login")
    parser.add_argument("--url", default="")
    parser.add_argument("--token", default="")
    parsed = parser.parse_args(args)
    url = _http_url(parsed.url or opt.url)
    token = (parsed.token or opt.token).strip()
    if not url or not token:
        _die("need --url and --token")
    path = config_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"url": url, "token": token}, indent=2) + "\n")
    path.chmod(0o600)
    print(path)


def _repo(opt: Opt, args: list[str]) -> None:
    if not args:
        _die("repo needs a subcommand")
    client = _load(opt)
    if args[0] == "list":
        _show(opt, _api(client, "GET", "/api/v1/repos", None), "repos")
    elif args[0] == "new":
        if len(args) < 2:
            _die("repo new <name> [--force]")
        parser = argparse.ArgumentParser(prog="yard repo new")
        parser.add_argument("--force", action="store_true")
        parsed = parser.parse_args(args[2:])
        _show(opt, _api(client, "POST", "/api/v1/repos", {"name": args[1], "force": parsed.force}), "raw")
    elif args[0] == "rm":
        if len(args) < 2:
            _die("repo rm <name>")
        _show(opt, _api(client, "DELETE", "/api/v1/repos/" + args[1], None), "raw")
    elif args[0] == "clone":
        _clone(opt, args[1:])
    elif args[0] == "link":
        _link(opt, args[1:])
    else:
        _die("unknown repo command")


def _slot(opt: Opt, args: list[str]) -> None:
    if not args:
        _die("slot needs a subcommand")
    client = _load(opt)
    command = args[0]
    if command == "list":
        parser = argparse.ArgumentParser(prog="yard slot list")
        parser.add_argument("--running", action="store_true")
        parsed = parser.parse_args(args[1:])
        path = "/api/v1/slots" + ("?running=1" if parsed.running else "")
        _show(opt, _api(client, "GET", path, None), "rows")
    elif command == "show":
        if len(args) < 2:
            _die("slot show <name>")
        _slot_show(opt, client, args[1])
    elif command == "logs":
        _logs(opt, args[1:])
    elif command == "deploys":
        _deploys(opt, args[1:])
    elif command == "inspect":
        if len(args) != 2 or args[1].startswith("-"):
            _die("slot inspect <container>")
        print(_api(client, "GET", "/api/v1/containers/" + quote(args[1]), None))
    elif command == "edit":
        _slot_edit(opt, client, args[1:])
    elif command == "new":
        _slot_new(opt, client, args)
    elif command == "rm":
        if len(args) < 2:
            _die("slot rm <name>")
        _show(opt, _api(client, "DELETE", "/api/v1/slots/" + quote(args[1]), None), "raw")
    else:
        _die("unknown slot command")


def _slot_show(opt: Opt, client: Client, name: str) -> None:
    body = _api(client, "GET", "/api/v1/slots/" + quote(name), None)
    if as_json(opt):
        print(body)
        return
    print(format_values(_object(body)), end="")


def _slot_edit(opt: Opt, client: Client, args: list[str]) -> None:
    if not args:
        _die("slot edit <name> [--set KEY VALUE]")
    name = args[0]
    if len(args) == 1:
        before = _object(_api(client, "GET", "/api/v1/slots/" + quote(name), None))
        text = _edit_text(format_values(before)) if _stdin_tty() else _read_stdin()
        try:
            parsed = parse_env(text)
        except YardError as exc:
            _die(exc.message)
        _slot_put(opt, client, name, slot_patch(before, parsed))
        return
    try:
        sets = parse_slot_sets(args[1:])
    except ValueError as exc:
        _die(str(exc))
    _slot_put(opt, client, name, {key: value for key, value in sets})


def _slot_new(opt: Opt, client: Client, args: list[str]) -> None:
    if len(args) < 2:
        _die("slot new <name> --repo <repo> [--ref main] [--dir path] [--domain host] [--every 5m]")
    parser = argparse.ArgumentParser(prog="yard slot new")
    parser.add_argument("--repo", default="")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--domain", default="")
    parser.add_argument("--dir", default="")
    parser.add_argument("--every", default="")
    parsed = parser.parse_args(args[2:])
    if not parsed.repo:
        _die("slot new <name> --repo <repo> [--ref main] [--dir path] [--domain host] [--every 5m]")
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
        print(f"warning: {warning}", file=sys.stderr)
    if as_json(opt):
        print(body)
        return
    values = wrap.get("values")
    if not isinstance(values, dict):
        _die(body)
    print(format_values({str(key): str(value) for key, value in values.items()}), end="")


def _token(opt: Opt, args: list[str]) -> None:
    if not args:
        _die("token needs a subcommand")
    client = _load(opt)
    if args[0] == "list":
        _show(opt, _api(client, "GET", "/api/v1/tokens", None), "tokens")
    elif args[0] == "new":
        parser = argparse.ArgumentParser(prog="yard token new")
        parser.add_argument("--name", default="")
        parsed = parser.parse_args(args[1:])
        if not parsed.name:
            _die("token new --name <name>")
        _show(opt, _api(client, "POST", "/api/v1/tokens", {"name": parsed.name}), "raw")
    elif args[0] == "revoke":
        if len(args) < 2:
            _die("token revoke <name>")
        _show(opt, _api(client, "DELETE", "/api/v1/tokens/" + quote(args[1]), None), "raw")
    else:
        _die("unknown token command")


def _deploys(opt: Opt, args: list[str]) -> None:
    if not args or args[0].startswith("-"):
        _die("slot deploys <name> [<id>]")
    name = quote(args[0])
    client = _load(opt)
    if len(args) >= 2 and not args[1].startswith("-"):
        _show(opt, _api(client, "GET", f"/api/v1/slots/{name}/deploys/{quote(args[1])}", None), "deploy")
        return
    _show(opt, _api(client, "GET", f"/api/v1/slots/{name}/deploys", None), "deploys")


def _logs(opt: Opt, args: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="yard slot logs")
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--cursor", default="")
    parser.add_argument("--container", default="")
    parser.add_argument("name", nargs="?")
    if args and not args[0].startswith("-"):
        name = args[0]
        parsed = parser.parse_args(args[1:])
        if parsed.name:
            _die("slot logs <name>")
    else:
        parsed = parser.parse_args(args)
        name = parsed.name or ""
    if not name:
        _die("slot logs <name> [--container name]")
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
    parser = argparse.ArgumentParser(prog="yard settings")
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
        _die("repo clone <repo>")
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
        _die("repo link <repo>")
    client = _load(opt)
    remote = f"{client.url}/git/{args[0]}.git"
    _link_dir(".", remote, client.token)
    print("remote yard ->", remote)


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
        _die("unknown exp command")


def _exp_new(opt: Opt, args: list[str]) -> None:
    if args:
        _die("exp new")
    text = _edit_text("# \n\n") if _stdin_tty() else _read_stdin()
    try:
        blocks = parse_blocks(text)
    except YardError as exc:
        _die(exc.message)
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
            _die(exc.message)
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
        _die(exc.message)
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
        print(body)
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
        _print_exps(rows)
    elif kind == "repos":
        _table(["REPO", "CREATED"], [[row.get("name", ""), row.get("created", "")] for row in rows])
    elif kind == "rows":
        _print_rows(rows)
    elif kind == "deploys":
        _print_deploys(rows)
    elif kind == "deploy":
        _print_deploy(rows)
    elif kind == "tokens":
        _print_tokens(rows)
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


def _print_exps(rows: list) -> None:
    table = []
    for row in rows:
        labels = row.get("labels") or []
        parts = [f"{label.get('key')}={label.get('value')}" for label in labels if isinstance(label, dict)]
        table.append([str(row.get("id", "")), str(row.get("title", "")), ",".join(parts)])
    _table(["ID", "TITLE", "LABELS"], table)


def _print_rows(rows: list) -> None:
    table = []
    for row in rows:
        exit_code = "-" if not row.get("container") else str(row.get("exit_code", 0))
        table.append(
            [
                str(row.get("slot") or ""),
                str(row.get("container") or ""),
                str(row.get("health") or "-"),
                str(row.get("status") or "-"),
                exit_code,
                str(row.get("cpu") or "-"),
                str(row.get("mem") or "-"),
                _short(str(row.get("sha") or "-")),
                _short(str(row.get("expected") or "-")),
                str(row.get("build") or "-"),
            ]
        )
    _table(["SLOT", "CONTAINER", "HEALTH", "STATUS", "EXIT", "CPU", "MEM", "SHA", "WANT", "BUILD"], table)


def _print_deploys(rows: list) -> None:
    table = []
    for row in rows:
        table.append(
            [
                str(row.get("id", "")),
                str(row.get("slot") or ""),
                str(row.get("repo") or ""),
                str(row.get("status") or ""),
                _short(str(row.get("sha") or "")),
                _took(str(row.get("started") or ""), str(row.get("ended") or "")),
                str(row.get("started") or ""),
            ]
        )
    _table(["ID", "SLOT", "REPO", "STATUS", "SHA", "TOOK", "STARTED"], table)


def _print_deploy(row: dict) -> None:
    took = _took(str(row.get("started") or ""), str(row.get("ended") or ""))
    sha = _short(str(row.get("sha") or ""))
    print(f"deploy {row.get('id')}  slot {row.get('slot')}  {sha}  {row.get('status')}  took {took}")
    log_text = str(row.get("log") or "").strip()
    if log_text:
        print(f"\n{log_text}")


def _print_tokens(rows: list) -> None:
    table = []
    for row in rows:
        table.append([str(row.get("name") or ""), str(row.get("parent") or ""), "yes" if row.get("revoked") else ""])
    _table(["NAME", "PARENT", "REVOKED"], table)


def _object(body: str) -> dict[str, str]:
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        _die(body)
    if not isinstance(loaded, dict):
        _die(body)
    return {str(key): str(value) for key, value in loaded.items()}


def _table(headers: list[str], rows: list[list[str]]) -> None:
    width = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(width):
                width[index] = max(width[index], len(cell))

    def line(cells: list[str]) -> None:
        print("  ".join(cell.ljust(width[index]) for index, cell in enumerate(cells)))

    line(headers)
    if not rows:
        print("(none)")
        return
    for row in rows:
        line(row)


def _short(sha: str) -> str:
    return sha[:12]


def _took(started: str, ended: str) -> str:
    start = _time(started)
    stop = _time(ended)
    if start is None or stop is None or stop <= start:
        return "-"
    return _duration(stop - start)


def _time(text: str) -> datetime | None:
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _duration(delta: timedelta) -> str:
    ms = int(round(delta.total_seconds() * 1000))
    if ms < 1000:
        return f"{ms}ms"
    seconds, frac = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    if frac:
        return f"{seconds}.{frac:03d}".rstrip("0") + "s"
    return f"{seconds}s"


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


def _update(opt: Opt) -> None:
    client = _load(opt)
    if _settings_image(client):
        print("pulling the image from the registry", file=sys.stderr)
    else:
        print("building the image from the repo this Yard holds", file=sys.stderr)
    try:
        status, body = _api_try(client, "POST", "/api/v1/yard", {"action": "update"})
    except OSError as exc:
        print(f"the connection closed ({exc}). Checking the swap.", file=sys.stderr)
        status, body = 0, ""
    else:
        if status >= 300:
            _die(body)
        print(body)
        print("swap started. Waiting until Yard answers.", file=sys.stderr)
    _wait_swap(client, 90)


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
    if last in {"building", "pulling"}:
        _die("Yard is still on the previous process. The update stopped before the swap. Run yard update again.")
    if last == "swapping":
        _die("The swap started but Yard did not report ready. Check the yard-swap container logs.")
    _die(f"Yard did not confirm the swap (last status {last}). Run yard slot list.")


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
        _die(text)
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
        _die(exc.read().decode().strip())
    with response:
        if response.status >= 300:
            _die(response.read().decode().strip())
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


def _usage() -> NoReturn:
    print(USAGE, file=sys.stderr, end="")
    raise SystemExit(2)


def _die(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)
