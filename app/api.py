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
        if not isinstance(name, str) or not isinstance(force, bool):
            raise YardError(400, "bad_json", "bad json")
        responses.json(w, await yard.create_repo(name, force))

    async def repos_delete(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        yard.delete_repo(name)
        responses.json(w, {"name": name})

    app.add(Route("GET /api/v1/repos"), repos_list, middleware=auth)
    app.add(Route("POST /api/v1/repos"), repos_create, middleware=auth)
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
        slot = await yard.update_slot(name, edit)
        responses.json(w, cast(JsonValue, {"values": slot_values(slot), "warnings": edit.warn}))

    async def slots_delete(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        await yard.delete_slot(name)
        responses.json(w, {"name": name})

    async def logs(c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        tail_text = c.req.query.get("tail") or "200"
        if not tail_text.isdigit() or int(tail_text) < 1:
            raise YardError(400, "bad_tail", "tail must be a positive number")
        tail = int(tail_text)
        cursor = c.req.query.get("cursor") or ""
        container = c.req.query.get("container") or ""
        if c.req.query.get("follow") == "1":
            await _follow(yard, w, name, tail, cursor, container)
            return
        responses.json(w, await yard.log_page(name, tail, cursor, container))

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
            raise YardError(400, "bad_id", "deploy id must be a number")
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
    app.add(Route("GET /api/v1/slots/{name}/logs"), logs, middleware=auth)
    app.add(Route("GET /api/v1/slots/{name}/deploys"), deploys, middleware=auth)
    app.add(Route("GET /api/v1/slots/{name}/deploys/{id}"), deploy, middleware=auth)
    app.add(Route("GET /api/v1/containers/{name}"), inspect, middleware=auth)

    async def settings_get(_c: Context, w: Writer) -> None:
        responses.json(w, asdict(yard.store.settings()))

    async def settings_put(c: Context, w: Writer) -> None:
        body = await _object(c)
        responses.json(
            w,
            await yard.save_settings(
                _optional(body, "domain"),
                _optional(body, "email"),
                _optional(body, "self_repo"),
                _optional(body, "self_ref"),
                _optional(body, "self_image"),
            ),
        )

    app.add(Route("GET /api/v1/settings"), settings_get, middleware=auth)
    app.add(Route("PUT /api/v1/settings"), settings_put, middleware=auth)

    async def tokens_list(_c: Context, w: Writer) -> None:
        responses.json(w, [asdict(token) for token in yard.store.list_tokens()])

    async def tokens_create(c: Context, w: Writer) -> None:
        body = await _object(c)
        name = body.get("name", "")
        if not isinstance(name, str):
            raise YardError(400, "bad_json", "bad json")
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
        repo, old, new, ref = body.get("repo", ""), body.get("old", ""), body.get("new", ""), body.get("ref", "")
        if not all(isinstance(item, str) for item in (repo, old, new, ref)):
            raise YardError(400, "bad_json", "bad json")
        status, text = await yard.hook_push(repo, old, new, ref)
        responses.text(w, text, status)

    async def yard_post(c: Context, w: Writer) -> None:
        action = (await _object(c)).get("action", "")
        if not isinstance(action, str):
            raise YardError(400, "bad_json", "bad json")
        if action == "rollback":
            await yard.rollback()
            responses.json(w, {"image": "localhost/yard:previous", "container": "yard-swap"})
            return
        if action not in {"", "update"}:
            raise YardError(400, "bad_action", "action is update or rollback")
        responses.json(w, await yard.update_yard())

    async def yard_get(_c: Context, w: Writer) -> None:
        responses.json(w, yard.swap_status())

    app.add(Route("POST /api/v1/hooks/push"), hook, middleware=auth)
    app.add(Route("POST /api/v1/yard"), yard_post, middleware=auth)
    app.add(Route("GET /api/v1/yard"), yard_get, middleware=auth)


def _bearer(yard: Yard) -> Middleware:
    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            header = c.req.headers.get("authorization") or ""
            raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
            try:
                token = yard.store.token_ok(raw)
            except YardError as exc:
                responses.json(w, {"error": {"code": exc.code, "message": exc.message}}, exc.status)
                return
            if token is None:
                responses.json(w, {"error": {"code": "unauthorized", "message": "unauthorized"}}, 401)
                return
            c.state["actor"] = token.name
            try:
                await handler(c, w)
            except YardError as exc:
                # The writer already sent bytes. A second body would corrupt that response.
                if not w.started:
                    responses.json(w, {"error": {"code": exc.code, "message": exc.message}}, exc.status)
            except Exception as exc:
                if not w.started:
                    responses.json(w, {"error": {"code": "internal", "message": str(exc)}}, 500)
                else:
                    raise

        return wrapped

    return middleware


async def _follow(yard: Yard, w: Writer, name: str, tail: int, cursor: str, container: str) -> None:
    yard.store.slot(name)
    target = container or await yard.log_target(name)
    args = ["logs", "--follow", "--tail", str(tail)]
    if cursor:
        args.extend(("--since", cursor))
    args.append(target)
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
        raise YardError(400, "bad_json", "bad json") from exc
    if not isinstance(body, dict):
        raise YardError(400, "bad_json", "bad json")
    return body


def _string_map(body: dict) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in body.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise YardError(400, "bad_json", "bad json")
        found[key] = value
    return found


def _optional(body: dict, key: str) -> str | None:
    if key not in body:
        return None
    value = body[key]
    if not isinstance(value, str):
        raise YardError(400, "bad_json", "bad json")
    return value


def _one_exp(yard: Yard, raw: str) -> Experiment:
    if not raw.isdigit() or int(raw) <= 0:
        raise YardError(400, "bad_id", "experiment id must be a number")
    exp_id = int(raw)
    for exp in yard.store.list_experiments():
        if exp.id == exp_id:
            return exp
    raise YardError(404, "not_found", "experiment does not exist")


async def _exp_body(c: Context) -> tuple[str, str, list[Label]]:
    body = await _object(c)
    title = body.get("title", "")
    text = body.get("body", "")
    labels = body.get("labels", [])
    if not isinstance(title, str) or not isinstance(text, str) or not isinstance(labels, list):
        raise YardError(400, "bad_json", "bad json")
    if not title.strip():
        raise YardError(400, "bad_title", "a title is required")
    parsed: list[Label] = []
    for item in labels:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not isinstance(item.get("value"), str):
            raise YardError(400, "bad_json", "bad json")
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
