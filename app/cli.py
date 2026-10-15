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
from app.caddy import check_window
from app.errors import YardError, explain
from app.render import (
    _deploy_attention,
    ansi,
    format_doctor,
    format_prune,
    format_record,
    format_repos,
    format_slot,
    format_slot_status,
    format_slots,
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
    slug,
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

    # High-level commands combine the resources. The resource commands stay on one record.
    status = subs.add_parser("status", help="running containers and project config", parents=[common])
    status.add_argument("name", nargs="?", default="")
    status.add_argument("--running", action="store_true", help="only running containers")
    status.add_argument("--full", action="store_true", help="include the host checks")
    status.set_defaults(func=_cmd_status)

    up = subs.add_parser("up", help="build the project and start it", parents=[common])
    up.add_argument("name")
    up.add_argument("sha", nargs="?", default="")
    up.set_defaults(func=_cmd_up)

    init = subs.add_parser("init", help="create a repo, a project, and an optional bind", parents=[common])
    init.add_argument("--repo", default="", help="the repo name")
    init.add_argument("--project", default="", help="the project name")
    init.add_argument("--ref", default="", help="the branch the project follows")
    init.add_argument("--dir", default="", help="a subdirectory of the repo")
    init.add_argument("--trigger", default="", help="push, manual, or every 1h for a job")
    init.add_argument("--remote", default="", help="clone this remote on the host")
    side = init.add_mutually_exclusive_group()
    side.add_argument("--public", metavar="HOST", help="a name on the public internet")
    side.add_argument("--internal", metavar="HOST", help="a name on the private network")
    init.add_argument("--try", dest="window", default=None, help="how long caddy waits for the upstream")
    init.set_defaults(func=_cmd_init)

    clone = subs.add_parser("clone", help="copy a repo from the host", parents=[common])
    clone.add_argument("name")
    clone.add_argument("--project", default="", help="the project name")
    clone.add_argument("--ref", default="", help="the branch the project follows")
    clone.add_argument("--dir", default="", help="a subdirectory of the repo")
    clone.add_argument("--lazy", action="store_true", help="fetch blobs on demand")
    clone.add_argument("--full", action="store_true", help="the full history")
    side_clone = clone.add_mutually_exclusive_group()
    side_clone.add_argument("--public", metavar="HOST", help="a name on the public internet")
    side_clone.add_argument("--internal", metavar="HOST", help="a name on the private network")
    clone.add_argument("--try", dest="window", default=None, help="how long caddy waits for the upstream")
    clone.set_defaults(func=_cmd_clone)

    prune = subs.add_parser("prune", help="remove what no project needs", parents=[common])
    prune.add_argument("--days", type=int, default=7, help="keep what is newer than this")
    prune.add_argument("--dry-run", action="store_true", help="report only, remove nothing")
    prune.set_defaults(func=_cmd_prune)

    login = subs.add_parser("login", help="save a host and token", parents=[common])
    login.set_defaults(func=_cmd_login)

    for name, help_text, build in (
        ("project", "projects", _slot_parser),
        ("repo", "repos", _repo_parser),
        ("bind", "binds", _bind_parser),
        ("token", "tokens", _token_parser),
    ):
        group = subs.add_parser(name, help=help_text, parents=[common])
        verbs = group.add_subparsers(dest="verb", metavar="command", required=True)
        build(verbs, common)

    git_cmd = subs.add_parser("git", help="run git in a bare repo", parents=[common])
    git_cmd.add_argument("repo")
    git_cmd.add_argument("args", nargs=argparse.REMAINDER)
    git_cmd.set_defaults(func=_cmd_git)

    podman_cmd = subs.add_parser("podman", help="run podman", parents=[common])
    podman_cmd.add_argument("args", nargs=argparse.REMAINDER)
    podman_cmd.set_defaults(func=_cmd_podman)

    caddy_cmd = subs.add_parser("caddy", help="run caddy", parents=[common])
    caddy_cmd.add_argument("args", nargs=argparse.REMAINDER)
    caddy_cmd.set_defaults(func=_cmd_caddy)

    return parser


def _slot_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the projects", parents=[common])
    listed.set_defaults(func=_cmd_slot_ls)

    new = subs.add_parser("new", help="create a project", parents=[common])
    new.add_argument("name")
    new.add_argument("--repo", default="", help="the repo this project follows")
    new.add_argument("--ref", default="main", help="the branch to follow")
    new.add_argument("--dir", default="", help="a subdirectory of the repo")
    new.add_argument("--trigger", default="", help="push, manual, or every 1h for a job")
    new.set_defaults(func=_cmd_slot_new)

    show = subs.add_parser("show", help="the project file", parents=[common])
    show.add_argument("name")
    show.set_defaults(func=_cmd_slot_show)

    edit = subs.add_parser("edit", help="edit the project file", parents=[common])
    edit.add_argument("name")
    edit.add_argument("--set", dest="sets", action="append", nargs="+", metavar="KEY", help="set a field or env value")
    edit.set_defaults(func=_cmd_slot_edit)

    rm = subs.add_parser("rm", help="remove a project", parents=[common])
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_slot_rm)


