"""The `yard` argument tree and the entry point. One function per command; `main` dispatches."""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlencode, urlsplit

from app import package_version
from app.cli.render import (
    _deploy_attention,
    ansi,
    format_app_status,
    format_apps,
    format_binds,
    format_doctor,
    format_events,
    format_forges,
    format_prune,
    format_record,
    format_status,
    format_tokens,
    status_hint,
)
from app.shared import (
    FORGE_HOSTS,
    YardError,
    atomic_write,
    check_app_name,
    check_domain,
    check_repo,
    check_window,
    explain,
    format_env,
    parse_env,
)

_no_color = False
_json = False

TIMEOUT = 30
DEPLOY_TIMEOUT = 1800

FORGE_HINT = "No forge is connected. Run yard forge setup origin or yard forge setup github."


@dataclass(slots=True)
class Client:
    url: str
    token: str


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


def _api(client: Client, method: str, path: str, body: object, timeout: int = TIMEOUT) -> str:
    status, text = _api_try(client, method, path, body, timeout)
    if status >= 300:
        _die(text if _json else explain(text))
    return text


def _api_try(client: Client, method: str, path: str, body: object, timeout: int = TIMEOUT) -> tuple[int, str]:
    request = _request(client, method, path, body)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode().strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode().strip()
    except TimeoutError:
        _die(f"the Yard API did not answer in {timeout} s")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            _die(f"the Yard API did not answer in {timeout} s")
        _die(f"the Yard API did not answer: {exc.reason}")


def _stream_post(client: Client, path: str, body: object) -> None:
    _stream(client, "POST", path, body)


def _stream_get(client: Client, path: str) -> None:
    _stream(client, "GET", path, None)


def _stream(client: Client, method: str, path: str, body: object) -> None:
    """A streaming call: log follow has no timeout."""
    request = _request(client, method, path, body)
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode().strip()
        _die(raw if _json else explain(raw))
    except TimeoutError:
        _die("the Yard API did not answer")
    except urllib.error.URLError as exc:
        _die(f"the Yard API did not answer: {exc.reason}")
    with response:
        if response.status >= 300:
            raw = response.read().decode().strip()
            _die(raw if _json else explain(raw))
        shutil.copyfileobj(response, sys.stdout.buffer)


def _request(client: Client, method: str, path: str, body: object) -> urllib.request.Request:
    data = None if body is None else json.dumps(body).encode()
    headers = {"authorization": f"Bearer {client.token}"}
    if data is not None:
        headers["content-type"] = "application/json"
    return urllib.request.Request(client.url + path, data=data, headers=headers, method=method)


def _fail(exc: YardError) -> NoReturn:
    if _json:
        _die(json.dumps({"error": exc.payload()}))
    _die(str(exc))


