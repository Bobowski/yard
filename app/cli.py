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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from app import package_version
from app.errors import YardError, explain
from app.render import (
    _deploy_attention,
    ansi,
    deploy_line,
    format_deploys,
    format_doctor,
    format_inspect,
    format_prune,
    format_record,
    format_repos,
    format_slot,
    format_slot_status,
    format_status,
    format_tokens,
    parse_slot,
    slot_edit_patch,
    status_hint,
)
from app.values import (
    check_domain,
    check_name,
    check_origin,
    check_ref,
    check_slot_name,
    clean_dir,
    origin_name,
    parse_trigger,
    public_origin,
)

_no_color = False
_json = False


@dataclass(slots=True)
class Client:
    url: str
    token: str


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON")
    common.add_argument("--text", action="store_true", help="print text")
    common.add_argument("--no-color", action="store_true", help="plain output")
    common.add_argument("--url", default="", help="the host address")
    common.add_argument("--token", default="", help="a token")

    parser = argparse.ArgumentParser(
        prog="yard",
        description="Push a repo. Yard builds the image and serves the app.",
        parents=[common],
    )
    parser.add_argument("--version", action="store_true", help="print the version")
    subs = parser.add_subparsers(dest="command", metavar="command")

    status = subs.add_parser("status", help="the slot table, or one slot", parents=[common])
    status.add_argument("name", nargs="?", default="")
    status.add_argument("--running", action="store_true", help="only running containers")
    status.set_defaults(func=_cmd_status)

    doctor = subs.add_parser("doctor", help="check the host", parents=[common])
    doctor.set_defaults(func=_cmd_doctor)

    prune = subs.add_parser("prune", help="remove what no slot needs", parents=[common])
    prune.add_argument("--days", type=int, default=7, help="keep what is newer than this")
    prune.add_argument("--dry-run", action="store_true", help="report only, remove nothing")
    prune.set_defaults(func=_cmd_prune)

    login = subs.add_parser("login", help="save a host and token", parents=[common])
    login.set_defaults(func=_cmd_login)

    init = subs.add_parser("init", help="connect this directory and create a slot", parents=[common])
    init.add_argument("--domain", default="", help="the public host name")
    init.add_argument("--trigger", default="", help="push, manual, or every 1h for a job slot")
    init.add_argument("--remote", default="", help="clone this remote on the host")
    init.add_argument("--name", default="", help="name for the repo and slot")
    init.add_argument("--repo", default="", help="the repo name")
    init.add_argument("--slot", default="", help="the slot name")
    init.add_argument("--ref", default="", help="the branch to follow")
    init.add_argument("--dir", default="", help="a subdirectory of the repo")
    init.set_defaults(func=_cmd_init)

    clone = subs.add_parser("clone", help="clone a repo from the host", parents=[common])
    clone.add_argument("name")
    clone.add_argument("--lazy", action="store_true", help="fetch blobs on demand")
    clone.add_argument("--full", action="store_true", help="the full history")
    clone.set_defaults(func=_cmd_clone)

    logs = subs.add_parser("logs", help="slot logs", parents=[common])
    logs.add_argument("--slot", default="")
    logs.add_argument("--role", default="")
    logs.add_argument("--service", default="")
    logs.add_argument("--tail", type=int, default=200)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--cursor", default="")
    logs.set_defaults(func=_cmd_logs)

    deploy = subs.add_parser("deploy", help="deploy a slot now", parents=[common])
    deploy.add_argument("name")
    deploy.add_argument("sha", nargs="?", default="")
    deploy.set_defaults(func=_cmd_deploy)

    run = subs.add_parser("run", help="run a slot now", parents=[common])
    run.add_argument("name")
    run.set_defaults(func=_cmd_run)

    deploys = subs.add_parser("deploys", help="deploys of a slot", parents=[common])
    deploys.add_argument("name")
    deploys.add_argument("deploy_id", nargs="?", default="")
    deploys.set_defaults(func=_cmd_deploys)

    git_cmd = subs.add_parser("git", help="run git on a host repo", parents=[common])
    git_cmd.add_argument("repo")
    git_cmd.add_argument("args", nargs=argparse.REMAINDER)
    git_cmd.set_defaults(func=_cmd_git)

    for name, help_text, build in (
        ("slot", "slots", _slot_parser),
        ("repo", "repos", _repo_parser),
        ("token", "tokens", _token_parser),
        ("podman", "containers", _podman_parser),
    ):
        group = subs.add_parser(name, help=help_text, parents=[common])
        verbs = group.add_subparsers(dest="verb", metavar="command", required=True)
        build(verbs, common)

    return parser