def _repo_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the repos", parents=[common])
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


def _bind_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    new = subs.add_parser("new", help="bind a host to a container", parents=[common])
    side = new.add_mutually_exclusive_group(required=True)
    side.add_argument("--public", metavar="HOST", help="a name on the public internet")
    side.add_argument("--internal", metavar="HOST", help="a name on the private network")
    new.add_argument("container", help="CONTAINER or CONTAINER:PORT")
    new.add_argument("--try", dest="window", default="10s", help="how long caddy waits for the upstream")
    new.set_defaults(func=_cmd_bind_new)

    listed = subs.add_parser("ls", help="the binds", parents=[common])
    listed.set_defaults(func=_cmd_bind_ls)

    rm = subs.add_parser("rm", help="remove a bind", parents=[common])
    rm.add_argument("container")
    rm.set_defaults(func=_cmd_bind_rm)


def _token_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the tokens", parents=[common])
    listed.set_defaults(func=_cmd_token_list)

    new = subs.add_parser("new", help="create a token", parents=[common])
    new.add_argument("name")
    new.set_defaults(func=_cmd_token_new)

    rm = subs.add_parser("rm", help="remove a token", parents=[common])
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_token_rm)


def main(argv: list[str] | None = None) -> None:
    global _no_color, _json
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    _no_color = args.no_color
    _json = _as_json(args)
    if args.version:
        print(package_version())
        return
    if not hasattr(args, "func"):
        _parser().print_help()
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
    full = bool(getattr(args, "full", False))
    if name:
        raw = _api(client, "GET", f"/api/v1/projects/{name}/status", None)
    else:
        path = "/api/v1/status" + ("?running=1" if getattr(args, "running", False) else "")
        raw = _api(client, "GET", path, None)
    report = _object(raw)
    checks: Any = None
    if full:
        checks = _object(_api(client, "GET", "/api/v1/doctor", None))
    if _json:
        print(json.dumps({"status": report, "doctor": checks}) if full else raw)
        _doctor_exit(checks)
        return
    if not isinstance(report, dict):
        print(raw)
        return
    if name:
        print(format_slot_status(report, datetime.now(UTC), color=_color_on()), end="")
    else:
        print(format_status(report, datetime.now(UTC), color=_color_on()), end="")
        hint = status_hint(report)
        if hint and sys.stderr.isatty():
            if _color_on(sys.stderr):
                hint = ansi(hint, "2")
            print(hint, file=sys.stderr)
    if isinstance(checks, list):
        print(format_doctor(checks, color=_color_on()), end="")
    _doctor_exit(checks)


def _doctor_exit(checks: Any) -> None:
    if isinstance(checks, list) and any(not check.get("ok") for check in checks if isinstance(check, dict)):
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
    if args.internal:
        _die("An internal bind is not available yet.")
    if args.window and not args.public:
        _die("yard init --try needs --public")
    trigger = args.trigger.strip()
    origin = args.remote.strip()
    host = (args.public or "").strip()
    try:
        if origin:
            origin = check_origin(origin)
        ref = args.ref.strip() or "main"
        check_ref(ref)
        directory = clean_dir(args.dir) if args.dir.strip() else ""
        trigger = parse_trigger(trigger)
        window = check_window(args.window or "") if host else ""
        if host:
            check_domain(host)
    except YardError as exc:
        _fail(exc)

    client = _load(args)
    top = "" if origin else _git_top()
    folder = origin_name(origin) if origin else Path(top or Path.cwd()).name
    repo = _checked(args.repo.strip() or folder, bool(args.repo.strip()), check_name, "--repo")
    slot_name = _checked(args.project.strip() or folder, bool(args.project.strip()), check_slot_name, "--project")
    fields = (repo, slot_name, ref, directory, trigger, host, window)
    if origin:
        _init_remote(client, origin, *fields)
    else:
        _init_local(client, *fields, top)


