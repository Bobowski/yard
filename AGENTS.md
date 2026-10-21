# Stario app guide

How to build a Stario app. This is the default; when the developer asks for something else, do that. App notes live in `## This app` at the end.

Stario is Python hypermedia. A handler writes HTML. Datastar morphs it into the page. One open SSE response keeps a page live. State lives on the server. Docs: https://stario.dev/docs. Datastar: https://data-star.dev.

Skills in `.agents/skills/`: `stario-feature` (add or remove a screen, complete example files), `stario-litewriter` (SQLite), `stario-auth` (login), `stario-tailwind-daisyui` (CSS).

Before you call work done, run `just check` (ruff, ruff format, pyright, pytest) and fix every failure.

## Layout

```text
app/
  main.py           bootstrap: config, shared objects, register every feature
  config.py         frozen Config dataclass, from_env(); every setting and secret
  assets.py         ASSETS, STYLE_CSS, DATASTAR_JS (files in static/)
  common/shell.py   page(title, body): the <html> document, @baked
  features/<screen>/
    urls.py         Route values; imports only stario
    views.py        render(state) -> the full document
    handlers.py     handlers + register_<screen>(app, deps)
    data.py         SCHEMA + queries, when the screen owns tables
  <name>/           code with no URL (an engine, a parser, an API client); never imports features/
tests/test_<screen>.py
```

- A screen is a page or a resource with its own URL. One screen, one folder, named after the URL noun: `/rooms/{room_id}` → `room`. Delete the folder with the screen.
- A feature may import another feature's `urls.py` (links) and `data.py` (reads and writes), never its views or handlers.
- `common/` holds what every screen shares (the shell, the session), not borrowed views or queries. Two screens with similar HTML keep their own copies.
- `app/db.py` exists only when several features share one connection object.
- The tree serves the problem: split a file when it is hard to read; keep imports one way.
- No `.js` files: a Datastar expression on the element covers small UI behaviour.

## Stario rules

- Not ASGI: no Uvicorn, FastAPI, or response objects. `python -m app.main` runs `asyncio.run(serve(bootstrap))`.
- `bootstrap(app: App, span: Span)` is an async generator with one `yield`: build shared objects, `span.attrs(await ASSETS.attach(app))`, register features, `yield`, clean up. The scaffold adds `app.use("/", same_origin(), security_headers())` (`stario.http`): cross-site commands get 403, and responses get baseline headers. Routes are fixed after `bootstrap`.
- A handler is `async def handler(c: Context, w: Writer) -> None`. It answers once: `w.html`, `w.text`, `w.empty`, `w.redirect`, or `SSE(w)`.
- `Route("GET /rooms/{room_id}")` is one method on one path. `app.add(ROUTE, handler)` registers it; `ROUTE.href(room_id)` builds the link. No `url_for`, no `app.get`.
- `c.match.params`, `c.req.query`, `c.req.cookies`; `c.state` is filled by middleware. Middleware is `(inner: Handler) -> Handler`, added with `app.use("/prefix", mw)`.
- A long loop iterates `c.alive(source)`. Background work is `c.app.create_task(coro)`, never bare `asyncio.create_task`.
- JSON goes through `stario.json` (`w.json`, `SSE`), not `json.dumps`.
- One process, one event loop. Shared state is an object built in `bootstrap` and passed to `register_<screen>`. No module-level state. No `os.environ` outside `config.py`.
- Stario does not read `.env`. Settings come from the environment; `set dotenv-load := true` in the `justfile` loads a local `.env`.

## Three kinds of action

Sort every user action first:

- **Go somewhere**: `<a href>`.
- **Ask for something only this tab needs** (search as you type, a filter, a preview): a `GET` that reads the signals and answers `SSE(w).patch_elements(part)`, where `part` is the element with that id. No state change, no stream, no `Relay`, no result kept on the server. `POST` only when the input is large.
- **Change shared state** (add, edit, delete, start a job): a command.

A screen whose state cannot change while it is open is static: `w.html(render(state))`. A screen that shows shared state that can change while it is open is live, with or without its own commands:

```text
GET  /board        the first document
GET  /board/live   an SSE stream that stays open and repaints the page
POST /board/...    a command: validate, reply 204, change state, notify
```

The stream is the only thing that paints shared state. A command does not return HTML; its answer would race the stream, and other tabs would not see it.

1. `GET /board` returns `render(state)`. The page opens the stream on load: `data.init(at.get(BOARD_LIVE.href(), retry="always"))`.
2. The stream subscribes, sends `render(state)`, then sends it again after each notification.
3. A command reads and validates the signals, replies `w.empty()` (204), changes state, and publishes.