def _slot_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("list", help="the slot table", parents=[common])
    listed.add_argument("--running", action="store_true", help="only running containers")
    listed.set_defaults(func=_cmd_status)

    new = subs.add_parser("new", help="create a slot", parents=[common])
    new.add_argument("name")
    new.add_argument("--repo", default="", help="the repo to deploy")
    new.add_argument("--ref", default="main", help="the branch to follow")
    new.add_argument("--domain", default="", help="the public host name")
    new.add_argument("--dir", default="", help="a subdirectory of the repo")
    new.add_argument("--trigger", default="", help="push, manual, or every 1h for a job slot")
    new.set_defaults(func=_cmd_slot_new)

    show = subs.add_parser("show", help="the slot file", parents=[common])
    show.add_argument("name")
    show.set_defaults(func=_cmd_slot_show)

    edit = subs.add_parser("edit", help="edit the slot file", parents=[common])
    edit.add_argument("name")
    edit.add_argument("--set", dest="sets", action="append", nargs="+", metavar="KEY", help="set a field or env value")
    edit.set_defaults(func=_cmd_slot_edit)

    rm = subs.add_parser("rm", help="remove a slot", parents=[common])
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_slot_rm)


def _repo_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("list", help="the repos", parents=[common])
    listed.set_defaults(func=_cmd_repo_list)

    new = subs.add_parser("new", help="create a repo on the host", parents=[common])
    new.add_argument("name")
    new.add_argument("--remote", default="", help="clone this remote on the host")
    new.add_argument("--force", action="store_true", help="replace a leftover directory")
    new.set_defaults(func=_cmd_repo_new)

    rm = subs.add_parser("rm", help="remove a repo", parents=[common])
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_repo_rm)

    link = subs.add_parser("link", help="point this directory at a repo", parents=[common])
    link.add_argument("repo")
    link.set_defaults(func=_cmd_repo_link)


def _token_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("list", help="the tokens", parents=[common])
    listed.set_defaults(func=_cmd_token_list)

    new = subs.add_parser("new", help="create a token", parents=[common])
    new.add_argument("name")
    new.set_defaults(func=_cmd_token_new)

    rm = subs.add_parser("rm", help="remove a token", parents=[common])
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_token_rm)


def _podman_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    logs = subs.add_parser("logs", help="container logs", parents=[common])
    logs.add_argument("container")
    logs.add_argument("--tail", type=int, default=200)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--cursor", default="")
    logs.set_defaults(func=_cmd_podman_logs)

    inspect = subs.add_parser("inspect", help="inspect a container", parents=[common])
    inspect.add_argument("container")
    inspect.set_defaults(func=_cmd_podman_inspect)


def main(argv: list[str] | None = None) -> None:
    global _no_color, _json
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    _no_color = args.no_color
    _json = _as_json(args)
    if args.version:
        print(package_version())
        return
    if not hasattr(args, "func"):
        args.running = False
        _cmd_status(args)
        return
    args.func(args)


def _as_json(args: argparse.Namespace) -> bool:
    if args.json:
        return True
    if args.text:
        return False
    return not sys.stdout.isatty()


def config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return base / "yard" / "config.json"


def _load(args: argparse.Namespace) -> Client:
    raw: dict[str, Any] = {}
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
    if args.url:
        url = args.url
    if args.token:
        token = args.token
    url = _http_url(url)
    if not url or not token:
        _die("run yard login, or set --url and --token")
    return Client(url, token)


def _http_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        return "https://" + url
    return url


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


def _fail(exc: YardError) -> NoReturn:
    if _json:
        _die(json.dumps({"error": exc.payload()}))
    _die(str(exc))


