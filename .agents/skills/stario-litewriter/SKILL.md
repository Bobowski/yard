---
name: stario-litewriter
description: SQLite in a Stario app through litewriter (one writer thread, group commit, watch). The recommended store when a Stario app needs persistent data. Use when the user asks for a database, SQLite, persistence, data that survives a restart, or a live screen that repaints when rows change.
---

# SQLite with litewriter

`litewriter` is the recommended SQLite store for a Stario app. Use it when the developer wants data on disk; an app with no persistent data needs no database. One SQLite file. One writer thread. Reads from any thread on their own connection. `watch` wakes a stream when a commit changes the rows it reads. It needs CPython 3.14.

```bash
uv add "litewriter>=0.3,<0.4"
```

Source and full reference: https://github.com/Bobowski/litewriter

## Open the file in bootstrap

```python
from litewriter import LiteWriter
from stario import App, Span

from app.assets import ASSETS
from app.config import Config
from app.features.board import data as board_data
from app.features.board.handlers import register_board


async def bootstrap(app: App, span: Span):
    config = Config.from_env()
    async with LiteWriter(config.db_path, schema=board_data.SCHEMA, claim=30) as db:
        span.attrs(await ASSETS.attach(app))
        register_board(app, db)
        yield
```

`async with` claims the file, runs `schema=` once in one transaction, starts the writer, serves, then drains and closes after `yield`. `claim=30` makes a second process wait 30 seconds, then raise `ClaimBusy`, so two servers never write one file. Every `SCHEMA` statement is `CREATE TABLE IF NOT EXISTS`, so a restart is safe. Several features join their schemas: `schema=board_data.SCHEMA + room_data.SCHEMA`.

Pass `db` into each feature's `register` function. No wrapper module is needed around it.

## Reads

Reads are plain calls on the loop thread. They do not queue behind the writer.

```python
rows = db.query("SELECT id, color FROM tiles ORDER BY id")
row = db.one("SELECT color FROM tiles WHERE id = :id", id=3)
count = db.value("SELECT count(*) FROM tiles")
```

`:name` in the SQL takes the keyword argument `name`. `one` returns `None` when there is no row; `value` raises. A second positional argument builds each row:

```python
@dataclass(frozen=True, slots=True)
class Tile:
    id: int
    color: str


tiles = db.query("SELECT id, color FROM tiles ORDER BY id", Tile)
```

A query is SQL text or a statement. Text is fine. Use a `Select` when a page composes the same query with different filters. Build statements once, at module level in `data.py`: a statement compiles its SQL on first use and keeps it.

```python
from litewriter import Select, col, param

TILES = Select("id", "color", from_="tiles", order_by=col("id"))
RED = TILES.where(col("color") == param("color"))

rows = db.query(RED, Tile, color="#ff0000")
```

`.where()` returns a new statement. The original stays as it was. Values from outside go in as `:name` parameters (`param("color")`), never pasted into SQL text. SQL functions are `fn("lower", col("name"))`.

## Writes

A write is a plain `def` whose first argument is `tx`. The writer runs it on its own thread inside one transaction and commits in a batch. You do not commit.

```python
from litewriter import Insert, Tx, Update, col, param

ADD_TILE = Insert("tiles", columns=["color"], returning="id")
SET_COLOR = Update("tiles", set=["color"], where=col("id") == param("id"))


def add_tile(tx: Tx, color: str) -> int:
    return tx.value(ADD_TILE, color=color)


def set_color(tx: Tx, tile_id: int, color: str) -> None:
    tx.execute(SET_COLOR, color=color, id=tile_id)
```

`columns=["color"]` renders `VALUES (:color)`, and `set=["color"]` renders `SET color = :color`. Plain SQL text works the same way: `tx.execute("UPDATE tiles SET color = :color WHERE id = :id", color=color, id=tile_id)`.

In a handler:

```python
tile_id = await db.write(add_tile, "#ff0000")
await db.write(set_color, 3, "#00ff00")
```

