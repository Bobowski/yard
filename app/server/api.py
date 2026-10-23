"""The public HTTP API. The forge webhooks sit beside it and skip auth. Only this file writes JSON shapes."""

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from stario import App as StarioApp
from stario import Context, Handler, Middleware, Route, Writer, json
from stario.http.middleware import catch_errors
from stario.json import JsonValue

from app.server import apps, binds, compose, doctor, journal, status
from app.server.forges import Forge, GitHub, Origin
from app.server.store import App, Config, Store
from app.server.yard import Yard
from app.shared import YardError, now

HEALTH = Route("GET /health")
HOOK_ORIGIN = Route("POST /hooks/origin")
HOOK_GITHUB = Route("POST /hooks/github")
STATUS = Route("GET /api/v1/status")
LOGS = Route("GET /api/v1/logs")
EVENTS = Route("GET /api/v1/events")
DOCTOR = Route("GET /api/v1/doctor")
FORGES = Route("GET /api/v1/forges")
FORGE_PUT = Route("PUT /api/v1/forges/{name}")
APPS = Route("GET /api/v1/apps")
APP_CREATE = Route("POST /api/v1/apps")
APP = Route("GET /api/v1/apps/{name}")
APP_PATCH = Route("PATCH /api/v1/apps/{name}")
APP_DELETE = Route("DELETE /api/v1/apps/{name}")
APP_DEPLOY = Route("POST /api/v1/apps/{name}/deploy")
APP_FILE = Route("GET /api/v1/apps/{name}/files/{which}")
ENV = Route("GET /api/v1/apps/{name}/env")
ENV_PATCH = Route("PATCH /api/v1/apps/{name}/env")
ENV_PUT = Route("PUT /api/v1/apps/{name}/env")
BINDS = Route("GET /api/v1/binds")
BIND_PUT = Route("PUT /api/v1/binds/{host}")
BIND_DELETE = Route("DELETE /api/v1/binds/{host}")
PRUNE = Route("POST /api/v1/prune")
TOKENS = Route("GET /api/v1/tokens")
TOKEN_CREATE = Route("POST /api/v1/tokens")
TOKEN_DELETE = Route("DELETE /api/v1/tokens/{name}")


async def json_object(c: Context) -> dict:
    raw = await c.req.body()
    if not raw or not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.") from exc
    if not isinstance(body, dict):
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.")
    return body


def app_fields(body: dict) -> dict[str, str]:
    """The fields an app PATCH or POST takes: repo, ref, trigger."""
    fields: dict[str, str] = {}
    for key in ("repo", "ref", "trigger", "containerfile", "compose"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str):
            raise YardError(400, "bad_json", "bad json", help_text=f"{key} is a string.")
        fields[key] = value
    return fields


def app_json(app: App) -> dict[str, Any]:
    return {
        "name": app.name,
        "repo": app.repo,
        "ref": app.ref,
        "trigger": app.trigger,
        "created": app.created,
        "containerfile": app.containerfile,
        "compose": app.compose,
    }


def bind_json(bind: binds.Bind) -> dict[str, str]:
    return {"host": bind.host, "dial": bind.dial, "id": bind.id, "try": bind.window, "state": bind.state}


def page_json(page: status.Page) -> dict[str, Any]:
    app = app_json(page.app)
    if page.image:
        app["image"] = page.image
        app["sha"] = page.sha
    return {
        "app": app,
        "env_file": page.env_file,
        "rows": [asdict(row) for row in page.rows],
        "binds": page.binds,
        "deploys": page.deploys,
        "log": page.log,
    }


def _actor(c: Context) -> str:
    """The token name on the request. Webhook work passes its own actor."""
    return str(c.state.get("actor") or "")


async def health(_c: Context, w: Writer) -> None:
    w.empty(200)