def _die(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _warn(message: str) -> None:
    text = f"warning: {message}"
    if _color_on(sys.stderr):
        text = ansi(text, "33")
    print(text, file=sys.stderr)


def _object(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _die(raw)


# --- commands -------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> None:
    client = _load(args)
    name = getattr(args, "name", "") or ""
    if name:
        raw = _api(client, "GET", f"/api/v1/slots/{name}/status", None)
        report = _object(raw)
        if _json or not isinstance(report, dict):
            print(raw)
            return
        print(format_slot_status(report, datetime.now(UTC), color=_color_on()), end="")
        return
    path = "/api/v1/status" + ("?running=1" if getattr(args, "running", False) else "")
    raw = _api(client, "GET", path, None)
    report = _object(raw)
    if _json or not isinstance(report, dict):
        print(raw)
        return
    print(format_status(report, datetime.now(UTC), color=_color_on()), end="")
    hint = status_hint(report)
    if hint and sys.stderr.isatty():
        if _color_on(sys.stderr):
            hint = ansi(hint, "2")
        print(hint, file=sys.stderr)


def _cmd_doctor(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "GET", "/api/v1/doctor", None)
    checks = _object(raw)
    if _json or not isinstance(checks, list):
        print(raw)
        return
    print(format_doctor(checks, color=_color_on()), end="")
    if any(not check.get("ok") for check in checks if isinstance(check, dict)):
        raise SystemExit(1)


def _cmd_prune(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "POST", "/api/v1/prune", {"days": args.days, "dry": args.dry_run})
    found = _object(raw)
    if _json or not isinstance(found, dict):
        print(raw)
        return
    print(format_prune(found), end="")


def _cmd_login(args: argparse.Namespace) -> None:
    url = _http_url(args.url)
    token = args.token.strip()
    if not url or not token:
        _die("yard login needs --url and --token")

    # The token file stays private to this user.
    path = config_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"url": url, "token": token}, indent=2) + "\n")
    path.chmod(0o600)
    print("logged in")
    print(f"{'host'.ljust(6)}  {url}")
    print(f"{'saved'.ljust(6)}  {path}")


def _cmd_init(args: argparse.Namespace) -> None:
    domain = args.domain.strip()
    trigger = args.trigger.strip()
    if not domain and not trigger:
        _die("yard init needs --domain or --trigger")
    origin = args.remote.strip()
    try:
        if origin:
            origin = check_origin(origin)
        ref = args.ref.strip() or "main"
        check_ref(ref)
        if domain:
            check_domain(domain)
        directory = clean_dir(args.dir) if args.dir.strip() else ""
        trigger = parse_trigger(trigger)
    except YardError as exc:
        _fail(exc)

    client = _load(args)
    top = "" if origin else _git_top()
    folder = origin_name(origin) if origin else Path(top or Path.cwd()).name
    shared = args.name.strip()
    explicit = bool(args.repo.strip() or shared or origin)
    repo = _checked(args.repo.strip() or shared or folder, explicit, check_name)
    slot_name = _checked(args.slot.strip() or shared or folder, explicit, check_slot_name)
    fields = (repo, slot_name, ref, domain, directory, trigger)
    if origin:
        _init_remote(client, origin, *fields)
    else:
        _init_local(client, *fields, top)


def _init_remote(
    client: Client, origin: str, repo: str, slot_name: str, ref: str, domain: str, directory: str, trigger: str
) -> None:
    created, recorded = _ensure_repo(client, repo, origin)
    branch = ref or str(recorded.get("ref") or "main")
    if not branch.startswith("origin/"):
        branch = f"origin/{branch}"
    try:
        check_ref(branch)
    except YardError as exc:
        _fail(exc)
    url = f"{client.url}/git/{repo}.git"
    remote = public_origin(origin)
    slot = _create_slot(client, slot_name, repo, branch, domain, directory, trigger)
    summary = _init_summary(client, repo, slot_name, branch, remote, created, slot)
    if _json:
        print(json.dumps(summary))
        return
    rows = _init_rows(repo, url, slot_name, branch, domain, directory, trigger)
    rows.insert(2, ("remote", f"origin {remote}"))
    note = ["The host repo has origin set to that remote.", *_follow_note(repo, branch)]
    _print_setup("created a repo on the host and a slot", rows, note)


def _init_local(
    client: Client, repo: str, slot_name: str, ref: str, domain: str, directory: str, trigger: str, top: str
) -> None:
    created, _recorded = _ensure_repo(client, repo)
    url = f"{client.url}/git/{repo}.git"
    directory_path = top or _make_git(ref)
    _link_dir(directory_path, url, client.token)
    slot = _create_slot(client, slot_name, repo, ref, domain, directory, trigger)
    summary = _init_summary(client, repo, slot_name, ref, url, created, slot)
    push = f"git push -u yard {ref}"
    summary["push"] = push
    if _json:
        print(json.dumps(summary))
        return
    rows = _init_rows(repo, url, slot_name, ref, domain, directory, trigger)
    note = _follow_note(repo, ref)
    if not _head_ok(directory_path):
        note = ["Commit first.", *note]
    note.append(push)
    _print_setup("created a repo and a slot", rows, note)


