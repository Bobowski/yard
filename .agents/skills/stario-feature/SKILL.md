---
name: stario-feature
description: Add, change, or remove a screen (page or resource) in a Stario app. Use when the user asks for a new page or route, wants a screen to update live or in every tab, adds a button, form, command, edit dialog, search box, filter, or background job to a screen, fixes a page that does not refresh, or deletes a screen. Has complete example files for a static screen, a private read, a live screen, slow work, and an edit dialog.
---

# Add, change, or remove a screen

`AGENTS.md` holds the rules. This skill holds the steps and complete files. To change an existing screen, read its folder first, then use the example below that matches the change.

## Decide first

1. The URL. One screen, one folder `app/features/<screen>/`, named after the URL noun.
2. Sort the actions (`AGENTS.md`, Three kinds of action). Only commands make a screen live: page `GET`, `GET .../live`, one `POST` per command. A private read is one more `GET` that patches its part.
3. State. None, an object built in `bootstrap`, or tables in `data.py`. Ask the developer before you add a database (`AGENTS.md`, Storage).
4. Slow work. A command that waits on something slow becomes a job (last example below).

## Steps

1. `app/features/<screen>/__init__.py`, `urls.py`, `views.py`, `handlers.py`, and `data.py` only if the screen owns tables.
2. `app/main.py`: build the shared object once, then `register_<screen>(app, ...)` before `yield`.
3. A link from an existing screen: import its `urls.py` and use `ROUTE.href()` in `h.A({"href": ...})`.
4. `tests/test_<screen>.py`: the page loads; a command repaints the stream; malformed input returns 400. That stream test is the proof. Do not open a browser for a command.
5. `just check`. Fix every failure.

## A static screen

```python
# urls.py
from stario import Route

ABOUT = Route("GET /about")
```

```python
# views.py
from stario import h
from stario.markup import HtmlElement

from app.common.shell import page


def render(version: str) -> HtmlElement:
    return page("About", h.Main({"id": "about"}, h.H1("About"), h.P(f"Version {version}")))
```

```python
# handlers.py
from stario import App, Context, Writer

from app.features.about.urls import ABOUT
from app.features.about.views import render


def show_about(version: str):
    async def handler(c: Context, w: Writer) -> None:
        w.html(render(version))

    return handler


def register_about(app: App, version: str) -> None:
    app.add(ABOUT, show_about(version))
```

`version` is a `Config` field read from the environment. The feature never reads `os.environ`.

## A private read (search as you type)

```python
# urls.py
SEARCH = Route("GET /search")
SEARCH_RESULTS = Route("GET /search/results")
```

```python
# views.py: the page, and the part the read replaces
def results(matches: list[str], total: int) -> HtmlElement:
    return h.Div({"id": "results"}, h.P(f"{total} matches"), h.Ul(*[h.Li(m) for m in matches]))


def render(matches: list[str], total: int) -> HtmlElement:
    return page(
        "Search",
        h.Main(
            {"id": "search"},
            data.signals({"q": ""}, if_missing=True),
            h.Input(
                {"type": "search"},
                data.bind("q"),
                data.on("input", at.get(SEARCH_RESULTS.href()), debounce="200ms"),
            ),
            results(matches, total),
        ),
    )
```

```python
# handlers.py
def search_results(cities: list[str]):
    async def handler(c: Context, w: Writer) -> None:
        q = str((await read_signals(c.req)).get("q", "")).strip().lower()
        found = [city for city in cities if q in city.lower()]
        SSE(w).patch_elements(results(found[:20], len(found)))

    return handler
```

A `GET` carries the signals in the `datastar` query parameter; `read_signals` reads them. Test it with `client.get(SEARCH_RESULTS.href(), params={"datastar": '{"q": "par"}'})`.

A sort or a filter on a live screen is different: it is that tab's view of shared state, so it goes in the URL query (`TODOS.href(query={"sort": "title"})`), and the stream route takes the same query (`data.init(at.get(TODOS_LIVE.href(query={"sort": sort}), retry="always"))`).

## A live screen