def _init_remote(
    client: Client,
    origin: str,
    repo: str,
    slot_name: str,
    ref: str,
    directory: str,
    trigger: str,
    host: str,
    window: str,
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
    slot = _create_slot(client, slot_name, repo, branch, directory, trigger)
    bind = _bind_slot(client, slot_name, host, window)
    summary = _init_summary(client, repo, slot_name, branch, remote, created, slot, bind)
    if _json:
        print(json.dumps(summary))
        return
    rows = _init_rows(repo, url, slot_name, branch, directory, trigger, bind)
    rows.insert(2, ("remote", f"origin {remote}"))
    note = ["The host repo has origin set to that remote.", *_follow_note(repo, branch)]
    _print_setup(_init_title(host, remote=True), rows, note)


def _init_local(
    client: Client,
    repo: str,
    slot_name: str,
    ref: str,
    directory: str,
    trigger: str,
    host: str,
    window: str,
    top: str,
) -> None:
    created, _recorded = _ensure_repo(client, repo)
    url = f"{client.url}/git/{repo}.git"
    directory_path = top or _make_git(ref)
    _link_dir(directory_path, url, client.token)
    slot = _create_slot(client, slot_name, repo, ref, directory, trigger)
    bind = _bind_slot(client, slot_name, host, window)
    summary = _init_summary(client, repo, slot_name, ref, url, created, slot, bind)
    push = f"git push -u yard {ref}"
    summary["push"] = push
    if _json:
        print(json.dumps(summary))
        return
    rows = _init_rows(repo, url, slot_name, ref, directory, trigger, bind)
    note = _follow_note(repo, ref)
    if not _head_ok(directory_path):
        note = ["Commit first.", *note]
    note.append(push)
    _print_setup(_init_title(host, remote=False), rows, note)


def _bind_slot(client: Client, slot_name: str, host: str, window: str) -> dict[str, Any]:
    """One public bind for the new project. The dial is the project name."""
    if not host:
        return {}
    raw = _api(client, "PUT", "/api/v1/binds", {"host": host, "dial": slot_name, "try": window})
    data = _object(raw)
    return data if isinstance(data, dict) else {}


def _init_title(host: str, *, remote: bool) -> str:
    if remote and host:
        return "created a repo on the host, a project, and a bind"
    if remote:
        return "created a repo on the host and a project"
    if host:
        return "created a repo, a project, and a bind"
    return "created a repo and a project"


def _create_slot(client: Client, name: str, repo: str, ref: str, directory: str, trigger: str) -> Any:
    # The remote is already in git. The project is what the next push builds.
    body: dict[str, object] = {"name": name, "repo": repo, "ref": ref}
    if trigger:
        body["trigger"] = trigger
    if directory:
        body["dir"] = directory
    wrap = _object(_api(client, "POST", "/api/v1/projects", body))
    return wrap.get("project") if isinstance(wrap, dict) else wrap


def _init_summary(
    client: Client,
    repo: str,
    slot_name: str,
    ref: str,
    remote: str,
    created: bool,
    slot: Any,
    bind: dict[str, Any],
) -> dict[str, Any]:
    url = f"{client.url}/git/{repo}.git"
    body: dict[str, Any] = {
        "name": slot_name,
        "repo": repo,
        "url": url,
        "remote": remote,
        "ref": ref,
        "created": created,
        "project": slot,
    }
    if bind:
        body["bind"] = bind
    return body


def _init_rows(
    repo: str,
    url: str,
    slot_name: str,
    ref: str,
    directory: str,
    trigger: str,
    bind: dict[str, Any],
) -> list[tuple[str, str]]:
    rows = [("repo", repo), ("url", url), ("project", slot_name), ("ref", ref)]
    if directory:
        rows.append(("dir", directory))
    if trigger:
        rows.append(("trigger", trigger))
    if bind:
        rows.append(("public", f"{bind.get('host') or ''}  {bind.get('dial') or ''}"))
    return rows


def _print_setup(title: str, rows: list[tuple[str, str]], note: list[str]) -> None:
    print(title)
    print()
    for key, value in rows:
        if value:
            print(f"{key.ljust(7)}  {value}")
    print()
    for line in note:
        print(line)


def _follow_note(repo: str, branch: str) -> list[str]:
    if branch.startswith("origin/"):
        return [
            f"The project follows {branch}.",
            f"Run yard git {repo} fetch. A new tip deploys this project.",
        ]
    return [
        f"The project follows {branch}.",
        "Push this ref. Yard builds the image and starts the container.",
    ]


def _checked(name: str, explicit: bool, check: Callable[[str], None], flag: str) -> str:
    try:
        check(name)
    except YardError as exc:
        if not explicit:
            exc.help_text = f"Pass {flag}."
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


def _cmd_slot_ls(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/projects", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_slots(rows, color=_color_on()), end="")


def _cmd_slot_new(args: argparse.Namespace) -> None:
    if not args.repo:
        _die("yard project new needs --repo")
    client = _load(args)
    body: dict[str, object] = {"name": args.name, "repo": args.repo, "ref": args.ref}
    if args.trigger:
        body["trigger"] = args.trigger
    if args.dir:
        body["dir"] = args.dir
    raw = _api(client, "POST", "/api/v1/projects", body)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    slot = wrap.get("project") if isinstance(wrap, dict) else None
    if not isinstance(slot, dict):
        print(raw)
        return
    print(format_slot(slot), end="")


def _cmd_slot_show(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "GET", "/api/v1/projects/" + quote(args.name), None)
    slot = _object(raw)
    if _json or not isinstance(slot, dict):
        print(raw)
        return
    print(format_slot(slot), end="")


def _cmd_slot_edit(args: argparse.Namespace) -> None:
    client = _load(args)
    raw = _api(client, "GET", "/api/v1/projects/" + quote(args.name), None)
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
            "trigger": fields["trigger"],
            "env": env,
        }
    raw = _api(client, "PATCH", "/api/v1/projects/" + quote(args.name), patch)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    slot = wrap.get("project") if isinstance(wrap, dict) else None
    if not isinstance(slot, dict):
        _die(raw)
    print(format_slot(slot), end="")
    note = wrap.get("deploy")
    if isinstance(note, dict):
        _print_deploy_note(note)


def _cmd_slot_rm(args: argparse.Namespace) -> None:
    client = _load(args)
    _show_record(_api(client, "DELETE", "/api/v1/projects/" + quote(args.name), None))


def _cmd_up(args: argparse.Namespace) -> None:
    """A service builds and starts. A job runs now."""
    client = _load(args)
    name = quote(args.name)
    if args.sha:
        _up_deploy(client, name, args.sha)
        return
    raw = _api(client, "GET", f"/api/v1/projects/{name}", None)
    slot = _object(raw)
    trigger = str(slot.get("trigger") or "") if isinstance(slot, dict) else ""
    if trigger:
        _up_run(client, name)
        return
    _up_deploy(client, name, "")


def _up_deploy(client: Client, name: str, sha: str) -> None:
    body = {"sha": sha} if sha else {}
    raw = _api(client, "POST", f"/api/v1/projects/{name}/deploy", body)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    note = wrap.get("deploy") if isinstance(wrap, dict) else None
    if not isinstance(note, dict):
        print(raw)
        return
    _print_deploy_note(note)


def _up_run(client: Client, name: str) -> None:
    raw = _api(client, "POST", f"/api/v1/projects/{name}/run", None)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    text = str(wrap.get("output") or "") if isinstance(wrap, dict) else ""
    if text:
        print(text, end="" if text.endswith("\n") else "\n")
        return
    print(raw)


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


def _cmd_podman(args: argparse.Namespace) -> None:
    words = _remainder(args.args)
    container = _follow_logs(words)
    if container:
        query = {"follow": "1", "tail": _log_tail(words)}
        _stream(_load(args), "/api/v1/containers/" + quote(container, safe="") + "/logs?" + urlencode(query))
        return
    raw = _api(_load(args), "POST", "/api/v1/podman", {"args": words})
    _print_command(raw)


def _cmd_caddy(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "POST", "/api/v1/caddy", {"args": _remainder(args.args)})
    _print_command(raw)


def _cmd_bind_new(args: argparse.Namespace) -> None:
    if args.internal:
        _die("An internal bind is not available yet.")
    raw = _api(
        _load(args),
        "PUT",
        "/api/v1/binds",
        {"host": args.public, "dial": args.container, "try": args.window},
    )
    _print_rows(raw, ("host", "dial", "state"))


def _cmd_bind_ls(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/binds", None)
    _print_rows(raw, ("host", "dial", "state"))


def _cmd_bind_rm(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "DELETE", "/api/v1/binds/" + quote(args.container, safe=""), None)
    data = _object(raw)
    if _json or not isinstance(data, dict):
        print(raw)
        return
    print(f"removed {data.get('dial') or args.container}")


def _follow_logs(args: list[str]) -> str:
    """The container for `podman logs -f`. An empty string means this is not that command."""
    if not args or args[0] != "logs" or "-f" not in args and "--follow" not in args:
        return ""
    skip = {"--tail", "-n", "--since", "--until"}
    container = ""
    index = 1
    while index < len(args):
        word = args[index]
        if word in {"-f", "--follow"}:
            index += 1
            continue
        if word.split("=", 1)[0] in skip and "=" not in word:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        container = word
        index += 1
    if not container:
        _die("yard podman logs -f needs a container")
    return container


def _log_tail(args: list[str]) -> str:
    for index, word in enumerate(args):
        if word in {"--tail", "-n"} and index + 1 < len(args):
            return args[index + 1]
        if word.startswith("--tail="):
            return word.split("=", 1)[1]
    return "200"


def _remainder(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def _print_rows(raw: str, keys: tuple[str, ...]) -> None:
    data = _object(raw)
    rows = [data] if isinstance(data, dict) else data
    if _json or not isinstance(rows, list):
        print(raw)
        return
    for row in rows:
        if isinstance(row, dict):
            print("  ".join(str(row.get(key) or "") for key in keys))


def _print_command(raw: str) -> None:
    if _json:
        print(raw)
        return
    data = _object(raw)
    if not isinstance(data, dict):
        print(raw)
        return
    text = str(data.get("output") or "")
    print(text, end="" if text.endswith("\n") or not text else "\n")


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
    note = ["The host repo has origin set to that remote."] if origin else ["Push a branch to that URL."]
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
    if args.internal:
        _die("An internal bind is not available yet.")
    if args.window and not args.public:
        _die("yard clone --try needs --public")
    ref = args.ref.strip()
    project_flag = args.project.strip()
    host = (args.public or "").strip()
    directory = args.dir.strip()
    if (project_flag or host or directory) and not ref:
        _die("yard clone --ref names the branch")
    try:
        check_name(args.name)
        if ref == "main" or ref.startswith("origin/"):
            _die("yard clone --ref follows a feature branch")
        if ref:
            check_ref(ref)
        directory = clean_dir(directory) if directory else ""
        window = check_window(args.window or "") if host else ""
        if host:
            check_domain(host)
        project = project_flag or (f"{args.name}-{slug(ref)}" if ref else "")
        if project:
            try:
                check_slot_name(project)
            except YardError as exc:
                if not project_flag:
                    exc.help_text = "Pass --project."
                raise
    except YardError as exc:
        _fail(exc)

    client = _load(args)
    _clone_repo(client, args.name, full=args.full, lazy=args.lazy)
    if not ref:
        print(args.name)
        return
    _git([args.name], ["checkout", "-b", ref])
    slot = _create_slot(client, project, args.name, ref, directory, "")
    bind = _bind_slot(client, project, host, window)
    url = f"{client.url}/git/{args.name}.git"
    push = f"git push -u yard {ref}"
    summary = _init_summary(client, args.name, project, ref, url, False, slot, bind)
    summary["push"] = push
    if _json:
        print(json.dumps(summary))
        return
    title = "cloned the repo, created a project, and added a bind" if host else "cloned the repo and created a project"
    rows = _init_rows(args.name, url, project, ref, directory, "", bind)
    note = [
        f"The project follows {ref}.",
        "Push this branch. Yard builds the image and starts the container.",
        "Yard removes the project when the branch merges.",
        push,
    ]
    _print_setup(title, rows, note)


def _clone_repo(client: Client, name: str, *, full: bool, lazy: bool) -> None:
    remote = f"{client.url}/git/{name}.git"
    with tempfile.TemporaryDirectory(prefix="yard-cred") as tmp:
        cred = Path(tmp) / "cred"
        cred.write_text(_with_token(remote, client.token) + "\n")
        cred.chmod(0o600)
        command = ["-c", f"credential.helper=store --file {cred}", *clone_git_args(full=full, lazy=lazy)]
        command.extend([remote, name])
        _git([], command)
    _git([name], ["remote", "set-url", "origin", remote])
    _link_dir(name, remote, client.token)


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
    handle = tempfile.NamedTemporaryFile(prefix="yard-project-", delete=False)
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