def _create_slot(client: Client, name: str, repo: str, ref: str, domain: str, directory: str, trigger: str) -> Any:
    # The remote is already in git. The slot is what the next push builds.
    body: dict[str, object] = {"name": name, "repo": repo, "ref": ref}
    if trigger:
        body["trigger"] = trigger
    if domain:
        body["domain"] = domain
    if directory:
        body["dir"] = directory
    wrap = _object(_api(client, "POST", "/api/v1/slots", body))
    return wrap.get("slot") if isinstance(wrap, dict) else wrap


def _init_summary(
    client: Client, repo: str, slot_name: str, ref: str, remote: str, created: bool, slot: Any
) -> dict[str, Any]:
    url = f"{client.url}/git/{repo}.git"
    return {
        "name": slot_name,
        "repo": repo,
        "url": url,
        "remote": remote,
        "ref": ref,
        "created": created,
        "slot": slot,
    }


def _init_rows(
    repo: str, url: str, slot_name: str, ref: str, domain: str, directory: str, trigger: str
) -> list[tuple[str, str]]:
    rows = [("repo", repo), ("url", url), ("slot", slot_name), ("ref", ref)]
    if domain:
        rows.append(("domain", domain))
    if directory:
        rows.append(("dir", directory))
    if trigger:
        rows.append(("trigger", trigger))
    return rows


def _print_setup(title: str, rows: list[tuple[str, str]], note: list[str]) -> None:
    print(title)
    print()
    for key, value in rows:
        if value:
            print(f"{key.ljust(6)}  {value}")
    print()
    for line in note:
        print(line)


def _follow_note(repo: str, branch: str) -> list[str]:
    if branch.startswith("origin/"):
        return [
            f"The slot follows {branch}.",
            f"Run yard git {repo} fetch. A new tip deploys this slot.",
        ]
    return [
        f"The slot follows {branch}.",
        "Push this ref. Yard builds the image and starts the container.",
    ]


def _checked(name: str, explicit: bool, check: Callable[[str], None]) -> str:
    try:
        check(name)
    except YardError as exc:
        if not explicit:
            exc.help_text = "Pass --name, --repo, or --slot."
        _fail(exc)
    return name


def _ensure_repo(client: Client, name: str, origin: str = "") -> tuple[bool, dict[str, Any]]:
    payload: dict[str, Any] = {"name": name, "force": False}
    if origin:
        payload["origin"] = origin
    status, text = _api_try(client, "POST", "/api/v1/repos", payload)
    if status < 300:
        body = _object(text)
        return True, body if isinstance(body, dict) else {}
    if status == 409 and _error_code(text) == "exists" and not origin:
        return False, {}
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


def _cmd_slot_new(args: argparse.Namespace) -> None:
    if not args.repo:
        _die("yard slot new needs --repo")
    client = _load(args)
    body: dict[str, object] = {"name": args.name, "repo": args.repo, "ref": args.ref}
    if args.trigger:
        body["trigger"] = args.trigger
    if args.domain:
        body["domain"] = args.domain
    if args.dir:
        body["dir"] = args.dir
    raw = _api(client, "POST", "/api/v1/slots", body)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    slot = wrap.get("slot") if isinstance(wrap, dict) else None
    if not isinstance(slot, dict):
        print(raw)
        return
    repo = str(slot.get("repo") or "")
    branch = str(slot.get("ref") or "main")
    _print_setup(
        "created a slot",
        [
            ("repo", repo),
            ("url", f"{client.url}/git/{repo}.git"),
            ("slot", str(slot.get("name") or "")),
            ("ref", branch),
            ("domain", str(slot.get("domain") or "")),
            ("trigger", str(slot.get("trigger") or "")),
        ],
        _follow_note(repo, branch),
    )


def _cmd_slot_show(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "GET", "/api/v1/slots/" + quote(args.name), None)
    slot = _object(raw)
    if _json or not isinstance(slot, dict):
        print(raw)
        return
    print(format_slot(slot), end="")