An in-memory store. The same shape works with SQLite: the store methods become `data.py` functions (skill `stario-litewriter`).

```python
# urls.py
from stario import Route

BOARD = Route("GET /board")
BOARD_LIVE = Route("GET /board/live")
PAINT = Route("POST /board/{tile_id}/paint")
```

```python
# data.py — the state object, built once in bootstrap
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Tile:
    id: int
    color: str


@dataclass(slots=True)
class Board:
    colors: dict[int, str] = field(default_factory=lambda: dict.fromkeys(range(1, 10), "#ffffff"))

    def list_tiles(self) -> list[Tile]:
        return [Tile(i, c) for i, c in sorted(self.colors.items())]

    def set_color(self, tile_id: int, color: str) -> bool:
        if tile_id not in self.colors:
            return False
        self.colors[tile_id] = color
        return True
```

```python
# views.py
from stario import h
from stario.datastar import at, data
from stario.markup import HtmlElement, classes

from app.common.shell import page
from app.features.board.data import Tile
from app.features.board.urls import BOARD_LIVE, PAINT


def render(tiles: list[Tile]) -> HtmlElement:
    return page(
        "Board",
        h.Main(
            {"id": "board"},
            data.signals({"color": "#000000"}, if_missing=True),
            data.init(at.get(BOARD_LIVE.href(), retry="always")),
            h.Label("Color", h.Input({"type": "color"}, data.bind("color"))),
            h.Div(
                classes("grid"),
                *[
                    h.Button(
                        {"style": f"background:{t.color}", "aria-label": f"Tile {t.id}"},
                        data.on("click", at.post(PAINT.href(t.id))),
                    )
                    for t in tiles
                ],
            ),
        ),
    )
```

```python
# handlers.py
import re

from stario import App, Context, Relay, Writer
from stario.datastar import SSE, read_signals

from app.features.board.data import Board
from app.features.board.urls import BOARD, BOARD_LIVE, PAINT
from app.features.board.views import render

CHANGED = "board.changed"


def show_board(board: Board):
    async def handler(c: Context, w: Writer) -> None:
        w.html(render(board.list_tiles()))

    return handler


def board_live(board: Board, relay: Relay[str]):
    async def handler(c: Context, w: Writer) -> None:
        async with relay.subscribe(CHANGED, latest=True) as events:
            sse = SSE(w)
            sse.patch_elements(render(board.list_tiles()))
            async for _ in c.alive(events):
                sse.patch_elements(render(board.list_tiles()))

    return handler


def paint(board: Board, relay: Relay[str]):
    async def handler(c: Context, w: Writer) -> None:
        signals = await read_signals(c.req)
        color = str(signals.get("color", ""))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            w.text("color must be #rrggbb", status=400)
            return
        if not board.set_color(int(c.match.params["tile_id"]), color):
            w.text("no such tile", status=404)
            return
        w.empty()
        relay.publish(CHANGED, "paint")

    return handler


def register_board(app: App, board: Board, relay: Relay[str]) -> None:
    app.add(BOARD, show_board(board))
    app.add(BOARD_LIVE, board_live(board, relay))
    app.add(PAINT, paint(board, relay))
```

```python
# app/main.py, inside bootstrap before yield
relay: Relay[str] = Relay()
register_board(app, Board(), relay)
```

One `Relay` per app, built in `bootstrap`, shared by every feature.

```python
# tests/test_board.py
from app.features.board.urls import BOARD_LIVE, PAINT


async def test_paint_repaints_the_stream(client):
    headers = {"accept-encoding": "identity"}
    async with client.stream("GET", BOARD_LIVE.href(), headers=headers) as live:
        events = live.iter_events()
        assert 'id="board"' in (await anext(events))["data"]
        r = await client.post(PAINT.href(1), json={"color": "#ff0000"})
        assert r.status_code == 204
        assert "#ff0000" in (await anext(events))["data"]
```

Open the live stream. Send the command. Assert the next event. That is the proof.

## A screen with slow work

The command queues a job and returns at once. A task does the work and publishes each status change. The stream shows the jobs, so the `queued` row is the click feedback.