`await db.write(fn, ...)` returns after the commit, with the function's return value or its exception. `db.push(fn, ...)` does not wait; errors go to `on_error`. Use `write` in a command, so a failure becomes a response. On a plain thread (a synchronous test, a worker started with `asyncio.to_thread`) `db.submit(fn, ...).result()` is the same call without a loop; `.result()` on a running loop or on the writer thread raises `StateError`.

Put the write functions in the feature's `data.py` next to the queries. `tx.value`, `tx.one`, `tx.query`, `tx.execute`, and `tx.executemany` mirror the read calls and see the uncommitted batch.

- A write function holds the whole batch. No network, no sleep, no password hashing inside it: do that before `db.write`.
- An `async def` write function raises `QueryError`.
- Each write runs in its own savepoint. When it raises, only that write is undone and only that caller gets the exception.
- Writes awaited one after another each wait for a commit (up to 1/60 s each). Put related changes in one write function.
- SQLite errors pass through from `apsw`. A taken unique value raises `apsw.ConstraintError`; catch it in the command and render the conflict.

## The stream repaints from the database

`watch` replaces `Relay` when the state lives in the file. The first read is the rows now. Each later read comes after a commit that changed them. Equal rows do not wake, and a slow stream gets the newest rows, not a backlog.

```python
def board_live(db: LiteWriter):
    async def handler(c: Context, w: Writer) -> None:
        sse = SSE(w)
        async with db.watch(TILES, Tile) as rows:
            async for tiles in c.alive(rows):
                sse.patch_elements(render(tiles))

    return handler
```

A command then has no publish step. It validates, replies `204`, and calls `db.write`. The commit wakes every open stream that reads those rows. Only commits through this `LiteWriter` wake a watch.

Keep `Relay` for events that are not rows: presence, typing, a toast. Mix both in one `live` handler only when the screen needs both. Then subscribe to `Relay` and read the rows again inside the loop.

## Changing the schema

`schema=` also takes a function of `tx`, for a migration that must look before it changes something:

```python
def migrate(tx: Tx) -> None:
    tx.execute(SCHEMA)
    columns = {row[0] for row in tx.query("SELECT name FROM pragma_table_info('tiles')")}
    if "label" not in columns:
        tx.execute("ALTER TABLE tiles ADD COLUMN label TEXT NOT NULL DEFAULT ''")
```

Pass it as `LiteWriter(config.db_path, schema=board_data.migrate, claim=30)`. It runs once at start, before the first write.

## Tests

The scaffold's `tests/conftest.py` already points `APP_DB_PATH` at `tmp_path`, and `Config.from_env()` runs inside `bootstrap`, so each test gets its own file. The writer creates the file on start.

## Rules

- One `LiteWriter` per file per process. Open it in `bootstrap`. Pass it down. The claim is an `flock`: it releases when the holder dies, so a `ClaimBusy` means the holder is still alive.
- On dev reload the old process must die before the new one claims. Run the watcher so the app is its direct child: `uv run watchfiles 'python -m app.main' app/ static/`. A nested `uv run` inside the watched command is killed instead of the app, and the orphaned app keeps the claim. A `ClaimBusy` in dev means an orphan: find it with `lsof <db>.sqlite3-claim` and kill it.
- Reads on the loop thread, short and indexed. A rare large read (an export, a report over every row) awaits `db.aquery(REPORT)` (or `aone`, `avalue`), which runs on a worker thread. Each thread reads on its own connection.
- Writes through `db.write`. On a plain thread, `db.submit(fn).result()`; never `.result()` on the loop. A synchronous helper called from a handler goes through `await asyncio.to_thread(helper, ...)`. Inside a write function read with `tx`, never `db`: `db.query` on the writer thread raises `StateError`. Never keep `tx` after the function returns.
- `:name` parameters for every outside value. No f-strings into SQL.
- `IF NOT EXISTS` in every `SCHEMA`.
- `watch` for rows. `Relay` for events that are not rows.