```python
def board_live(board: Board, relay: Relay[str]):
    async def handler(c: Context, w: Writer) -> None:
        async with relay.subscribe(CHANGED, latest=True) as events:
            sse = SSE(w)
            sse.patch_elements(render(board.list_tiles()))
            async for _ in c.alive(events):
                sse.patch_elements(render(board.list_tiles()))

    return handler
```

Subscribe before the first render. `latest=True` keeps only the newest event per subject: a repaint needs no backlog. Subjects are dotted strings at the top of `handlers.py`; `"room.*"` matches every room. With litewriter, `db.watch(query)` replaces `Relay` for row changes.

A command is a Datastar action: `data.on("click", at.post(ROUTE.href()))`, or `data.on("submit", at.post(ROUTE.href()), prevent=True)` on a `<form>`. A page with inputs uses Datastar, not `<form method="post">`.

Bad input: empty or unchanged input replies 204 and does nothing. A domain conflict (name taken) is written to state and rendered. Malformed or forged input (bad format, unknown id, another user's row) gets `w.text("why", status=400)` (or 404, 403) and `return`.

Slow work (a model call, a download, a report): the command adds a job with status `queued`, replies 204, publishes, and runs `c.app.create_task(run(job))`. The task sets `running`, then `done` or `failed`, and publishes each change. Never `await` slow work in a command. The delay or the client is a `Config` field, so tests run fast.

A command that ends on another screen replies `SSE(w).navigate(OTHER.href())`. A 303 to a Datastar request does not change the URL.

## Render from state

- `render(state)` returns the whole document. The first `GET` and every stream wake call it. It does not fetch.
- The stream sends the whole document every time. Datastar morphs only the changes. Narrow a patch only after you measure a cost.
- A page that differs per viewer takes the viewer: `render(state, viewer)`.
- The element with `data.init(...)` and `data.signals(...)` keeps its tag and `id` in every render.
- Write the tree inline in `render`. A helper view exists for a block in a loop. `page()` is `@baked`: each parameter is a whole child or attribute; loops and conditions stay in the caller.

## Signals and UI state

Morphing HTML is the default: the server renders the state. Signals carry what the user types or picks.

- Use a signal for an input, and for client-only state the HTML cannot hold. Stario passes signal names through unchanged; a leading `_` (`_menu_open`) makes a signal local — Datastar does not send it, so it is not in `read_signals`.
- `data.bind("title")` ties an input to a signal. `await read_signals(c.req)` returns the sent signals. Validate each one.
- One `data.signals({...}, if_missing=True)` per page, on its top element (on a live page, the one that opens the stream), so a repaint keeps what the user typed.
- Keep the count small: one signal per field (`title`) and one for the row being edited (`editing_id`), not one per row or cell. The command validates `editing_id` like any input. Correctness comes first.
- A screen's resource id lives in its URL. The signed-in user lives in a signed cookie (skill `stario-auth`), never in a signal.
- How one tab views shared state (sort, filter, page) lives in the URL query. Links change it; the page and the stream both read `c.req.query`. A reload or a shared link keeps the view.
- A command may clear an input it consumed: `SSE(w).patch_signals({"title": ""})` instead of `w.empty()`.
- `data.indicator("_saving")` makes `$_saving` true while that request is in flight. Use it like any signal: `data.attr("disabled", "$_saving")`, `data.class_("loading", "$_saving")`.

UI state must survive a morph, because a repaint can arrive at any moment. A `<dialog>` or `<details>` holds its own open state; add `data.preserve_attr("open")`. The server never renders a user-opened dialog as open. A subtree the client owns gets `data.ignore_morph()`.

## Storage

Choose with the developer. Do not add a database the app does not need.

- No persistence: an object built in `bootstrap`.
- SQLite: litewriter is the recommended default (skill `stario-litewriter`). Plain `sqlite3` is fine for a small app.
- Every `SCHEMA` statement is `IF NOT EXISTS`. The database path is a `Config` field; tests point it at `tmp_path`.
- A host data directory (Yard sets `PROJECT_DATA_DIR=/data`) must hold the database, or a redeploy loses it. Reading it in `Config` is worth it for an app that deploys.

## Code shape

- A handler that needs nothing is a plain `async def health(c: Context, w: Writer)`, added as `app.add(HEALTH, health)`. A factory exists only to close over dependencies: `show_board(board)` returns the handler. One `register_<screen>(app, deps)`. No handler classes.
- In a handler: read input, validate (4xx and `return`), reply, change state, publish.
- Queries are named by verb: `list_*`, `get_*` (one or `None`), `add_*`, `set_*`, `remove_*`.
- Fewer, fuller functions. Inline a function with one call site. No helper per element, no compatibility aliases, no comments that repeat the code.

## Tests

Tests run the production `bootstrap` through `TestClient`. Each screen: the page loads; a command repaints the stream; malformed input returns 400. That stream test is the proof. Do not open a browser for a command.

```python
async def test_paint_repaints_the_stream(client):
    headers = {"accept-encoding": "identity"}
    async with client.stream("GET", BOARD_LIVE.href(), headers=headers) as live:
        events = live.iter_events()
        assert 'id="board"' in (await anext(events))["data"]
        r = await client.post(PAINT.href(1), json={"color": "#ff0000"})
        assert r.status_code == 204
        assert "#ff0000" in (await anext(events))["data"]
```

`client.post(url, json=...)` sends signals; a `GET` takes `params={"datastar": '{"q": "x"}'}`. A `navigate` reply is a 200 event stream with `window.location` in it. The client keeps cookies.

## Common mistakes

| Avoid | Do |
| --- | --- |
| Flat `app/routes.py`, `app/views/` | `app/features/<screen>/` |
| A command that returns HTML | 204, change state, publish |
| A stream or server state for a result one tab asked for | a `GET` that patches its part |
| `await` slow work in a command | a job and `c.app.create_task` |
| `w.redirect` after a Datastar action | `SSE(w).navigate(url)` |
| A first version with small targeted patches | `patch_elements(render(state))` |
| A signal per row or cell | one per field and an `editing_id` |
| UI state in signals when HTML can hold it | `<dialog>`, `<details>`, `preserve_attr` |
| A signal that holds a row or the user | the store, the cookie |
| Module-level lists or dicts as state | an object built in `bootstrap` |

## Cursor Cloud specific instructions

Install is `.cursor/cloud-install.sh`. Use computer use only when the stream test cannot prove the change. Do not open a pull request. Push `main` to deploy.

## This app

Yard is a Stario JSON API plus the `yard` command line (the PyPI name is `staryard`). It builds app Containerfiles, runs the containers through Podman, and writes the Caddy config. The Yard app name is `yard`.

There is no HTML. The screen, render, and Datastar parts of this guide do not apply. Handlers, `Route`, and `TestClient` do. There is no `app/features/`; three layers carry the code:

- `app/shared.py` — errors, values, time helpers, and `atomic_write`. Stdlib only.
- `app/cli/` — the `yard` command. `main.py` holds the argument tree, the HTTP client, the checkout deduction, and the commands; `render.py` holds the tables. Stdlib only; it talks to the API over HTTP.
- `app/server/` — the API. `api.py` owns HTTP parsing and JSON; api.py holds Route values and one handler factory per route, and `register(app, yard)` wires them; the services (`apps`, `binds`, `status`, `doctor`) return dataclasses; the adapters (`git`, `podman`, `caddy`, `compose`, `forges`, `journal`, `store`) wrap one tool each. `forges.py` is Origin and GitHub behind one `Forge` class. `yard.py` holds the dependencies and runtime state only. `main.py` is the composition root. Forge setup is not a service: it lives in the CLI (`yard forge setup`).

Imports go one way: api → services → yard/store/adapters → shared. Only `app.server.main` imports `app.server.api`. `app.cli` never imports `app.server`, `stario`, or `cryptography`. `tests/test_layers.py` enforces it. HTTP tests use `TestClient(bootstrap)` from `app.server.main`; service tests build a `Yard` with fake adapters.

Run the server with `python -m app.server.main`.

`Config.from_env` reads `YARD_ROOT` (default `yard-data`), `YARD_PODMAN_SOCK`, `YARD_PODMAN_BIN`, `YARD_SKIP_DEPLOY`, `YARD_BOOT`, `YARD_CHECK_SECONDS`, `YARD_JOURNAL_DIR`, `YARD_JOURNALCTL_BIN`, `YARD_JOURNAL_SOCKET`, and `CONTAINER_HOST`. App state lives under `YARD_ROOT/apps/<name>/`.

The `journal` adapter works both ways: `send` writes a Yard event to the journald native socket (stdout JSON when the socket is missing), and `read`/`follow` run journalctl. No event field may carry a secret — an env change logs key names only.

The package uses `uv_build` with `module-name = ["app"]`. `package = false` in `[tool.uv]` keeps the top-level `app` importable from the checkout.