def _cmd_slot_edit(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "GET", "/api/v1/slots/" + quote(args.name), None)
    current = _object(raw)
    if not isinstance(current, dict):
        _die(raw)
    if args.sets:
        try:
            patch = slot_edit_patch(current, [token for group in args.sets for token in _set_tokens(group)])
        except YardError as exc:
            _fail(exc)
    else:
        text = _edit_text(format_slot(current)) if sys.stdin.isatty() else _read_stdin()
        try:
            fields, env = parse_slot(text)
        except YardError as exc:
            _fail(exc)
        if fields["repo"] and fields["repo"] != str(current.get("repo") or ""):
            _warn(f"REPO stays {current.get('repo')}")
        patch = {
            "ref": fields["ref"],
            "dir": fields["dir"],
            "domain": fields["domain"],
            "trigger": fields["trigger"],
            "env": env,
        }
    raw = _api(client, "PATCH", "/api/v1/slots/" + quote(args.name), patch)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    slot = wrap.get("slot") if isinstance(wrap, dict) else None
    if not isinstance(slot, dict):
        _die(raw)
    print(format_slot(slot), end="")
    note = wrap.get("deploy")
    if isinstance(note, dict):
        _print_deploy_note(note)


def _cmd_slot_rm(args: argparse.Namespace) -> None:
    client = _load(args)
    _show_record(_api(client, "DELETE", "/api/v1/slots/" + quote(args.name), None))


def _cmd_deploy(args: argparse.Namespace) -> None:
    client = _load(args)
    body = {"sha": args.sha} if args.sha else {}
    raw = _api(client, "POST", "/api/v1/slots/" + quote(args.name) + "/deploy", body)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    note = wrap.get("deploy") if isinstance(wrap, dict) else None
    if not isinstance(note, dict):
        print(raw)
        return
    _print_deploy_note(note)


def _cmd_run(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "POST", "/api/v1/slots/" + quote(args.name) + "/run", None)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    print(str(wrap.get("output") or ""), end="" if str(wrap.get("output") or "").endswith("\n") else "\n")


def _print_deploy_note(note: dict) -> None:
    status = str(note.get("status") or "")
    image = str(note.get("image") or "")
    line = "  ".join(part for part in (status, image) if part)
    if _color_on():
        line = ansi(line, _deploy_attention(status)) or line
    if line:
        print(line)
    text = str(note.get("log") or "")
    if text:
        print(text, end="" if text.endswith("\n") else "\n")


def _cmd_deploys(args: argparse.Namespace) -> None:
    client = _load(args)
    name = quote(args.name)
    if args.deploy_id:
        raw = _api(client, "GET", f"/api/v1/slots/{name}/deploys/{quote(args.deploy_id)}", None)
        if _json:
            print(raw)
            return
        row = _object(raw)
        if not isinstance(row, dict):
            print(raw)
            return
        print(deploy_line(row, color=_color_on()))
        text = str(row.get("log") or "")
        if text:
            print(text, end="" if text.endswith("\n") else "\n")
        return
    raw = _api(client, "GET", f"/api/v1/slots/{name}/deploys", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_deploys(rows, color=_color_on()), end="")


def _cmd_logs(args: argparse.Namespace) -> None:
    _show_logs(
        args,
        "/api/v1/logs",
        {
            "slot": args.slot,
            "role": args.role,
            "service": args.service,
            "tail": str(args.tail),
            "cursor": args.cursor,
        },
    )


def _cmd_podman_logs(args: argparse.Namespace) -> None:
    _show_logs(
        args,
        "/api/v1/containers/" + quote(args.container) + "/logs",
        {"tail": str(args.tail), "cursor": args.cursor},
    )


def _show_logs(args: argparse.Namespace, path: str, query: dict[str, str]) -> None:
    picked = {key: value for key, value in query.items() if value}
    client = _load(args)
    if args.follow:
        picked["follow"] = "1"
        _stream(client, path + "?" + urlencode(picked))
        return
    raw = _api(client, "GET", path + "?" + urlencode(picked), None)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    if not isinstance(wrap, dict):
        print(raw)
        return
    if wrap.get("log"):
        text = str(wrap["log"])
        print(text, end="" if text.endswith("\n") else "\n")
    if wrap.get("cursor"):
        print(f"cursor: {wrap['cursor']}", file=sys.stderr)
    if wrap.get("more"):
        print("more: true", file=sys.stderr)


def _cmd_podman_inspect(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "GET", "/api/v1/containers/" + quote(args.container), None)
    if _json:
        print(raw)
        return
    print(format_inspect(raw, color=_color_on()), end="")