def _die(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _object(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _die(raw)


def _git_out(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode:
        return ""
    return proc.stdout.strip()


def _checkout_pair() -> tuple[str, str]:
    """(https repo URL, branch) when this directory is a forge checkout, else two empties."""
    if not _git_out("rev-parse", "--show-toplevel"):
        return "", ""
    repo = _remote_repo(_git_out("remote", "get-url", "origin"))
    if not repo:
        return "", ""
    return repo, _git_out("branch", "--show-current")


def _remote_repo(remote: str) -> str:
    """The https repo URL when the remote is on Origin or GitHub. Three spellings count."""
    remote = remote.strip()
    short = re.fullmatch(r"git@([^:/]+):(.+)", remote)
    if short:
        host, path = short.group(1), short.group(2)
    else:
        parts = urlsplit(remote)
        host, path = parts.hostname or "", parts.path.lstrip("/")
    if host not in FORGE_HOSTS:
        return ""
    if path.endswith(".git"):
        path = path[:-4]
    found = path.split("/")
    if len(found) != 2 or not all(found):
        return ""
    return f"https://{host}/{found[0]}/{found[1]}"


def _default_name(repo: str, ref: str) -> str:
    """repo name for main and master; otherwise <repo>-<branch> as an app name."""
    name = repo.split("/")[-1]
    if ref not in {"main", "master"}:
        name = f"{name}-{ref}"
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    name = re.sub(r"-+", "-", name).strip("-")[:63].strip("-")
    return name


def _app_name(client_: Client, args: argparse.Namespace) -> str:
    """The app for this checkout. --app overrides. Every error mentions --app."""
    if str(getattr(args, "app", "") or "").strip():
        return str(args.app).strip()
    if not _git_out("rev-parse", "--show-toplevel"):
        _die("not a git checkout here; pass --app NAME")
    repo = _remote_repo(_git_out("remote", "get-url", "origin"))
    if not repo:
        _die("the origin remote is not on Origin or GitHub; pass --app NAME")
    branch = _git_out("branch", "--show-current")
    if not branch:
        _die("detached HEAD; pass --app NAME")
    rows = _object(_api(client_, "GET", "/api/v1/apps", None))
    found = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("repo") or "").lower() == repo.lower()
        and str(row.get("ref") or "") == branch
    ]
    if not found:
        _die(f"no app follows {repo} {branch}; pass --app NAME")
    if len(found) > 1:
        _die(f"more than one app follows {repo} {branch}; pass --app NAME")
    return str(found[0]["name"])


def _cmd_status(args: argparse.Namespace) -> None:
    session = _load(args)
    path = "/api/v1/status" + ("?running=1" if getattr(args, "running", False) else "")
    raw = _api(session, "GET", path, None)
    report = _object(raw)
    checks: Any = None
    if getattr(args, "full", False):
        checks = _object(_api(session, "GET", "/api/v1/doctor", None))
    if _json:
        print(json.dumps({"status": report, "doctor": checks}) if checks is not None else raw)
        _doctor_exit(checks)
        return
    if not isinstance(report, dict):
        print(raw)
        return
    print(format_status(report, datetime.now(UTC), color=_color_on()), end="")
    hint = status_hint(report)
    if hint and sys.stderr.isatty():
        if _color_on(sys.stderr):
            hint = ansi(hint, "2")
        print(hint, file=sys.stderr)
    _forge_warn(report)
    if isinstance(checks, list):
        print(format_doctor(checks, color=_color_on()), end="")
    _doctor_exit(checks)


def _doctor_exit(checks: Any) -> None:
    if isinstance(checks, list) and any(not check.get("ok") for check in checks if isinstance(check, dict)):
        raise SystemExit(1)


def _forge_warn(report: Any) -> None:
    """The forge hint on stderr. One connected forge stays quiet."""
    if not isinstance(report, dict):
        return
    host = report.get("host")
    forges = host.get("forges") if isinstance(host, dict) else {}
    if isinstance(forges, dict) and forges and not any(forges.values()):
        print(FORGE_HINT, file=sys.stderr)


def _cmd_prune(args: argparse.Namespace) -> None:
    session = _load(args)
    raw = _api(session, "POST", "/api/v1/prune", {"days": args.days, "dry": args.dry_run})
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
    atomic_write(path, json.dumps({"url": url, "token": token}, indent=2) + "\n")
    print("logged in")
    print(f"{'host'.ljust(6)}  {url}")
    print(f"{'saved'.ljust(6)}  {path}")

    # The login stands even when this call fails.
    code, text = _api_try(Client(url, token), "GET", "/api/v1/status", None)
    if code < 300:
        _forge_warn(_object(text))


def _cmd_app_ls(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/apps", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_apps(rows, color=_color_on()), end="")


def _cmd_app_new(args: argparse.Namespace) -> None:
    repo, ref = args.repo.strip(), args.ref.strip()
    if not repo or not ref:
        found_repo, found_ref = _checkout_pair()
        repo = repo or found_repo
        if not ref:
            # The checkout branch counts only when the repo is the checkout's.
            ref = found_ref if found_repo and repo.lower() == found_repo.lower() else ""
    ref = ref or "main"
    if not repo:
        _die("no repo; pass --repo https://<host>/<owner>/<name> or run inside a forge checkout")
    name = args.app.strip() or _default_name(repo, ref)
    try:
        repo = check_repo(repo)
        check_app_name(name)
        host = (args.public or "").strip()
        window = check_window(args.window or "") if host else ""
        if host:
            check_domain(host)
    except YardError as exc:
        _fail(exc)

    session = _load(args)
    body: dict[str, object] = {"name": name, "repo": repo, "ref": ref}
    if args.trigger:
        body["trigger"] = args.trigger
    raw = _api(session, "POST", "/api/v1/apps", body)
    if host:
        _api(session, "PUT", f"/api/v1/binds/{quote(host, safe='')}", {"dial": f"{name}-app-1", "try": window})
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    app = wrap.get("app") if isinstance(wrap, dict) else None
    if not isinstance(app, dict):
        print(raw)
        return
    print(format_record(json.dumps(app)), end="")
    print(f"The app follows {repo} {ref}. A push to it deploys. Run yard app deploy to deploy now.")


def _cmd_app_set(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    patch: dict[str, str] = {}
    for key in ("repo", "ref", "trigger"):
        value = str(getattr(args, key, "") or "").strip()
        if value:
            patch[key] = value
    if not patch:
        _die("yard app set needs --repo, --ref, or --trigger")
    page = _object(_api(session, "GET", "/api/v1/apps/" + quote(name), None))
    current = page.get("app") if isinstance(page, dict) else None
    current = current if isinstance(current, dict) else {}
    raw = _api(session, "PATCH", "/api/v1/apps/" + quote(name), patch)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    found = wrap.get("app") if isinstance(wrap, dict) else None
    app = found if isinstance(found, dict) else {}
    moved = ("repo" in patch and patch["repo"] != str(current.get("repo") or "")) or (
        "ref" in patch and patch["ref"] != str(current.get("ref") or "")
    )
    if moved and str(app.get("trigger") or "") == "push":
        print("The next check deploys it within 5 minutes. Run yard app deploy to deploy now.")
    else:
        print("Saved.")


def _cmd_app_status(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    raw = _api(session, "GET", f"/api/v1/apps/{quote(name)}", None)
    if _json:
        print(raw)
        return
    report = _object(raw)
    if not isinstance(report, dict):
        print(raw)
        return
    print(format_app_status(report, datetime.now(UTC), color=_color_on()), end="")


def _cmd_logs(args: argparse.Namespace) -> None:
    session = _load(args)
    _logs(session, args.container, args)


def _cmd_app_logs(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    container = args.container or (f"deploy-{name}" if args.deploy else f"{name}-app-1")
    _logs(session, container, args)


def _logs(session: Client, container: str, args: argparse.Namespace) -> None:
    query = {"lines": str(args.tail), "container": container}
    if args.since:
        query["since"] = args.since
    if args.until:
        query["until"] = args.until
    if args.follow:
        query["follow"] = "1"
    path = "/api/v1/logs?" + urlencode(query)
    if args.follow:
        _stream_get(session, path)
        return
    raw = _api(session, "GET", path, None)
    if _json:
        print(json.dumps({"log": raw}))
        return
    print(raw, end="" if raw.endswith("\n") or not raw else "\n")


def _cmd_events(args: argparse.Namespace) -> None:
    session = _load(args)
    query: dict[str, str] = {"lines": str(args.tail)}
    for key in ("since", "until", "app", "container", "source", "event"):
        value = str(getattr(args, key, "") or "")
        if value:
            query[key] = value
    if args.follow:
        query["follow"] = "1"
    path = "/api/v1/events?" + urlencode(query)
    if args.follow:
        _stream_get(session, path)
        return
    raw = _api(session, "GET", path, None)
    if _json:
        print(raw)
        return
    rows = _object(raw)
    if not isinstance(rows, list):
        print(raw)
        return
    print(format_events(rows, color=_color_on()), end="")


def _cmd_app_deploy(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    sha = str(args.sha or "").strip()
    body = {"sha": sha} if sha else {}
    raw = _api(session, "POST", f"/api/v1/apps/{quote(name)}/deploy", body, DEPLOY_TIMEOUT)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
    note = wrap.get("deploy") if isinstance(wrap, dict) else None
    if not isinstance(note, dict):
        print(raw)
        return
    _print_deploy_note(note)
    record = wrap.get("app") if isinstance(wrap, dict) else None
    if sha and isinstance(record, dict) and record.get("trigger") == "push":
        print(
            "The trigger is push: the next check deploys the tip again."
            " Run yard app set --trigger manual to keep this revision."
        )


def _cmd_app_rm(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    _show_record(_api(session, "DELETE", "/api/v1/apps/" + quote(name), None))


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


def _cmd_env_show(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    raw = _api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None)
    if _json:
        print(raw)
        return
    env = _object(raw)
    if not isinstance(env, dict):
        print(raw)
        return
    print(format_env(env), end="")


def _env_saved() -> None:
    print("Saved. Run yard app deploy to apply.")


def _cmd_env_set(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    patch: dict[str, str] = {}
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            _die(f"need KEY=VALUE, got {pair}")
        patch[key] = value
    raw = _api(session, "PATCH", f"/api/v1/apps/{quote(name)}/env", patch)
    if _json:
        print(raw)
        return
    _env_saved()


def _cmd_env_unset(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    patch = {key: None for key in args.keys}
    raw = _api(session, "PATCH", f"/api/v1/apps/{quote(name)}/env", patch)
    if _json:
        print(raw)
        return
    _env_saved()


def _cmd_env_edit(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    if sys.stdin.isatty():
        env = _object(_api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None))
        text = _edit_text(format_env(env if isinstance(env, dict) else {}))
    else:
        text = _read_stdin()
    try:
        env = parse_env(text)
    except YardError as exc:
        _fail(exc)
    raw = _api(session, "PUT", f"/api/v1/apps/{quote(name)}/env", env)
    if _json:
        print(raw)
        return
    _env_saved()


def _cmd_bind_set(args: argparse.Namespace) -> None:
    session = _load(args)
    dial = args.container.strip() or f"{_app_name(session, args)}-app-1"
    raw = _api(
        session,
        "PUT",
        f"/api/v1/binds/{quote(args.host, safe='')}",
        {"dial": dial, "try": args.window},
    )
    _print_binds(raw)


def _cmd_bind_ls(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/binds", None)
    _print_binds(raw)


def _cmd_bind_rm(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "DELETE", "/api/v1/binds/" + quote(args.host, safe=""), None)
    data = _object(raw)
    if _json or not isinstance(data, dict):
        print(raw)
        return
    print(f"removed {data.get('host') or args.host}")


def _print_binds(raw: str) -> None:
    data = _object(raw)
    rows = [data] if isinstance(data, dict) else data
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_binds(rows, color=_color_on()), end="")


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


def _cmd_forge_ls(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/forges", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_forges(rows, color=_color_on()), end="")


def _cmd_forge_setup(args: argparse.Namespace) -> None:
    session = _load(args)
    if args.forge == "origin":
        _setup_origin(session, args)
    else:
        _setup_github(session, args)


def _normalize_path(text: str) -> str:
    """A pasted path: whitespace, one pair of quotes, and `\\ ` escapes come off; ~ expands."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return os.path.expanduser(text.replace("\\ ", " "))


def _ask(prompt: str) -> str:
    """One wizard answer. EOF or Ctrl-C exits without a traceback."""
    try:
        return input(prompt).strip()
    except EOFError, KeyboardInterrupt:
        _die("stopped; run the command again to continue")


def _ask_key(key_flag: str) -> str:
    """The PEM text. An unreadable path or a file without a BEGIN line asks again."""
    default = _normalize_path(key_flag) if key_flag else ""
    while True:
        if default:
            key_path = _normalize_path(_ask(f"Private key path [{default}]: ")) or default
        else:
            key_path = _normalize_path(_ask("Private key path: "))
        if not key_path:
            print("pass --key PATH or paste a path", file=sys.stderr)
            continue
        try:
            pem = Path(key_path).read_text()
        except OSError as exc:
            print(f"cannot read the key: {exc}", file=sys.stderr)
            continue
        if "-----BEGIN" not in pem:
            print(f"{key_path} is not a PEM file (no -----BEGIN line)", file=sys.stderr)
            continue
        return pem


def _pending_setup(path: Path, url: str) -> dict:
    """A saved github setup for this host, or an empty dict."""
    try:
        raw = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict) or raw.get("url") != url:
        return {}
    return raw if isinstance(raw.get("secret"), str) else {}


def _setup_origin(session: Client, args: argparse.Namespace) -> None:
    """Connect to Cursor Origin: the host makes the key, the user makes the app."""
    key = _forge_row(session, "origin").get("public_key")
    print("Open https://cursor.com/codebase/settings/apps and create an app with these values.")
    print()
    print("public key:")
    print(str(key or ""), end="")
    print(f"webhook url: {session.url}/hooks/origin")
    print("event: repository.pushed")
    print("scope: repository:contents:read")
    print()
    _open(args, "https://cursor.com/codebase/settings/apps")

    while True:
        app_id = _ask("App ID: ")
        if re.fullmatch(r"app_[A-Za-z0-9]+", app_id):
            break
        print("That is not an app ID. It starts with app_.", file=sys.stderr)
    wrap = _object(_api(session, "PUT", "/api/v1/forges/origin", {"app_id": app_id}))
    app = wrap.get("app") if isinstance(wrap, dict) else None
    if not isinstance(app, dict):
        app = {}
    want = f"{session.url}/hooks/origin"
    have = str(app.get("webhook_url") or "")
    if have and have != want:
        print(f"warning: the webhook url is {have}; this host wants {want}", file=sys.stderr)
    events = app.get("events")
    if isinstance(events, list) and "repository.pushed" not in events:
        print("warning: the app does not subscribe to repository.pushed", file=sys.stderr)
    print("Install the app on your owner and select all repositories.")
    _open(
        args,
        "https://cursor.com/codebase/apps/install?"
        + urlencode({"client_id": app_id, "scope": "repository:contents:read"}),
    )
    _poll_installation(session, "origin")
    print("Done.")


def _setup_github(session: Client, args: argparse.Namespace) -> None:
    """Connect to GitHub: the key and the webhook secret come from the user."""
    parts = urlsplit(session.url)
    if parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
        _die("refusing to send the private key over http; log in with an https url")
    forge = {} if args.new else _forge_row(session, "github")
    if forge.get("connected"):
        print(
            f"GitHub is already connected: app {forge.get('app_id')},"
            f" installation {forge.get('installation_id')}. Run with --new to replace it."
        )
        return
    if forge.get("app_id") and not forge.get("installation_id"):
        print(f"App {forge['app_id']} is already set up. Install it and select all repositories.")
        slug = str(forge.get("slug") or "")
        if slug:
            _open(args, f"https://github.com/apps/{slug}/installations/new")
        else:
            print("Install it from https://github.com/settings/installations")
        _poll_installation(session, "github")
        print("Done.")
        return

    pending_path = config_path().parent / "github-setup.json"
    pending = {} if args.new else _pending_setup(pending_path, session.url)
    secret = str(pending.get("secret")) if pending else secrets.token_hex(32)
    print(f"webhook url: {session.url}/hooks/github")
    print("event: push")
    print("permission: contents read")
    print(f"webhook secret: {secret}")
    print("Paste this as the webhook secret.")
    print()
    host = parts.hostname or "host"
    base = (
        f"https://github.com/organizations/{quote(str(args.org))}/settings/apps/new"
        if args.org
        else "https://github.com/settings/apps/new"
    )
    link = (
        base
        + "?"
        + urlencode(
            {
                "name": f"yard-{host}",
                "url": session.url,
                "public": "false",
                "webhook_active": "true",
                "webhook_url": f"{session.url}/hooks/github",
                "contents": "read",
                "events[]": "push",
            }
        )
    )
    if pending:
        print(
            f"Continuing the setup from {pending.get('created') or 'an earlier run'}."
            " If you already created the GitHub App, do not create another: paste its App ID."
        )
        print(link)
    else:
        created = datetime.now(UTC).isoformat(timespec="seconds")
        atomic_write(
            pending_path,
            json.dumps({"url": session.url, "secret": secret, "created": created}, indent=2) + "\n",
        )
        _open(args, link)

    while True:
        app_id = _ask("App ID: ")
        if not app_id.isdigit():
            print("That is not an app ID. It is digits.", file=sys.stderr)
            continue
        pem = _ask_key(str(args.key))
        code, text = _api_try(
            session,
            "PUT",
            "/api/v1/forges/github",
            {"app_id": app_id, "private_key": pem, "webhook_secret": secret},
        )
        if 400 <= code < 500:
            print(text if _json else explain(text), file=sys.stderr)
            continue
        if code >= 300:
            _die(text if _json else explain(text))
        wrap = _object(text)
        break
    pending_path.unlink(missing_ok=True)
    app = wrap.get("app") if isinstance(wrap, dict) else None
    if not isinstance(app, dict):
        app = {}
    slug = str(app.get("slug") or "<slug>")
    print("Install the app on your owner and select all repositories.")
    _open(args, f"https://github.com/apps/{slug}/installations/new")
    _poll_installation(session, "github")
    print("Done.")


def _open(args: argparse.Namespace, url: str) -> None:
    """A link to open in the browser. --no-browser only prints it."""
    print(url)
    if not getattr(args, "no_browser", False):
        webbrowser.open(url)


SETUP_POLL = 3.0
SETUP_LIMIT = 600.0


def _forge_row(session: Client, name: str) -> dict:
    """One forge summary by name, or an empty one when the row is missing."""
    rows = _object(_api(session, "GET", "/api/v1/forges", None))
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("name") == name:
                return row
    return {}


def _poll_installation(session: Client, name: str) -> None:
    """Read the forge summary every three seconds until the installation lands. Ten minutes tops."""
    deadline = time.monotonic() + SETUP_LIMIT
    seen_error = ""
    while time.monotonic() < deadline:
        forge = _forge_row(session, name)
        error = str(forge.get("error") or "")
        if error and error != seen_error:
            print(f"warning: {error}", file=sys.stderr)
            seen_error = error
        if forge.get("installation_id"):
            selection = str(forge.get("selection") or "")
            if selection and selection != "all":
                print(
                    "warning: the installation covers selected repos only; new repos need a grant",
                    file=sys.stderr,
                )
            return
        time.sleep(SETUP_POLL)
    _die("No installation showed up in ten minutes.")


def _cmd_podman(args: argparse.Namespace) -> None:
    _stream_post(_load(args), "/api/v1/podman", {"args": _remainder(args.args)})


def _remainder(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def _show_record(raw: str) -> None:
    if _json:
        print(raw)
        return
    print(format_record(raw), end="")


def _color_on(stream: object | None = None) -> bool:
    if _no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _read_stdin() -> str:
    raw = sys.stdin.buffer.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        _die("the file is too large")
    return raw.decode()


def _edit_text(initial: str) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="yard-env-", delete=False)
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


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON")
    common.add_argument("--text", action="store_true", help="print text")
    common.add_argument("--no-color", action="store_true", help="plain output")
    common.add_argument("--url", default="", help="the host address")
    common.add_argument("--token", default="", help="a token")

    parser = argparse.ArgumentParser(
        prog="yard",
        description="Deploy a repo. Yard builds the image and serves the app.",
        parents=[common],
    )
    parser.add_argument("--version", action="store_true", help="print the version")
    subs = parser.add_subparsers(dest="command", metavar="command")

    status = subs.add_parser("status", help="running containers and app config", parents=[common])
    status.add_argument("--running", action="store_true", help="only running containers")
    status.add_argument("--full", action="store_true", help="include the host checks")
    status.set_defaults(func=_cmd_status)

    prune = subs.add_parser("prune", help="remove what no app needs", parents=[common])
    prune.add_argument("--days", type=int, default=7, help="keep what is newer than this")
    prune.add_argument("--dry-run", action="store_true", help="report only, remove nothing")
    prune.set_defaults(func=_cmd_prune)

    login = subs.add_parser("login", help="save a host and token", parents=[common])
    login.set_defaults(func=_cmd_login)

    logs = subs.add_parser("logs", help="container logs from the journal", parents=[common])
    logs.add_argument("container", help="a container name or id")
    _log_flags(logs)
    logs.set_defaults(func=_cmd_logs)

    events = subs.add_parser("events", help="the podman and yard event timeline", parents=[common])
    events.add_argument("--app", default="", help="only this app")
    events.add_argument("--container", default="", help="a container name or id")
    events.add_argument("--source", default="", choices=["", "podman", "yard"], help="podman or yard")
    events.add_argument("--event", default="", help="an event name prefix, like deploy.")
    events.add_argument("--since", default="", help="-15m, -1h, -2d, or 2025-01-31 14:00")
    events.add_argument("--until", default="", help="same forms as --since")
    events.add_argument("--tail", type=int, default=200, help="how many rows")
    events.add_argument("-f", "--follow", action="store_true", help="stream new events as ndjson")
    events.set_defaults(func=_cmd_events)

    for name, help_text, build in (
        ("app", "apps", _app_parser),
        ("env", "app env", _env_parser),
        ("bind", "binds", _bind_parser),
        ("token", "tokens", _token_parser),
        ("forge", "git forges", _forge_parser),
    ):
        group = subs.add_parser(name, help=help_text, parents=[common])
        verbs = group.add_subparsers(dest="verb", metavar="command", required=True)
        build(verbs, common)

    podman_cmd = subs.add_parser("podman", help="run podman on the host", parents=[common])
    podman_cmd.add_argument("args", nargs=argparse.REMAINDER)
    podman_cmd.set_defaults(func=_cmd_podman)

    return parser


def _add_app_flag(parser: argparse.ArgumentParser, help_text: str = "the app name") -> None:
    parser.add_argument("--app", default="", help=help_text)


def _log_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-f", "--follow", action="store_true", help="stream the log")
    parser.add_argument("--tail", type=int, default=100, help="how many lines")
    parser.add_argument("--since", default="", help="-15m, -1h, -2d, or 2025-01-31 14:00")
    parser.add_argument("--until", default="", help="same forms as --since")


def _app_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the apps", parents=[common])
    listed.set_defaults(func=_cmd_app_ls)

    new = subs.add_parser("new", help="create an app", parents=[common])
    new.add_argument("--repo", default="", help="the repo https URL")
    new.add_argument("--ref", default="", help="the branch to follow")
    new.add_argument("--trigger", default="", help="push or manual")
    new.add_argument("--public", metavar="HOST", default="", help="a name on the public internet")
    new.add_argument("--try", dest="window", default=None, help="how long caddy waits for the upstream")
    _add_app_flag(new, "the app name; the repo and branch give a default")
    new.set_defaults(func=_cmd_app_new)

    set_ = subs.add_parser("set", help="change the repo, ref, or trigger", parents=[common])
    set_.add_argument("--repo", default="", help="the repo https URL")
    set_.add_argument("--ref", default="", help="the branch to follow")
    set_.add_argument("--trigger", default="", help="push or manual")
    _add_app_flag(set_)
    set_.set_defaults(func=_cmd_app_set)

    stat = subs.add_parser("status", help="one app's page", parents=[common])
    _add_app_flag(stat)
    stat.set_defaults(func=_cmd_app_status)

    logs = subs.add_parser("logs", help="container logs", parents=[common])
    _log_flags(logs)
    logs.add_argument("--deploy", action="store_true", help="the deploy log instead")
    logs.add_argument("--container", default="", help="a container of the app")
    _add_app_flag(logs)
    logs.set_defaults(func=_cmd_app_logs)

    deploy = subs.add_parser("deploy", help="deploy the ref, or one commit with --sha", parents=[common])
    deploy.add_argument("--sha", default="", help="a commit to deploy; a prefix resolves to one deployed sha")
    _add_app_flag(deploy)
    deploy.set_defaults(func=_cmd_app_deploy)

    rm = subs.add_parser("rm", help="remove an app", parents=[common])
    _add_app_flag(rm)
    rm.set_defaults(func=_cmd_app_rm)


def _forge_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the forges", parents=[common])
    listed.set_defaults(func=_cmd_forge_ls)

    setup = subs.add_parser("setup", help="connect this host to a forge", parents=[common])
    setup.add_argument("forge", choices=["origin", "github"], help="the forge")
    setup.add_argument("--key", default="", help="the GitHub app private key PEM")
    setup.add_argument("--org", default="", help="create the GitHub app in this organization")
    setup.add_argument("--no-browser", action="store_true", help="print the links; open nothing")
    setup.add_argument("--new", action="store_true", help="start the GitHub setup over with a fresh secret")
    setup.set_defaults(func=_cmd_forge_setup)


def _env_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    show = subs.add_parser("show", help="the env file", parents=[common])
    _add_app_flag(show)
    show.set_defaults(func=_cmd_env_show)

    set_ = subs.add_parser("set", help="set env values", parents=[common])
    set_.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    _add_app_flag(set_)
    set_.set_defaults(func=_cmd_env_set)

    unset = subs.add_parser("unset", help="remove env values", parents=[common])
    unset.add_argument("keys", nargs="+", metavar="KEY")
    _add_app_flag(unset)
    unset.set_defaults(func=_cmd_env_unset)

    edit = subs.add_parser("edit", help="edit the env file", parents=[common])
    _add_app_flag(edit)
    edit.set_defaults(func=_cmd_env_edit)


def _bind_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the binds", parents=[common])
    listed.set_defaults(func=_cmd_bind_ls)

    set_ = subs.add_parser("set", help="bind a host to a container", parents=[common])
    set_.add_argument("host", help="a name on the public internet")
    set_.add_argument(
        "container",
        nargs="?",
        default="",
        help="CONTAINER or CONTAINER:PORT; the app container by default",
    )
    set_.add_argument("--try", dest="window", default="10s", help="how long caddy waits for the upstream")
    _add_app_flag(set_)
    set_.set_defaults(func=_cmd_bind_set)

    rm = subs.add_parser("rm", help="remove a bind", parents=[common])
    rm.add_argument("host", help="the public name")
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