```python
# data.py
from dataclasses import dataclass, field
from itertools import count


@dataclass(slots=True)
class Job:
    id: int
    status: str = "queued"  # queued | running | done | failed
    result: str = ""


@dataclass(slots=True)
class Jobs:
    rows: dict[int, Job] = field(default_factory=dict)
    ids: count = field(default_factory=lambda: count(1))

    def add_job(self) -> Job:
        job = Job(next(self.ids))
        self.rows[job.id] = job
        return job

    def list_jobs(self) -> list[Job]:
        return sorted(self.rows.values(), key=lambda j: j.id, reverse=True)
```

```python
# handlers.py (the command and the task)
def start_report(jobs: Jobs, relay: Relay[str], delay: float):
    async def run(job: Job) -> None:
        job.status = "running"
        relay.publish(CHANGED, "running")
        try:
            await asyncio.sleep(delay)
            job.result, job.status = f"Report at {datetime.now(UTC):%H:%M:%S}", "done"
        except Exception as exc:
            job.result, job.status = str(exc), "failed"
        relay.publish(CHANGED, job.status)

    async def handler(c: Context, w: Writer) -> None:
        job = jobs.add_job()
        w.empty()
        relay.publish(CHANGED, "queued")
        c.app.create_task(run(job))

    return handler
```

`delay` is a `Config` field, so a test sets it to `0`. In the test, read stream events until one contains `done`.

In the view, the button gets `data.indicator("_starting")` and `data.attr("disabled", "$_starting")` for the moment the request is in flight; the new `queued` row takes over after that.

## An edit dialog that survives repaints

One dialog for the whole list. The Edit button fills the field signals and opens it. One route takes the edit; the row id travels as a signal and is validated like any input.

```python
# urls.py
RENAME = Route("POST /notes/rename")
```

```python
# views.py, inside render(notes)
h.Main(
    {"id": "notes"},
    data.signals({"editing_id": 0, "title": ""}, if_missing=True),
    data.init(at.get(NOTES_LIVE.href(), retry="always")),
    h.Ul(
        *[
            h.Li(
                n.title,
                h.Button(
                    {"type": "button"},
                    data.on(
                        "click",
                        f"$editing_id = {n.id}; $title = {string_literal(n.title)}; "
                        "document.getElementById('edit').showModal()",
                    ),
                    "Edit",
                ),
            )
            for n in notes
        ]
    ),
    h.Dialog(
        {"id": "edit"},
        data.preserve_attr("open"),
        h.Form(
            data.on("submit", at.post(RENAME.href()) + "; el.closest('dialog').close()", prevent=True),
            h.Input({"type": "text"}, data.bind("title")),
            h.Button({"type": "submit"}, "Save"),
        ),
    ),
)
```

```python
# handlers.py
def rename(notes: Notes, relay: Relay[str]):
    async def handler(c: Context, w: Writer) -> None:
        signals = await read_signals(c.req)
        note_id, title = signals.get("editing_id"), str(signals.get("title", "")).strip()
        if type(note_id) is not int or notes.get_note(note_id) is None:
            w.text("no such note", status=404)
            return
        w.empty()
        if title:
            notes.set_title(note_id, title)
            relay.publish(CHANGED, "rename")

    return handler
```

`string_literal` comes from `stario.datastar.format`. A repaint keeps the dialog open (`preserve_attr`) and keeps what the user typed (`if_missing=True`). `type(...) is not int` also rejects `true`, which `isinstance` accepts. An add form on the same page binds its own signal (`new_title`), not the dialog's `title`.

## Remove a screen

1. Delete `app/features/<screen>/` and `tests/test_<screen>.py`.
2. Remove the `register_<screen>` call from `app/main.py`.
3. Fix every import of the deleted `urls.py` or `data.py`; pyright finds them.
4. Tables the screen owned: ask the developer whether to drop them.
5. `just check`.

## When the folder grows

A `handlers.py` over about 300 lines or a `views.py` over about 400 lines usually hides a second screen. Split by URL noun, not by layer. Code with no URL moves to `app/<name>/`.