def _cmd_git(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "POST", "/api/v1/repos/" + quote(args.repo) + "/git", {"args": args.args})
    if _json:
        print(raw)
        return
    data = _object(raw)
    if not isinstance(data, dict):
        print(raw)
        return
    text = str(data.get("output") or "")
    print(text, end="" if text.endswith("\n") or not text else "\n")


def _cmd_repo_list(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/repos", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_repos(rows, color=_color_on()), end="")


def _cmd_repo_new(args: argparse.Namespace) -> None:
    client = _load(args)
    body: dict[str, Any] = {"name": args.name, "force": args.force}
    origin = args.remote.strip()
    try:
        if origin:
            body["origin"] = check_origin(origin)
    except YardError as exc:
        _fail(exc)
    raw = _api(client, "POST", "/api/v1/repos", body)
    if _json:
        print(raw)
        return
    data = _object(raw)
    if not isinstance(data, dict):
        print(raw)
        return
    name = str(data.get("name") or args.name)
    branch = f"origin/{data.get('ref') or 'main'}" if origin else ""
    rows = [("repo", name), ("url", f"{client.url}/git/{name}.git")]
    if origin:
        rows.extend((("remote", f"origin {public_origin(origin)}"), ("ref", branch)))
    note = (
        [
            "The host repo has origin set to that remote.",
            f"Run yard git {name} fetch. A new tip deploys a slot on {branch}.",
        ]
        if origin
        else ["Push a branch to that URL.", "A slot on that branch deploys."]
    )
    _print_setup("created a repo on the host", rows, note)


def _cmd_repo_rm(args: argparse.Namespace) -> None:
    _show_record(_api(_load(args), "DELETE", "/api/v1/repos/" + quote(args.name), None))


def _cmd_repo_link(args: argparse.Namespace) -> None:
    client = _load(args)
    remote = f"{client.url}/git/{args.repo}.git"
    _link_dir(".", remote, client.token)
    print(f"{'remote'.ljust(6)}  {remote}")


def _cmd_token_list(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/tokens", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_tokens(rows, color=_color_on()), end="")


def _cmd_token_new(args: argparse.Namespace) -> None:
    _show_record(_api(_load(args), "POST", "/api/v1/tokens", {"name": args.name}))


def _cmd_token_rm(args: argparse.Namespace) -> None:
    _show_record(_api(_load(args), "DELETE", "/api/v1/tokens/" + quote(args.name), None))


def _cmd_clone(args: argparse.Namespace) -> None:
    client = _load(args)
    remote = f"{client.url}/git/{args.name}.git"
    with tempfile.TemporaryDirectory(prefix="yard-cred") as tmp:
        cred = Path(tmp) / "cred"
        cred.write_text(_with_token(remote, client.token) + "\n")
        cred.chmod(0o600)
        command = ["-c", f"credential.helper=store --file {cred}", *clone_git_args(full=args.full, lazy=args.lazy)]
        command.extend([remote, args.name])
        _git([], command)
    _git([args.name], ["remote", "set-url", "origin", remote])
    _link_dir(args.name, remote, client.token)
    print(args.name)


def _show_record(raw: str) -> None:
    if _json:
        print(raw)
        return
    print(format_record(raw), end="")


def _set_tokens(group: list[str]) -> list[str]:
    if len(group) == 1:
        return group
    if len(group) == 2:
        return [f"{group[0]}={group[1]}"]
    raise YardError(400, "bad_args", "--set takes KEY VALUE or KEY=VALUE", example="--set REF main")


def _color_on(stream: object | None = None) -> bool:
    if _no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


# --- git and files ----------------------------------------------------------


def clone_git_args(*, full: bool, lazy: bool) -> list[str]:
    args = ["clone"]
    if not full:
        args.extend(["--depth", "1", "--single-branch"])
    if lazy:
        args.append("--filter=blob:none")
    return args


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


def _read_stdin() -> str:
    raw = sys.stdin.buffer.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        _die("the file is too large")
    return raw.decode()


def _edit_text(initial: str) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="yard-slot-", delete=False)
    path = Path(handle.name)
    try:
        path.write_text(initial)
        editor = os.environ.get("EDITOR") or "vi"
        proc = subprocess.run([*editor.split(), str(path)])
        if proc.returncode:
            _die(f"editor: exit {proc.returncode}")
        return path.read_text()
    finally:
        path.unlink(missing_ok=True)
