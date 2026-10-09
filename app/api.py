"""JSON API. Git smart HTTP sits beside it and uses basic auth."""

import json
from dataclasses import asdict
from typing import cast

import stario.responses as responses
from stario import App, Context, Handler, Middleware, Route, Writer
from stario.responses import JsonValue

from app.errors import YardError
from app.podman import inspect_args
from app.store import Experiment, Label
from app.text import apply_slot_keys, slot_values
from app.yard import Yard


def register_api(app: App, yard: Yard) -> None:
    async def health(_c: Context, w: Writer) -> None:
        responses.empty(w, 200)

    app.add(Route("GET /health"), health)
    app.add(Route("GET /git/{path...}"), yard.serve_git)
    app.add(Route("POST /git/{path...}"), yard.serve_git)

    auth = (_bearer(yard),)

    async def repos_list(_c: Context, w: Writer) -> None:
        responses.json(w, [asdict(repo) for repo in yard.store.list_repos()])

    async def repos_create(c: Context, w: Writer) -> None:
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
        responses.json(w, await yard.create_repo(name, force, origin))

    async def repos_delete(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        yard.delete_repo(name)
        responses.json(w, {"name": name})

    app.add(Route("GET /api/v1/repos"), repos_list, middleware=auth)

    async def repos_git(c: Context, w: Writer) -> None:
        body = await _object(c)
        args = body.get("args")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="args is a list of strings.",
                example='{"args": ["log", "-1"]}',
            )
        responses.json(w, await yard.git_repo(c.match.params["name"], args))

    app.add(Route("POST /api/v1/repos"), repos_create, middleware=auth)
    app.add(Route("POST /api/v1/repos/{name}/git"), repos_git, middleware=auth)
    app.add(Route("DELETE /api/v1/repos/{name}"), repos_delete, middleware=auth)

    async def slots_list(c: Context, w: Writer) -> None:
        responses.json(w, await yard.list_rows(c.req.query.get("running") == "1"))

    async def slots_create(c: Context, w: Writer) -> None:
        slot, warn, note = await yard.create_slot(_string_map(await _object(c)))
        responses.json(w, cast(JsonValue, {"values": slot_values(slot), "warnings": warn, "deploy": note}))

    async def slots_get(c: Context, w: Writer) -> None:
        responses.json(w, cast(JsonValue, slot_values(yard.store.slot(c.match.params["name"]))))

    async def slots_put(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        edit = apply_slot_keys(yard.store.slot(name), _string_map(await _object(c)))
        slot, note = await yard.update_slot(name, edit)
        body: dict[str, JsonValue] = {"values": slot_values(slot), "warnings": edit.warn}
        if note is not None:
            body["deploy"] = note
        responses.json(w, cast(JsonValue, body))

    async def slots_delete(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        await yard.delete_slot(name)
        responses.json(w, {"name": name})

    async def logs(c: Context, w: Writer) -> None:
        slot, role, service, sha, tail, cursor = _log_filters(c)
        if c.req.query.get("follow") == "1":
            await _follow(yard, w, slot, role, service, sha, tail, cursor)
            return
        responses.json(w, await yard.log_page(slot, role, service, sha, tail, cursor))

    async def container_logs(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        tail, cursor = _tail(c), c.req.query.get("cursor") or ""
        if c.req.query.get("follow") == "1":
            inspect_args(name)
            await _stream_logs(yard, w, _follow_args(name, tail, cursor))
            return
        responses.json(w, await yard.container_log_page(name, tail, cursor))

    async def slot_deploy(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        note = await yard.deploy_existing(yard.store.slot(name))
        responses.json(w, cast(JsonValue, note))

    async def deploys(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        yard.store.slot(name)
        limit = c.req.query.get("limit") or "0"
        rows = yard.store.list_deploys(name, int(limit) if limit.isdigit() else 0)
        responses.json(w, [asdict(row) for row in rows])

    async def deploy(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        yard.store.slot(name)
        raw = c.match.params["id"]
        if not raw.isdigit():
            raise YardError(
                400,
                "bad_id",
                "deploy id must be a number",
                help_text="The id is the number from yard deploys <name>.",
            )

        row = yard.store.deploy(int(raw))
        if row.slot != name:
            raise YardError(404, "not_found", "deploy does not exist")
        responses.json(w, asdict(row))

    async def inspect(c: Context, w: Writer) -> None:
        text = await yard.podman.run(*inspect_args(c.match.params["name"]))
        if text and not text.endswith("\n"):
            text += "\n"
        responses.text(w, text)

    app.add(Route("GET /api/v1/slots"), slots_list, middleware=auth)
    app.add(Route("POST /api/v1/slots"), slots_create, middleware=auth)
    app.add(Route("GET /api/v1/slots/{name}"), slots_get, middleware=auth)
    app.add(Route("PUT /api/v1/slots/{name}"), slots_put, middleware=auth)
    app.add(Route("DELETE /api/v1/slots/{name}"), slots_delete, middleware=auth)
    app.add(Route("POST /api/v1/slots/{name}/deploy"), slot_deploy, middleware=auth)
    app.add(Route("GET /api/v1/logs"), logs, middleware=auth)
    app.add(Route("GET /api/v1/containers/{name}/logs"), container_logs, middleware=auth)
    app.add(Route("GET /api/v1/slots/{name}/deploys"), deploys, middleware=auth)
    app.add(Route("GET /api/v1/slots/{name}/deploys/{id}"), deploy, middleware=auth)
    app.add(Route("GET /api/v1/containers/{name}"), inspect, middleware=auth)

    async def tokens_list(_c: Context, w: Writer) -> None:
        responses.json(w, [asdict(token) for token in yard.store.list_tokens()])

    async def tokens_create(c: Context, w: Writer) -> None:
        body = await _object(c)
        name = body.get("name", "")
        if not isinstance(name, str):
            raise YardError(400, "bad_json", "bad json", help_text="name is a string.")
        actor = str(c.state.get("actor") or "")
        plain = yard.store.create_token(name, actor)
        responses.json(w, {"name": name, "token": plain, "parent": actor})

    async def tokens_delete(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        yard.store.revoke_token(str(c.state.get("actor") or ""), name)
        responses.json(w, {"name": name})

    app.add(Route("GET /api/v1/tokens"), tokens_list, middleware=auth)
    app.add(Route("POST /api/v1/tokens"), tokens_create, middleware=auth)
    app.add(Route("DELETE /api/v1/tokens/{name}"), tokens_delete, middleware=auth)

    async def experiments_list(c: Context, w: Writer) -> None:
        rows = _filter_exps(yard.store.list_experiments(), c.req.query.items())
        responses.json(w, [asdict(row) for row in rows])

    async def experiments_create(c: Context, w: Writer) -> None:
        title, body, labels = await _exp_body(c)
        saved = yard.store.save_experiments([(False, 0, title, body, labels)], [])
        responses.json(w, asdict(saved[0]))

    async def experiments_get(c: Context, w: Writer) -> None:
        responses.json(w, asdict(_one_exp(yard, c.match.params["id"])))

    async def experiments_put(c: Context, w: Writer) -> None:
        exp = _one_exp(yard, c.match.params["id"])
        title, body, labels = await _exp_body(c)
        saved = yard.store.save_experiments([(True, exp.id, title, body, labels)], [])
        responses.json(w, asdict(saved[0]))

    async def experiments_delete(c: Context, w: Writer) -> None:
        exp = _one_exp(yard, c.match.params["id"])
        yard.store.save_experiments([], [exp.id])
        responses.json(w, {"id": exp.id})

    app.add(Route("GET /api/v1/experiments"), experiments_list, middleware=auth)
    app.add(Route("POST /api/v1/experiments"), experiments_create, middleware=auth)
    app.add(Route("GET /api/v1/experiments/{id}"), experiments_get, middleware=auth)
    app.add(Route("PUT /api/v1/experiments/{id}"), experiments_put, middleware=auth)
    app.add(Route("DELETE /api/v1/experiments/{id}"), experiments_delete, middleware=auth)

    async def hook(c: Context, w: Writer) -> None:
        body = await _object(c)
        repo, new, ref = body.get("repo", ""), body.get("new", ""), body.get("ref", "")
        if not all(isinstance(item, str) for item in (repo, new, ref)):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="repo, new, and ref are strings.",
            )
        status, text = await yard.hook_push(repo, new, ref)
        responses.text(w, text, status)

    app.add(Route("POST /api/v1/hooks/push"), hook, middleware=(_local(), *auth))


def _local() -> Middleware:
    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            if c.req.host not in {"127.0.0.1", "localhost", "::1"}:
                responses.json(
                    w,
                    {
                        "error": YardError(
                            403,
                            "local_only",
                            "the hook accepts a request from this host",
                            help_text="The git hook calls 127.0.0.1.",
                        ).payload()
                    },
                    403,
                )
                return
            await handler(c, w)

        return wrapped

    return middleware


def _bearer(yard: Yard) -> Middleware:
    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            header = c.req.headers.get("authorization") or ""
            raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
            try:
                token = yard.store.token_ok(raw)
            except YardError as exc:
                responses.json(w, {"error": exc.payload()}, exc.status)
                return
            if token is None:
                responses.json(
                    w,
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
            try:
                await handler(c, w)
            except YardError as exc:
                # The writer already sent bytes. A second body would corrupt that response.
                if not w.started:
                    responses.json(w, {"error": exc.payload()}, exc.status)
            except Exception as exc:
                if not w.started:
                    responses.json(w, {"error": {"code": "internal", "message": str(exc)}}, 500)
                else:
                    raise

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


def _log_filters(c: Context) -> tuple[str, str, str, str, int, str]:
    role = c.req.query.get("role") or ""
    if role and role not in {"app", "deploy", "revert", "job"}:
        raise YardError(
            400,
            "bad_role",
            "role must be app, deploy, revert, or job",
            help_text="An empty role reads app.",
            example="yard logs --role deploy",
        )
    return (
        c.req.query.get("slot") or "",
        role,
        c.req.query.get("service") or "",
        c.req.query.get("sha") or "",
        _tail(c),
        c.req.query.get("cursor") or "",
    )


async def _follow(
    yard: Yard,
    w: Writer,
    slot: str,
    role: str,
    service: str,
    sha: str,
    tail: int,
    cursor: str,
) -> None:
    targets = await yard.log_targets(slot, role, service, sha)
    if len(targets) != 1:
        raise YardError(
            400,
            "many_services",
            "name one container to follow" if targets else "no container matches",
            help_text="Add --slot, --service, or --sha until one container matches.",
            example="yard logs --slot heldso --follow",
        )
    await _stream_logs(yard, w, _follow_args(targets[0][1], tail, cursor))


def _follow_args(name: str, tail: int, cursor: str) -> list[str]:
    args = ["logs", "--follow", "--tail", str(tail)]
    if cursor:
        args.extend(("--since", cursor))
    args.append(name)
    return args


async def _stream_logs(yard: Yard, w: Writer, args: list[str]) -> None:
    proc = await yard.podman.popen(*args)
    w.headers.set("content-type", "text/plain; charset=utf-8")
    w.write_headers(200)
    assert proc.stdout is not None
    try:
        while chunk := await proc.stdout.read(8192):
            w.write(chunk)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    w.end()


async def _object(c: Context) -> dict:
    raw = await c.req.body()
    try:
        body = json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.") from exc
    if not isinstance(body, dict):
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.")
    return body


def _string_map(body: dict) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in body.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise YardError(400, "bad_json", "bad json", help_text="Every slot field is a string.")
        found[key] = value
    return found


def _one_exp(yard: Yard, raw: str) -> Experiment:
    if not raw.isdigit() or int(raw) <= 0:
        raise YardError(
            400,
            "bad_id",
            "experiment id must be a number",
            help_text="The id is the number from yard exp.",
        )
    exp_id = int(raw)
    for exp in yard.store.list_experiments():
        if exp.id == exp_id:
            return exp
    raise YardError(
        404,
        "not_found",
        "experiment does not exist",
        context={"id": exp_id},
        help_text="Run yard exp to see the notes on this host.",
    )


async def _exp_body(c: Context) -> tuple[str, str, list[Label]]:
    body = await _object(c)
    title = body.get("title", "")
    text = body.get("body", "")
    labels = body.get("labels", [])
    if not isinstance(title, str) or not isinstance(text, str) or not isinstance(labels, list):
        raise YardError(
            400,
            "bad_json",
            "bad json",
            help_text="title and body are strings. labels is a list of {key, value}.",
        )
    if not title.strip():
        raise YardError(400, "bad_title", "a title is required", help_text="The first line of a note is a title.")
    parsed: list[Label] = []
    for item in labels:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not isinstance(item.get("value"), str):
            raise YardError(400, "bad_json", "bad json", help_text="A label is {key, value}, and both are strings.")
        parsed.append(Label(item["key"], item["value"]))
    return title.strip(), text, parsed


def _filter_exps(rows: list[Experiment], pairs: list[tuple[str, str]]) -> list[Experiment]:
    want: dict[str, list[str]] = {}
    for key, value in pairs:
        if key == "id" or not value:
            continue
        want.setdefault(key, []).append(value)
    if not want:
        return rows

    def matches(exp: Experiment) -> bool:
        for key, values in want.items():
            for value in values:
                if not any(label.key == key and label.value == value for label in exp.labels):
                    return False
        return True

    return [row for row in rows if matches(row)]
