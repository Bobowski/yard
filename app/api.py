"""The public HTTP API. Git smart HTTP sits beside it and uses basic auth."""

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, cast

from stario import App, Context, Handler, Middleware, Route, Writer
from stario.http.middleware import catch_errors
from stario.json import JsonValue

from app import doctor, git, podman, prune, status
from app.errors import YardError
from app.store import Slot
from app.values import check_slot_name
from app.yard import Yard


async def health(_yard: Yard, _c: Context, w: Writer) -> None:
    w.empty(200)


async def serve_git(yard: Yard, c: Context, w: Writer) -> None:
    password = _basic_password(c.req.headers.get("authorization") or "") or ""
    token = yard.store.token_ok(password)
    if token is None:
        w.headers.set("www-authenticate", 'Basic realm="yard"')
        w.text("unauthorized\n", 401)
        return
    await git.http_backend(yard.config.root, token.name, c, w)


async def repos_list(yard: Yard, _c: Context, w: Writer) -> None:
    w.json([asdict(repo) for repo in yard.store.list_repos()])


async def repos_create(yard: Yard, c: Context, w: Writer) -> None:
    body = await _object(c)
    name = body.get("name", "")
    force = body.get("force", False)
    origin = body.get("origin", "")
    if not isinstance(name, str) or not isinstance(force, bool) or not isinstance(origin, str):
        raise YardError(
            400,
            "bad_json",
            "bad json",
            help_text="name is a string. force is true or false. origin is an http address.",
        )
    w.json(await yard.create_repo(name, force, origin))


async def repos_delete(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    yard.delete_repo(name)
    w.json({"name": name})


async def repos_git(yard: Yard, c: Context, w: Writer) -> None:
    body = await _object(c)
    w.json(await yard.git_repo(c.match.params["name"], _words(body)))


async def routes_list(yard: Yard, _c: Context, w: Writer) -> None:
    w.json(cast(JsonValue, await yard.route_list()))


async def routes_put(yard: Yard, c: Context, w: Writer) -> None:
    body = await _object(c)
    host, dial, window = body.get("host", ""), body.get("dial", ""), body.get("try", "10s")
    if not isinstance(host, str) or not isinstance(dial, str) or not isinstance(window, str):
        raise YardError(
            400,
            "bad_json",
            "bad json",
            help_text="host and dial are strings. try is a duration.",
            example='{"host": "heldso.example.com", "dial": "heldso-app"}',
        )
    w.json(cast(JsonValue, await yard.route_put(host, dial, window)))


async def routes_delete(yard: Yard, c: Context, w: Writer) -> None:
    w.json(cast(JsonValue, await yard.route_delete(c.match.params["name"])))


async def caddy_command(yard: Yard, c: Context, w: Writer) -> None:
    w.json(cast(JsonValue, await yard.caddy_cli(_words(await _object(c)))))


async def podman_command(yard: Yard, c: Context, w: Writer) -> None:
    w.json(cast(JsonValue, await yard.podman_command(_words(await _object(c)))))


async def host_status(yard: Yard, c: Context, w: Writer) -> None:
    w.json(await status.report(yard, c.req.query.get("running") == "1"))


async def slots_list(yard: Yard, _c: Context, w: Writer) -> None:
    # The list carries no env; secrets show only on the single GET.
    w.json([status.slot_json(slot) for slot in yard.store.list_slots()])


def _slot_with_env(yard: Yard, slot: Slot) -> dict[str, Any]:
    return {**status.slot_json(slot), "env": yard.read_env(slot.name)}


async def slots_create(yard: Yard, c: Context, w: Writer) -> None:
    body = await _object(c)
    fields = _slot_fields(body)
    name = body.get("name", "")
    repo = body.get("repo", "")
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
            help_text="A project name uses lowercase letters, digits, and -",
            example="staging",
        )
    if not repo.strip():
        raise YardError(
            400,
            "bad_json",
            "repo is required",
            context={"field": "repo"},
            help_text="Run yard repo ls for the names.",
        )
    slot = await yard.create_slot(name, repo, **cast(dict[str, Any], fields))
    w.json(cast(JsonValue, {"project": _slot_with_env(yard, slot)}))