def host_status(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        report = await status.report(yard, c.req.query.get("running") == "1")
        w.json(cast(JsonValue, asdict(report)))

    return handler


def doctor_run(yard: Yard) -> Handler:
    async def handler(_c: Context, w: Writer) -> None:
        w.json([asdict(check) for check in await doctor.run(yard)])

    return handler


def run_prune(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        data = await json_object(c)
        days = data.get("days", 7)
        dry = data.get("dry", False)
        if not isinstance(days, int) or isinstance(days, bool) or days < 1 or not isinstance(dry, bool):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="days is a number 1 or more. dry is true or false.",
                example='{"days": 7, "dry": false}',
            )
        w.json(cast(JsonValue, asdict(await apps.prune(yard, days, dry, actor=_actor(c)))))

    return handler


async def _hook_body(c: Context, w: Writer) -> bytes | None:
    """One megabyte tops. Oversize gets a 413 and no processing."""
    raw = await c.req.body()
    if len(raw) > 1 << 20:
        w.empty(413)
        return None
    return raw


def _hook_received(config: Config, forge: Forge, repo: str, kind: str, c: Context) -> None:
    delivery = (
        c.req.headers.get("x-github-delivery")
        or c.req.headers.get("x-origin-delivery")
        or c.req.headers.get("x-delivery")
        or ""
    )
    journal.send(
        config,
        "hook.received",
        f"{forge.name} {kind}",
        forge=forge.name,
        repo=repo,
        kind=str(kind),
        delivery=delivery,
    )


def origin_hook(origin: Origin, yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        """An Origin event. No auth. A delivery only schedules a check.

        The envelope is {"event": {"type": ..., "payload": {...}}}. Yard reads the
        tip from the forge and never trusts the body. Always answers fast.
        """
        raw = await _hook_body(c, w)
        if raw is None:
            return
        if not raw.strip():
            w.empty(204)
            return
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.") from exc
        origin.last_delivery = now()
        event = body.get("event") if isinstance(body, dict) else None
        kind = event.get("type") if isinstance(event, dict) else ""
        payload = event.get("payload") if isinstance(event, dict) else {}
        if kind == "repository.pushed" and isinstance(payload, dict):
            repo = payload.get("repository")
            if isinstance(repo, dict):
                owner = repo.get("owner")
                slug = owner.get("slug") if isinstance(owner, dict) else ""
                name = repo.get("name") or ""
                if slug and name:
                    url = f"https://{origin.host}/{slug}/{name}"
                    _hook_received(yard.config, origin, url, kind, c)
                    c.app.create_task(apps.check_repo(yard, url))
        elif isinstance(kind, str) and kind.startswith("installation."):
            _hook_received(yard.config, origin, "", str(kind), c)
            c.app.create_task(apps.check_installation(yard, origin))
        w.empty(204)

    return handler


def github_hook(github: GitHub, yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        """A GitHub delivery. The signature proves it before anything runs.

        X-Hub-Signature-256 is HMAC-SHA256 over the raw body with the webhook
        secret the setup stored. A bad or missing signature gets a 401 and
        nothing else happens.
        """
        raw = await _hook_body(c, w)
        if raw is None:
            return
        if not github.verify(raw, c.req.headers.get("x-hub-signature-256") or ""):
            w.json({"error": YardError(401, "unauthorized", "bad signature").payload()}, 401)
            return
        github.last_delivery = now()
        if not raw.strip():
            w.empty(204)
            return
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.") from exc
        kind = c.req.headers.get("x-github-event") or ""
        if kind == "push" and isinstance(body, dict):
            repo = body.get("repository")
            full = repo.get("full_name") if isinstance(repo, dict) else ""
            if full:
                url = f"https://{github.host}/{full}"
                _hook_received(yard.config, github, url, kind, c)
                c.app.create_task(apps.check_repo(yard, url))
        elif kind in ("installation", "installation_repositories"):
            _hook_received(yard.config, github, "", kind, c)
            c.app.create_task(apps.check_installation(yard, github))
        w.empty(204)

    return handler


def _named(forges: dict[str, Forge], name: str) -> Forge:
    for forge in forges.values():
        if forge.name == name:
            return forge
    raise YardError(404, "not_found", f"unknown forge {name}", help_text="The forges are origin and github.")


def forges_list(forges: dict[str, Forge], config: Config) -> Handler:
    async def handler(_c: Context, w: Writer) -> None:
        """The forge summaries. A connected app with no installation gets one
        lookup per read; a lookup failure only marks that summary."""
        rows = []
        for forge in forges.values():
            error = await _install(forge, config)
            summary = forge.summary()
            if error:
                summary["error"] = error
            rows.append(summary)
        w.json(cast(JsonValue, rows))

    return handler


async def _install(forge: Forge, config: Config) -> str:
    """One find_installation for an app that has none. Returns the error text."""
    if not forge.app_id or forge.installation_id:
        return ""
    try:
        installation = await forge.find_installation()
    except Exception as exc:
        return str(exc)
    if installation is None:
        return ""
    selection = forge.selection(installation)
    forge.save(selection=selection)
    journal.send(
        config,
        "forge.installation",
        f"{forge.name} installation",
        forge=forge.name,
        installation_id=forge.installation_id,
        selection=selection,
    )
    return ""


def forge_put(forges: dict[str, Forge], config: Config) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        """Save a forge app once its credentials pass the forge's own check."""
        forge = _named(forges, c.match.params["name"])
        data = await json_object(c)
        if isinstance(forge, Origin):
            app_id = data.get("app_id", "")
            if not isinstance(app_id, str) or not app_id:
                raise YardError(
                    400,
                    "bad_json",
                    "bad json",
                    help_text="app_id is a string.",
                    example='{"app_id": "app_..."}',
                )
            info = await forge.connect(app_id)
            journal.send(
                config,
                "forge.connect",
                f"{forge.name} connected",
                forge=forge.name,
                app_id=app_id,
                actor=_actor(c),
            )
            app = {
                "name": info.get("displayName") or info.get("name") or "",
                "webhook_url": info.get("webhookUrl") or info.get("webhook_url") or "",
                "events": info.get("events") or [],
            }
        elif isinstance(forge, GitHub):
            app_id = data.get("app_id", "")
            private_key = data.get("private_key", "")
            secret = data.get("webhook_secret", "")
            if (
                not isinstance(app_id, str)
                or not isinstance(private_key, str)
                or not isinstance(secret, str)
                or not app_id
                or not private_key
                or not secret
            ):
                raise YardError(
                    400,
                    "bad_json",
                    "bad json",
                    help_text="app_id, private_key, and webhook_secret are strings.",
                )
            info = await forge.connect(app_id, private_key, secret)
            journal.send(
                config,
                "forge.connect",
                f"{forge.name} connected",
                forge=forge.name,
                app_id=app_id,
                actor=_actor(c),
            )
            app = {
                "name": info.get("name") or "",
                "slug": info.get("slug") or "",
                "events": info.get("events") or [],
            }
        else:
            raise YardError(
                404, "not_found", f"unknown forge {forge.name}", help_text="The forges are origin and github."
            )
        w.json(cast(JsonValue, {"forge": forge.summary(), "app": app}))

    return handler


def apps_list(store: Store) -> Handler:
    async def handler(_c: Context, w: Writer) -> None:
        w.json([app_json(app) for app in store.list_apps()])

    return handler


def apps_create(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        data = await json_object(c)
        fields = app_fields(data)
        name = data.get("name", "")
        repo = fields.pop("repo", "")
        if not isinstance(name, str) or not isinstance(repo, str):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="name and repo are strings.",
            )
        if not name.strip():
            raise YardError(
                400,
                "bad_json",
                "name is required",
                context={"field": "name"},
                help_text="An app name uses lowercase letters, digits, and -",
                example="staging",
            )
        if not repo.strip():
            raise YardError(
                400,
                "bad_json",
                "repo is required",
                context={"field": "repo"},
                help_text="The repo as a full https URL.",
                example="https://github.com/org/heldso",
            )
        app = apps.create(yard, name, repo, actor=_actor(c), **cast(dict[str, Any], fields))
        w.json(cast(JsonValue, {"app": app_json(app)}))

    return handler


def app_page(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        page = await status.app_report(yard, c.match.params["name"])
        w.json(cast(JsonValue, page_json(page)))

    return handler


def app_patch(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        fields = app_fields(await json_object(c))
        if not fields:
            raise YardError(
                400,
                "bad_json",
                "nothing to change",
                help_text="Set one of repo, ref, or trigger.",
            )
        app = apps.update(yard, name, actor=_actor(c), **cast(dict[str, Any], fields))
        w.json(cast(JsonValue, {"app": app_json(app)}))

    return handler


def app_delete(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        removed = await apps.delete(yard, name, with_binds=c.req.query.get("binds") == "1", actor=_actor(c))
        body: dict[str, Any] = {
            "name": removed.name,
            "binds_removed": [{"host": host, "dial": dial} for host, dial in removed.binds_removed],
            "binds_left": [{"host": host, "dial": dial} for host, dial in removed.binds_left],
        }
        if removed.binds_error:
            body["binds_error"] = removed.binds_error
        w.json(cast(JsonValue, body))

    return handler


def app_deploy(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        yard.store.app(name)
        data = await json_object(c)
        sha = data.get("sha", "")
        if not isinstance(sha, str):
            raise YardError(400, "bad_json", "bad json", help_text="sha is a string.")
        requested = sha = sha.strip()
        if sha and not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise YardError(
                400,
                "bad_sha",
                f"{sha} is not a sha",
                help_text="7 to 40 lowercase hex characters.",
            )
        if sha and len(sha) < 40:
            sha = await _resolve_sha(yard.config, name, sha)
        outcome = await apps.deploy(
            yard,
            name,
            sha=sha,
            requested=requested,
            cause="manual",
            actor=_actor(c),
        )
        if outcome.status == "failed":
            raise YardError(
                500,
                "deploy_failed",
                outcome.log.strip() or outcome.error or "deploy failed",
                context={"app": name},
                help_text=f"Read the full log: yard app logs --deploy --app {name}",
            )
        w.json(
            cast(
                JsonValue,
                {
                    "app": app_json(yard.store.app(name)),
                    "deploy": asdict(outcome),
                },
            ),
        )

    return handler


def app_file(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        """The compose or Containerfile an app runs, as text/plain."""
        app = yard.store.app(c.match.params["name"])
        w.text(_app_file(yard.config, app, c.match.params["which"]))

    return handler


def _app_file(config: Config, app: App, which: str) -> str:
    deploy = f"Yard decides on the first deploy: yard app deploy --app {app.name}"
    if which == "compose":
        if app.compose:
            return _clone_file(config.clone(app.name), app.compose, app.name)
        if app.containerfile:
            path = Path(compose.APPLY_DIR) / "default.compose.yml"
            if path.is_file():
                return path.read_text()
            raise YardError(404, "no_file", "Yard's compose file is missing on this host", help_text=deploy)
        raise YardError(404, "no_file", f"{app.name} has no compose file yet", help_text=deploy)
    if which == "containerfile":
        if app.containerfile == "yard":
            path = config.app_dir(app.name) / "Containerfile"
            if path.is_file():
                return path.read_text()
            raise YardError(
                404,
                "no_file",
                f"{app.name}'s Containerfile is missing",
                help_text="Run yard app detect to write it again.",
            )
        if app.containerfile:
            return _clone_file(config.clone(app.name), app.containerfile, app.name)
        if app.compose:
            raise YardError(
                404,
                "no_file",
                f"{app.name} runs its own compose file; that file names its build",
                help_text="yard app cat compose",
            )
        raise YardError(404, "no_file", f"{app.name} has no Containerfile yet", help_text=deploy)
    raise YardError(404, "no_file", f"no file {which}", help_text="The files are compose and containerfile.")


def _clone_file(clone: Path, rel: str, name: str) -> str:
    root = clone.resolve()
    path = (root / rel).resolve()
    if path.parent != root and root not in path.parents:
        raise YardError(404, "no_file", f"{rel} is outside the clone")
    if not path.is_file():
        raise YardError(
            404,
            "no_file",
            f"{rel} is not in {name}'s clone",
            help_text=f"The clone is the deployed commit: yard app deploy --app {name}",
        )
    return path.read_text()


async def _resolve_sha(config: Config, name: str, prefix: str) -> str:
    """A short sha names one deployed commit, found in the deploy journal."""
    try:
        entries = await journal.read_entries(
            config,
            ["SYSLOG_IDENTIFIER=yard", f"YARD_APP={name}", "YARD_EVENT=deploy.start"],
            lines=5000,
        )
    except YardError as exc:
        raise YardError(
            exc.status,
            exc.code,
            exc.message,
            help_text="pass the full 40-character SHA",
        ) from exc
    matches = sorted(
        {str(entry.get("YARD_SHA")) for entry in entries if str(entry.get("YARD_SHA") or "").startswith(prefix)}
    )
    if not matches:
        raise YardError(
            404,
            "unknown_sha",
            f"{prefix} was never deployed here; pass the full 40-character SHA",
        )
    if len(matches) > 1:
        raise YardError(
            400,
            "ambiguous_sha",
            f"{prefix} names {len(matches)} deployed shas",
            context={"matches": ",".join(matches)},
        )
    return matches[0]


_LOG_CONTAINER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_LOG_ABSOLUTE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?\Z")
_LOG_RELATIVE = re.compile(r"-(\d+)(s|m|h|d)\Z")
# journalctl reads -1h and -2d as they are; a bare m means months, so it maps.
_LOG_UNITS = {"s": "s", "m": "min", "h": "h", "d": "d"}
# Podman writes container events with SYSLOG_IDENTIFIER=podman; Yard's own events use yard.
_EVENT_MATCHES = ["SYSLOG_IDENTIFIER=podman", "SYSLOG_IDENTIFIER=yard"]
_YARD_TOP = {"YARD_EVENT", "YARD_APP", "YARD_CONTAINER", "YARD_ACTOR", "YARD_SHA", "YARD_STATUS"}


def logs_get(config: Config) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        """Container logs from the journal; they outlive the container.

        after is a journald cursor: only entries after it are returned, first
        `lines` of them, so a poller resumes where it left off. An Accept
        header that names application/json answers
        {"entries": [...], "cursor": ...} instead of text lines.
        """
        query = c.req.query
        lines = _log_lines(str(query.get("lines") or ""))
        since = _log_time(str(query.get("since") or ""))
        until = _log_time(str(query.get("until") or ""))
        after = str(query.get("after") or "")
        match = _container_match(str(query.get("container") or ""))
        if query.get("follow") == "1":
            await _stream_journal(config, c, w, [match], lines)
            return
        if "application/json" in (c.req.headers.get("accept") or ""):
            entries = await journal.read_entries(config, [match], lines=lines, since=since, until=until, after=after)
            w.json(cast(JsonValue, _logs_page(entries)))
            return
        text = await journal.read(config, [match], lines=lines, since=since, until=until, after=after)
        w.text(text if text.endswith("\n") or not text else text + "\n")

    return handler


def _logs_page(entries: list[dict]) -> dict[str, Any]:
    """One page of log entries and the cursor a poller passes back as after."""
    rows = [
        {
            "cursor": str(entry.get("__CURSOR") or ""),
            "container": str(entry.get("CONTAINER_NAME") or ""),
            "time": journal.timestamp(entry),
            "message": journal.message_text(entry),
        }
        for entry in entries
    ]
    return {"entries": rows, "cursor": rows[-1]["cursor"] if rows else ""}


def _container_match(given: str) -> str:
    """A name is CONTAINER_NAME; 12 or 64 hex picks the short or full id."""
    if re.fullmatch(r"[0-9a-fA-F]{64}", given):
        return f"CONTAINER_ID_FULL={given}"
    if re.fullmatch(r"[0-9a-fA-F]{12}", given):
        return f"CONTAINER_ID={given}"
    if _LOG_CONTAINER.fullmatch(given):
        return f"CONTAINER_NAME={given}"
    raise YardError(
        400,
        "bad_container",
        "bad container",
        help_text="A container name, a 12-char id, or a full id.",
    )


def events_get(config: Config) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        """One merged timeline of Podman and Yard events."""
        query = c.req.query
        lines = _event_lines(str(query.get("lines") or ""))
        since = _log_time(str(query.get("since") or "")) or "-1d"
        until = _log_time(str(query.get("until") or ""))
        want = (
            str(query.get("app") or ""),
            str(query.get("container") or ""),
            str(query.get("source") or ""),
            str(query.get("event") or ""),
        )
        if query.get("follow") == "1":
            await _stream_events(config, c, w, lines, since, want)
            return
        entries = await journal.read_entries(
            config,
            _EVENT_MATCHES,
            lines=50_000,
            since=since,
            until=until,
        )
        rows = [row for entry in entries if (row := _event_row(entry)) and _event_keeps(row, *want)]
        w.json(cast(JsonValue, rows[-lines:]))

    return handler


def _event_lines(value: str) -> int:
    if not value:
        return 200
    try:
        lines = int(value)
    except ValueError:
        lines = 0
    if not 1 <= lines <= 5_000:
        raise YardError(400, "bad_lines", "bad lines", help_text="lines is 1 to 5000.")
    return lines


def _event_row(entry: dict) -> dict | None:
    """One journald entry as a public event, or None when it carries no event."""
    when = journal.timestamp(entry)
    source = str(entry.get("SYSLOG_IDENTIFIER") or "")
    if source == "podman":
        if str(entry.get("PODMAN_TYPE") or "") != "container":
            return None
        container = str(entry.get("PODMAN_NAME") or "")
        app = ""
        labels = entry.get("PODMAN_LABELS")
        if isinstance(labels, str):
            try:
                labels = json.loads(labels)
            except ValueError:
                labels = None
        if isinstance(labels, dict):
            app = str(labels.get("com.docker.compose.project") or "")
        if not app and container.startswith(("deploy-", "up-")):
            # The apply container is not on a compose project.
            app = container.split("-", 1)[1].removesuffix("-job")
        row: dict[str, Any] = {
            "time": when,
            "source": "podman",
            "event": str(entry.get("PODMAN_EVENT") or ""),
            "app": app,
            "container": container,
            "container_id": str(entry.get("PODMAN_ID") or ""),
            "image": str(entry.get("PODMAN_IMAGE") or ""),
            "exit_code": str(entry.get("PODMAN_EXIT_CODE") or ""),
            "oom": str(entry.get("PODMAN_OOM_KILLED") or "").lower() in {"true", "1"},
            "message": journal.message_text(entry),
        }
    elif source == "yard":
        fields = {
            key[5:].lower(): str(value)
            for key, value in entry.items()
            if key.startswith("YARD_") and key not in _YARD_TOP and str(value)
        }
        row = {
            "time": when,
            "source": "yard",
            "event": str(entry.get("YARD_EVENT") or ""),
            "app": str(entry.get("YARD_APP") or ""),
            "container": str(entry.get("YARD_CONTAINER") or ""),
            "actor": str(entry.get("YARD_ACTOR") or ""),
            "sha": str(entry.get("YARD_SHA") or ""),
            "status": str(entry.get("YARD_STATUS") or ""),
            "message": journal.message_text(entry),
            "fields": fields,
        }
    else:
        return None
    return {key: value for key, value in row.items() if value not in ("", {}, False, None)}


def _event_keeps(row: dict, app: str, container: str, source: str, event: str) -> bool:
    if source and row.get("source") != source:
        return False
    if app and row.get("app") != app:
        return False
    if container and (
        row.get("container") != container and not str(row.get("container_id") or "").startswith(container)
    ):
        return False
    if event and not str(row.get("event") or "").startswith(event):
        return False
    return True


def _log_lines(value: str) -> int:
    if not value:
        return 100
    try:
        lines = int(value)
    except ValueError:
        lines = 0
    if not 1 <= lines <= 10_000:
        raise YardError(400, "bad_lines", "bad lines", help_text="lines is 1 to 10000.")
    return lines


def _log_time(value: str) -> str:
    if not value:
        return ""
    found = _LOG_RELATIVE.fullmatch(value)
    if found:
        return f"-{found.group(1)}{_LOG_UNITS[found.group(2)]}"
    if _LOG_ABSOLUTE.fullmatch(value):
        return value
    raise YardError(
        400,
        "bad_time",
        "bad time",
        help_text='since/until: -15m, -1h, -2d, or "2025-01-31 14:00".',
        context={"value": value},
    )


async def _stream_journal(config: Config, c: Context, w: Writer, matches: list[str], lines: int) -> None:
    proc = await journal.follow(config, matches, lines=lines)
    w.headers.set("content-type", "text/plain; charset=utf-8")
    w.write_headers(200)
    assert proc.stdout is not None
    try:
        while raw := await proc.stdout.readline():
            if c.disconnected:
                break
            for entry in journal.entries(raw):
                if line := journal.format_line(entry):
                    w.write(line.encode() + b"\n")
                    await w.drain()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    w.end()


async def _stream_events(
    config: Config,
    c: Context,
    w: Writer,
    lines: int,
    since: str,
    want: tuple[str, str, str, str],
) -> None:
    proc = await journal.follow(config, _EVENT_MATCHES, lines=lines, since=since)
    w.headers.set("content-type", "application/x-ndjson")
    w.write_headers(200)
    assert proc.stdout is not None
    try:
        while raw := await proc.stdout.readline():
            if c.disconnected:
                break
            for entry in journal.entries(raw):
                row = _event_row(entry)
                if row and _event_keeps(row, *want):
                    w.write(json.dumps(row).encode() + b"\n")
                    await w.drain()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    w.end()


def env_show(store: Store) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        store.app(name)
        w.json(cast(JsonValue, store.read_env(name)))

    return handler


def _env_set_event(config: Config, c: Context, name: str, keys: list[str], removed: list[str]) -> None:
    # Key names only; a value never reaches the journal.
    journal.send(
        config,
        "env.set",
        f"{name} env",
        app=name,
        keys=",".join(keys),
        removed=",".join(removed),
        actor=_actor(c),
    )


def env_patch(store: Store, config: Config) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        store.app(name)
        body = await json_object(c)
        patch: dict[str, str | None] = {}
        for key, value in body.items():
            if not isinstance(key, str) or (value is not None and not isinstance(value, str)):
                raise YardError(
                    400,
                    "bad_json",
                    "bad json",
                    help_text="env is an object of strings or null.",
                    example='{"KEY": null}',
                )
            patch[key] = value
        keys = store.patch_env(name, patch)
        _env_set_event(
            config,
            c,
            name,
            sorted(key for key, value in patch.items() if value is not None),
            sorted(key for key, value in patch.items() if value is None),
        )
        w.json({"keys": keys})

    return handler


def env_put(store: Store, config: Config) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        store.app(name)
        body = await json_object(c)
        env: dict[str, str] = {}
        for key, value in body.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise YardError(
                    400,
                    "bad_json",
                    "bad json",
                    help_text="env is an object of strings.",
                    example='{"KEY": "value"}',
                )
            env[key] = value
        removed = sorted(set(store.read_env(name)) - set(env))
        keys = store.write_env(name, env)
        _env_set_event(config, c, name, sorted(env), removed)
        w.json({"keys": keys})

    return handler


def binds_list(yard: Yard) -> Handler:
    async def handler(_c: Context, w: Writer) -> None:
        w.json(cast(JsonValue, [bind_json(bind) for bind in await binds.list_binds(yard)]))

    return handler


def binds_put(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        data = await json_object(c)
        dial, window = data.get("dial", ""), data.get("try", "10s")
        if not isinstance(dial, str) or not isinstance(window, str):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="dial is a string. try is a duration.",
                example='{"dial": "heldso-app-1"}',
            )
        host = c.match.params["host"]
        w.json(cast(JsonValue, bind_json(await binds.put_bind(yard, host, dial, window, actor=_actor(c)))))

    return handler


def binds_delete(yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        host = await binds.delete_bind(yard, c.match.params["host"], actor=_actor(c))
        w.json({"host": host})

    return handler


def tokens_list(store: Store) -> Handler:
    async def handler(_c: Context, w: Writer) -> None:
        w.json([asdict(token) for token in store.list_tokens()])

    return handler


def tokens_create(store: Store, config: Config) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        data = await json_object(c)
        name = data.get("name", "")
        if not isinstance(name, str) or not name:
            raise YardError(400, "bad_json", "bad json", help_text="name is a string.")
        plain = store.create_token(name)
        journal.send(config, "token.create", f"token {name}", name=name, actor=_actor(c))
        w.json({"name": name, "token": plain})

    return handler


def tokens_delete(store: Store, config: Config) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        actor = _actor(c)
        store.delete_token(name, actor=actor)
        journal.send(config, "token.delete", f"token {name}", name=name, actor=actor)
        w.json({"name": name})

    return handler


async def _error_body(_c: Context, w: Writer, exc: BaseException) -> None:
    err = cast(YardError, exc)
    w.json({"error": err.payload()}, err.status)


def bearer(store: Store) -> Middleware:
    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            header = c.req.headers.get("authorization") or ""
            raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
            token = store.token_ok(raw)
            if token is None:
                w.json(
                    {
                        "error": YardError(
                            401,
                            "unauthorized",
                            "unauthorized",
                            help_text="Pass a live token, or run yard login.",
                            example="Authorization: Bearer <token>",
                        ).payload()
                    },
                    401,
                )
                return
            c.state["actor"] = token.name
            await handler(c, w)

        return wrapped

    return middleware


def register(app: StarioApp, yard: Yard) -> None:
    auth = (bearer(yard.store),)
    # YardError is the API's failure shape on every route. Uncaught exceptions
    # fall through to the framework's own 500.
    app.use("/", catch_errors(YardError, respond=_error_body))
    origin = next(forge for forge in yard.forges.values() if isinstance(forge, Origin))
    github = next(forge for forge in yard.forges.values() if isinstance(forge, GitHub))
    app.add(HEALTH, health)
    app.add(HOOK_ORIGIN, origin_hook(origin, yard))
    app.add(HOOK_GITHUB, github_hook(github, yard))
    app.add(STATUS, host_status(yard), middleware=auth)
    app.add(LOGS, logs_get(yard.config), middleware=auth)
    app.add(EVENTS, events_get(yard.config), middleware=auth)
    app.add(DOCTOR, doctor_run(yard), middleware=auth)
    app.add(FORGES, forges_list(yard.forges, yard.config), middleware=auth)
    app.add(FORGE_PUT, forge_put(yard.forges, yard.config), middleware=auth)
    app.add(APPS, apps_list(yard.store), middleware=auth)
    app.add(APP_CREATE, apps_create(yard), middleware=auth)
    app.add(APP, app_page(yard), middleware=auth)
    app.add(APP_PATCH, app_patch(yard), middleware=auth)
    app.add(APP_DELETE, app_delete(yard), middleware=auth)
    app.add(APP_DEPLOY, app_deploy(yard), middleware=auth)
    app.add(APP_FILE, app_file(yard), middleware=auth)
    app.add(ENV, env_show(yard.store), middleware=auth)
    app.add(ENV_PATCH, env_patch(yard.store, yard.config), middleware=auth)
    app.add(ENV_PUT, env_put(yard.store, yard.config), middleware=auth)
    app.add(BINDS, binds_list(yard), middleware=auth)
    app.add(BIND_PUT, binds_put(yard), middleware=auth)
    app.add(BIND_DELETE, binds_delete(yard), middleware=auth)
    app.add(PRUNE, run_prune(yard), middleware=auth)
    app.add(TOKENS, tokens_list(yard.store), middleware=auth)
    app.add(TOKEN_CREATE, tokens_create(yard.store, yard.config), middleware=auth)
    app.add(TOKEN_DELETE, tokens_delete(yard.store, yard.config), middleware=auth)
