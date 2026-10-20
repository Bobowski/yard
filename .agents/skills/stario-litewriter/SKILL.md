---
name: stario-litewriter
description: SQLite in a Stario app through litewriter (one writer thread, group commit, watch). The recommended store when a Stario app needs persistent data. Use when the user asks for a database, SQLite, persistence, data that survives a restart, or a live screen that repaints when rows change.
---

# SQLite with litewriter

`litewriter` is the recommended SQLite store for a Stario app. Use it when the developer wants data on disk; an app with no persistent data needs no database. One SQLite file. One writer thread. Reads from any thread on their own connection. `watch` wakes a stream when a commit changes the rows it reads. It needs CPython 3.14.

```bash
uv add litewriter
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
    async with LiteWriter(config.db_path, claim=30) as db:
        db.execute_script(board_data.SCHEMA)
        span.attrs(await ASSETS.attach(app))
        register_board(app, db)
        yield
```

`async with` claims the file, starts the writer, serves, then drains and closes after `yield`. `claim=30` makes a second process wait 30 seconds, then fail, so two servers never write one file. `execute_script` runs DDL. Every `SCHEMA` statement is `CREATE TABLE IF NOT EXISTS`, so a restart is safe.

Pass `db` into each feature's `register` function. No wrapper module is needed around it.

## Reads

Reads are plain calls on the loop thread. They do not queue behind the writer.

```python
rows = db.query("SELECT id, color FROM tiles ORDER BY id")
row = db.one("SELECT color FROM tiles WHERE id = :id", id=3)
count = db.value("SELECT count(*) FROM tiles")
```

A query is SQL text or a `Select`. Text is fine. Use a `Select` when a page composes the same query with different filters:

```python
from litewriter import Select, col, param

TILES = Select("id", "color", from_="tiles", order_by=col("id"))
RED = TILES.where(col("color") == param("color"))

rows = db.query(RED, color="#ff0000")
```

`.where()` returns a new statement. The original stays as it was. Values from outside always go in as `:name` parameters.

## Writes

A write is a function of `tx`. The writer runs it on its own thread inside one transaction and commits in a batch. You do not commit.

```python
from litewriter import Insert, Tx, Update


def add_tile(tx: Tx, color: str) -> int:
    return tx.value(Insert("tiles", values={"color": ":color"}, returning="id"), color=color)


def set_color(tx: Tx, tile_id: int, color: str) -> None:
    tx.execute("UPDATE tiles SET color = :color WHERE id = :id", {"color": color, "id": tile_id})
```

In a handler:

```python
tile_id = await db.call(add_tile, "#ff0000")
await db.call(set_color, 3, "#00ff00")
```

`await db.call(fn, ...)` waits for the commit. `db.push(fn, ...)` does not wait; errors go to `on_error`. Use `call` in a command, so a failure becomes a response.

Put the write functions in the feature's `data.py` next to the queries. `tx.value`, `tx.one`, `tx.query`, and `tx.execute` mirror the read calls.

## The stream repaints from the database

`watch` replaces `Relay` when the state lives in the file. The first read is the rows now. Each later read comes after a commit that changed them. Equal rows do not wake.

```python
def board_live(db: LiteWriter):
    async def handler(c: Context, w: Writer) -> None:
        sse = SSE(w)
        async with db.watch(TILES) as rows:
            async for tiles in c.alive(rows):
                sse.patch_elements(render([Tile(*row) for row in tiles]))

    return handler
```

A command then has no publish step. It validates, replies `204`, and calls `db.call`. The commit wakes every open stream that reads those rows.

Keep `Relay` for events that are not rows: presence, typing, a toast. Mix both in one `live` handler only when the screen needs both. Then subscribe to `Relay` and read the rows again inside the loop.

## Tests

The scaffold's `tests/conftest.py` already points `APP_DB_PATH` at `tmp_path`, and `Config.from_env()` runs inside `bootstrap`, so each test gets its own file. The writer creates the file on start.

## Rules

- One `LiteWriter` per file per process. Open it in `bootstrap`. Pass it down. The claim is an `flock` — it releases when the holder dies, so a second process that times out means the holder is still alive.
- On dev reload the old process must die before the new one claims. Run the watcher so the app is its direct child: `uv run watchfiles 'python -m app.main' app/ static/`. A nested `uv run` inside the watched command is killed instead of the app, and the orphaned app keeps the claim — every restart then waits out `claim=` and dies with `WriterBusy`. A `WriterBusy` in dev means an orphan: find it with `lsof <db>.sqlite3-claim` and kill it.
- Reads on the loop thread. Writes through `db.call`. Never hold `tx` after the function returns. A rare large read (an export, a report over every row) goes through `await db.offload(fn, ...)`: `fn(conn, ...)` gets its own read-only connection on a worker thread, so the read does not block the loop.
- `:name` parameters for every outside value. No f-strings into SQL.
- `IF NOT EXISTS` in every `SCHEMA`. A column added later gets its own guarded `ALTER TABLE` in `bootstrap`, after `execute_script(SCHEMA)`:

  ```python
  columns = {row[0] for row in db.query("SELECT name FROM pragma_table_info('tiles')")}
  if "label" not in columns:
      db.execute_script("ALTER TABLE tiles ADD COLUMN label TEXT NOT NULL DEFAULT ''")
  ```
- `watch` for rows. `Relay` for events that are not rows.