async def slots_get(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    w.json(cast(JsonValue, _slot_with_env(yard, yard.store.slot(name))))


async def slots_patch(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    fields = _slot_fields(await _object(c))
    if not fields:
        raise YardError(
            400,
            "bad_json",
            "nothing to change",
            help_text="Set one of ref, dir, trigger, or env.",
        )
    slot = await yard.update_slot(name, **cast(dict[str, Any], fields))
    w.json(cast(JsonValue, {"project": _slot_with_env(yard, slot)}))


async def slots_delete(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    await yard.delete_slot(name)
    w.json({"name": name})


async def slot_run(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    w.json(cast(JsonValue, {"output": await yard.run_slot(name)}))


async def slot_deploy(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    slot = yard.store.slot(name)
    body = await _object(c)
    sha = body.get("sha", "")
    if not isinstance(sha, str):
        raise YardError(400, "bad_json", "bad json", help_text="sha is a string.")
    outcome = await yard.deploy_ref(slot, sha)
    w.json(
        cast(
            JsonValue,
            {
                "project": _slot_with_env(yard, yard.store.slot(name)),
                "deploy": asdict(outcome),
            },
        ),
    )


async def deploys(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    yard.store.slot(name)
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    row = status.up_record(listed, name)
    w.json(cast(JsonValue, [row] if row else []))


async def deploy(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    yard.store.slot(name)
    raw = c.match.params["id"]
    if not raw.isdigit():
        raise YardError(
            400,
            "bad_id",
            "deploy id must be a number",
            help_text="The id is the number on the status page.",
        )
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    row = status.up_record(listed, name)
    if row is None or int(raw) != 1:
        raise YardError(
            404,
            "not_found",
            "deploy does not exist",
            context={"id": raw},
            help_text="Run yard status <name> to see the latest up.",
        )
    w.json(cast(JsonValue, row))


async def logs(yard: Yard, c: Context, w: Writer) -> None:
    slot, role, service, tail, cursor = _log_filters(c)
    if c.req.query.get("follow") == "1":
        await _follow(yard, c, w, slot, role, service, tail, cursor)
        return
    w.json(await status.log_page(yard, slot, role, service, tail, cursor))


async def container_logs(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    tail, cursor = _tail(c), c.req.query.get("cursor") or ""
    podman.check_name(name)
    if c.req.query.get("follow") == "1":
        await _stream_logs(yard, c, w, _follow_args(name, tail, cursor))
        return
    w.json(await status.log_text(yard, [("", name)], tail, cursor))


async def inspect(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    podman.check_name(name)
    text = await yard.podman.run("inspect", "--format", "json", name)
    if text and not text.endswith("\n"):
        text += "\n"
    w.text(text)


async def run_prune(yard: Yard, c: Context, w: Writer) -> None:
    body = await _object(c)
    days = body.get("days", 7)
    dry = body.get("dry", False)
    if not isinstance(days, int) or isinstance(days, bool) or days < 1 or not isinstance(dry, bool):
        raise YardError(
            400,
            "bad_json",
            "bad json",
            help_text="days is a number 1 or more. dry is true or false.",
            example='{"days": 7, "dry": false}',
        )
    w.json(cast(JsonValue, await prune.run(yard, days, dry)))


async def slots_status(yard: Yard, c: Context, w: Writer) -> None:
    w.json(await status.slot_report(yard, c.match.params["name"]))


async def doctor_run(yard: Yard, _c: Context, w: Writer) -> None:
    w.json(cast(JsonValue, await doctor.run(yard)))


async def tokens_list(yard: Yard, _c: Context, w: Writer) -> None:
    w.json([asdict(token) for token in yard.store.list_tokens()])


async def tokens_create(yard: Yard, c: Context, w: Writer) -> None:
    body = await _object(c)
    name = body.get("name", "")
    if not isinstance(name, str) or not name:
        raise YardError(400, "bad_json", "bad json", help_text="name is a string.")
    plain = yard.store.create_token(name)
    w.json({"name": name, "token": plain})


async def tokens_delete(yard: Yard, c: Context, w: Writer) -> None:
    name = c.match.params["name"]
    actor = str(c.state.get("actor") or "")
    if actor == name:
        raise YardError(
            403,
            "forbidden",
            "you cannot delete your own token",
            context={"name": name},
            help_text="Use another token to remove this one.",
        )
    yard.store.delete_token(name)
    w.json({"name": name})


async def hook(yard: Yard, c: Context, w: Writer) -> None:
    body = await _object(c)
    repo, new, ref = body.get("repo", ""), body.get("new", ""), body.get("ref", "")
    if not all(isinstance(item, str) for item in (repo, new, ref)):
        raise YardError(
            400,
            "bad_json",
            "bad json",
            help_text="repo, new, and ref are strings.",
        )
    code, text = await yard.hook_push(repo, new, ref)
    w.text(text, code)


def _h(fn: Callable[[Yard, Context, Writer], Awaitable[None]], yard: Yard) -> Handler:
    async def handler(c: Context, w: Writer) -> None:
        await fn(yard, c, w)

    return handler


async def _error_body(_c: Context, w: Writer, exc: BaseException) -> None:
    err = cast(YardError, exc)
    w.json({"error": err.payload()}, err.status)


def register_api(app: App, yard: Yard) -> None:
    auth = (_bearer(yard),)
    # YardError is the API's failure shape on every route. Uncaught exceptions
    # fall through to the framework's own 500.
    app.use("/", catch_errors(YardError, respond=_error_body))
    routes = [
        (Route("GET /health"), health, ()),
        (Route("GET /git/{path...}"), serve_git, ()),
        (Route("POST /git/{path...}"), serve_git, ()),
        (Route("GET /api/v1/repos"), repos_list, auth),
        (Route("POST /api/v1/repos"), repos_create, auth),
        (Route("DELETE /api/v1/repos/{name}"), repos_delete, auth),
        (Route("POST /api/v1/repos/{name}/git"), repos_git, auth),
        (Route("GET /api/v1/status"), host_status, auth),
        (Route("GET /api/v1/projects"), slots_list, auth),
        (Route("POST /api/v1/projects"), slots_create, auth),
        (Route("GET /api/v1/doctor"), doctor_run, auth),
        (Route("GET /api/v1/projects/{name}/status"), slots_status, auth),
        (Route("GET /api/v1/projects/{name}"), slots_get, auth),
        (Route("PATCH /api/v1/projects/{name}"), slots_patch, auth),
        (Route("DELETE /api/v1/projects/{name}"), slots_delete, auth),
        (Route("POST /api/v1/projects/{name}/deploy"), slot_deploy, auth),
        (Route("POST /api/v1/projects/{name}/run"), slot_run, auth),
        (Route("GET /api/v1/logs"), logs, auth),
        (Route("GET /api/v1/containers/{name}/logs"), container_logs, auth),
        (Route("GET /api/v1/projects/{name}/deploys"), deploys, auth),
        (Route("GET /api/v1/projects/{name}/deploys/{id}"), deploy, auth),
        (Route("GET /api/v1/containers/{name}"), inspect, auth),
        (Route("GET /api/v1/binds"), routes_list, auth),
        (Route("PUT /api/v1/binds"), routes_put, auth),
        (Route("DELETE /api/v1/binds/{name}"), routes_delete, auth),
        (Route("POST /api/v1/caddy"), caddy_command, auth),
        (Route("POST /api/v1/podman"), podman_command, auth),
        (Route("POST /api/v1/prune"), run_prune, auth),
        (Route("GET /api/v1/tokens"), tokens_list, auth),
        (Route("POST /api/v1/tokens"), tokens_create, auth),
        (Route("DELETE /api/v1/tokens/{name}"), tokens_delete, auth),
        (Route("POST /api/v1/hooks/push"), hook, (_local(), *auth)),
    ]
    for route, fn, middleware in routes:
        app.add(route, _h(fn, yard), middleware=middleware)


def _local() -> Middleware:
    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            if c.req.host not in {"127.0.0.1", "localhost", "::1"}:
                raise YardError(
                    403,
                    "local_only",
                    "the hook accepts a request from this host",
                    help_text="The git hook calls 127.0.0.1.",
                )
            await handler(c, w)

        return wrapped

    return middleware


def _bearer(yard: Yard) -> Middleware:
    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            header = c.req.headers.get("authorization") or ""
            raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
            token = yard.store.token_ok(raw)
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


def _tail(c: Context) -> int:
    text = c.req.query.get("tail") or "200"
    if not text.isdigit() or int(text) < 1:
        raise YardError(
            400,
            "bad_tail",
            "tail must be a positive number",
            help_text="tail is how many log lines to read.",
            example="200",
        )
    return int(text)


def _log_filters(c: Context) -> tuple[str, str, str, int, str]:
    role = c.req.query.get("role") or ""
    if role and role not in {"app", "deploy", "job"}:
        raise YardError(
            400,
            "bad_role",
            "role must be app, deploy, or job",
            help_text="An empty role reads app.",
            example="yard podman logs up-heldso",
        )
    project = c.req.query.get("project") or ""
    if project:
        check_slot_name(project)
    return (
        project,
        role,
        c.req.query.get("service") or "",
        _tail(c),
        c.req.query.get("cursor") or "",
    )


async def _follow(
    yard: Yard,
    c: Context,
    w: Writer,
    slot: str,
    role: str,
    service: str,
    tail: int,
    cursor: str,
) -> None:
    targets = await status.log_targets(yard, slot, role, service)
    if len(targets) != 1:
        raise YardError(
            400,
            "many_services",
            "name one container to follow" if targets else "no container matches",
            help_text="Name the project or the service until one container matches.",
            example="yard podman logs heldso-app-1",
        )
    await _stream_logs(yard, c, w, _follow_args(targets[0][1], tail, cursor))


def _follow_args(name: str, tail: int, cursor: str) -> list[str]:
    args = ["logs", "--follow", "--tail", str(tail)]
    if cursor:
        args.extend(("--since", cursor))
    args.append(name)
    return args


async def _stream_logs(yard: Yard, c: Context, w: Writer, args: list[str]) -> None:
    proc = await yard.podman.popen(*args)
    w.headers.set("content-type", "text/plain; charset=utf-8")
    w.write_headers(200)
    assert proc.stdout is not None
    try:
        # drain() applies backpressure; disconnected stops the follow early.
        while chunk := await proc.stdout.read(8192):
            if c.disconnected:
                break
            w.write(chunk)
            await w.drain()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    w.end()


async def _object(c: Context) -> dict:
    raw = await c.req.body()
    if not raw or not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.") from exc
    if not isinstance(body, dict):
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.")
    return body


def _words(body: dict) -> list[str]:
    args = body.get("args")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise YardError(
            400,
            "bad_json",
            "bad json",
            help_text="args is a list of strings.",
            example='{"args": ["version"]}',
        )
    return args


def _slot_fields(body: dict) -> dict[str, str | bool | dict[str, str]]:
    """The fields a slot PATCH or POST takes. dir maps to the directory column."""
    fields: dict[str, str | bool | dict[str, str]] = {}
    for key in ("ref", "dir", "trigger"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str):
            raise YardError(400, "bad_json", "bad json", help_text=f"{key} is a string.")
        fields["directory" if key == "dir" else key] = value
    if "env" in body:
        env = body["env"]
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise YardError(400, "bad_json", "bad json", help_text="env is an object of strings.")
        fields["env"] = dict(env)
    return fields


def _basic_password(header: str) -> str | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip()).decode()
    except Exception:
        return None
    _user, sep, password = raw.partition(":")
    return password if sep else None
