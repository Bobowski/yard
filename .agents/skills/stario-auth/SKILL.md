---
name: stario-auth
description: User identity in a Stario app - a signed session cookie, middleware that puts the user on c.state, a login screen, logout, and protected screens. Use when the user asks for login, sign in, accounts, sessions, "who is the user", or wants a screen only some users can open.
---

# Sessions and login

Read `AGENTS.md` first. This skill adds one shared module, one screen, and one rule: user identity lives in a signed cookie, never in a signal or a URL.

Stario ships `set_cookie` and `delete_cookie` in `stario.cookies` (`httponly=True` and `samesite="lax"` by default), `same_origin()` in `stario.http` (the scaffold already applies it), and a per-request `c.state` dict. It ships no session or user store. The code below is the whole default. Copy it; do not import a session library.

## `app/common/session.py`

One frozen dataclass. It signs, verifies, issues, and clears the cookie, and it provides two middlewares.

```python
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from stario import Context, Handler, Middleware, Writer
from stario.cookies import delete_cookie, set_cookie
from stario.datastar import SSE


@dataclass(frozen=True, slots=True)
class Session:
    secret: bytes
    cookie: str = "session"
    max_age: int = 60 * 60 * 24 * 14
    secure: bool = True

    def sign(self, body: str) -> str:
        digest = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode()

    def issue(self, w: Writer, user_id: str) -> None:
        body = f"{user_id}.{int(time.time()) + self.max_age}"
        set_cookie(
            w,
            self.cookie,
            f"{body}.{self.sign(body)}",
            max_age=self.max_age,
            httponly=True,
            secure=self.secure,
            samesite="lax",
        )

    def clear(self, w: Writer) -> None:
        delete_cookie(w, self.cookie, httponly=True, secure=self.secure, samesite="lax")

    def user_id(self, c: Context) -> str | None:
        body, _, sig = c.req.cookies.get(self.cookie, "").rpartition(".")
        user_id, _, exp = body.rpartition(".")
        if not user_id or not exp.isdigit() or int(exp) < time.time():
            return None
        return user_id if hmac.compare_digest(self.sign(body), sig) else None

    def attach(self) -> Middleware:
        """Every request gets c.state["user"]: the user id or None."""

        def middleware(inner: Handler) -> Handler:
            async def handler(c: Context, w: Writer) -> None:
                c.state["user"] = self.user_id(c)
                await inner(c, w)

            return handler

        return middleware

    def require(self, login_url: str) -> Middleware:
        """A screen for signed-in users. A page load redirects. A Datastar action navigates."""

        def middleware(inner: Handler) -> Handler:
            async def handler(c: Context, w: Writer) -> None:
                if c.state.get("user") is None:
                    if c.req.headers.get("datastar-request"):
                        SSE(w).navigate(login_url)
                    else:
                        w.redirect(login_url, status=303)
                    return
                await inner(c, w)

            return handler

        return middleware


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    salt_b64, _, _ = stored.partition("$")
    return hmac.compare_digest(hash_password(password, base64.b64decode(salt_b64)), stored)
```

`c.state["user"]` is the user id as a string. A handler that needs more reads the row from the users table with that id.

## `Config`

```python
@dataclass(frozen=True, slots=True)
class Config:
    db_path: str
    session_secret: bytes
    dev: bool
    admin_email: str
    admin_password: str

    @classmethod
    def from_env(cls) -> Config:
        secret = os.environ.get("SESSION_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("Set SESSION_SECRET to 32+ random characters.")
        return cls(
            db_path=os.environ.get("APP_DB_PATH", "app.sqlite3"),
            session_secret=secret.encode(),
            dev=os.environ.get("APP_DEV") == "1",
            admin_email=os.environ.get("ADMIN_EMAIL", ""),
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        )
```

Generate a secret: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. `APP_DEV=1` allows the cookie over plain HTTP on localhost. Do not set it in production.

## Wire it in `bootstrap`

Protected screens share one URL prefix, `/app`. Public screens (`/`, `/login`) stay outside it. One `app.use` protects the prefix.

```python
session = Session(secret=config.session_secret, secure=not config.dev)
app.use("/", session.attach())
app.use("/app", session.require(LOGIN.href()))
register_login(app, conn, session)
register_board(app, conn, relay)  # BOARD = Route("GET /app/board"): protected screens live under /app
```

The scaffold's `app.use("/", same_origin(), security_headers())` must stay: `same_origin()` answers 403 to a `POST`, `PUT`, `PATCH`, or `DELETE` that another site sent. With the `samesite="lax"` cookie, a command cannot be forged from another page. An app without it adds `same_origin()` before the session middleware. `app.use("/", ...)` runs before every route, so `c.state["user"]` is always set. `app.use("/app", ...)` runs before every route under `/app`, including the 404/405 answers. `app.use` and `app.add` work in any order; the router composes middleware when the app compiles.

Seed the first user in `bootstrap`, after the schema and before `yield`:

```python
if conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0 and config.admin_email:
    login_data.add_user(
        conn, str(uuid.uuid7()), config.admin_email, hash_password(config.admin_password)
    )
```

A single protected screen outside the prefix passes `middleware=[session.require(LOGIN.href())]` to `app.add`.

### Accounts in an app that already has screens

- Keep the URLs. Links and bookmarks point at them. Protect a screen where it is: `app.use("/todos", session.require(LOGIN.href()))` covers `/todos` and every route below it. Use the `/app` prefix only for new apps.
- Ask the developer what happens to rows that exist before accounts: give them to the seed user, or delete them. Do not leave them with an empty owner that no user can see. If you cannot ask, keep the rows. Do not delete them. Write the open choice in `## This app`.
- Data that belongs to a user gets a `user_id` column. Every query, stream, and command filters by `c.state["user"]`. A command checks that the row belongs to the user before it changes it (404 when it does not).

## The login screen: `app/features/login/`

Login is a static screen. Its submit is a private request: the error belongs to this tab alone, so a wrong password patches the re-rendered page into that tab, and a success navigates.

`urls.py`:

```python
from stario import Route

LOGIN = Route("GET /login")
LOGIN_POST = Route("POST /login")
LOGOUT = Route("POST /logout")
```

`data.py` owns the users table:

```python
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL
);
"""


def get_user_by_email(conn: sqlite3.Connection, email: str) -> tuple[str, str] | None:
    return conn.execute("SELECT id, password FROM users WHERE email = ?", (email,)).fetchone()


def add_user(conn: sqlite3.Connection, user_id: str, email: str, password_hash: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO users (id, email, password) VALUES (?, ?, ?)",
            (user_id, email, password_hash),
        )
```

With litewriter (skill `stario-litewriter`), keep the same `SCHEMA` and write the two functions like this:

```python
from litewriter import LiteWriter, Tx


def get_user_by_email(db: LiteWriter, email: str) -> tuple[str, str] | None:
    return db.one("SELECT id, password FROM users WHERE email = :email", email=email)


def add_user(tx: Tx, user_id: str, email: str, password_hash: str) -> None:
    tx.execute(
        "INSERT INTO users (id, email, password) VALUES (:id, :email, :password)",
        id=user_id,
        email=email,
        password=password_hash,
    )
```

Then a handler writes with `await db.write(data.add_user, ...)`. Hash the password before that call, never inside the write function: the hash would hold the writer's whole batch. The seed check is `db.value("SELECT count(*) FROM users") == 0`, and the seed itself is `await db.write(...)` in `bootstrap` (`db.submit(...).result()` raises there, because `bootstrap` runs on the event loop). A sign-up with a taken email raises `apsw.ConstraintError` from `db.write`; catch it and render "That email is already registered."

`views.py`:

```python
from stario import h
from stario.datastar import at, data
from stario.markup import HtmlElement

from app.common.shell import page
from app.features.login.urls import LOGIN_POST


def render(*, error: str = "") -> HtmlElement:
    return page(
        "Sign in",
        h.Main(
            {"id": "login"},
            data.signals({"email": "", "password": ""}, if_missing=True),
            h.Form(
                data.on("submit", at.post(LOGIN_POST.href()), prevent=True),
                h.Label(
                    "Email",
                    h.Input({"type": "email", "autocomplete": "username"}, data.bind("email")),
                ),
                h.Label(
                    "Password",
                    h.Input(
                        {"type": "password", "autocomplete": "current-password"},
                        data.bind("password"),
                    ),
                ),
                h.P({"id": "login-error", "role": "alert"}, error) if error else None,
                h.Button({"type": "submit"}, "Sign in"),
            ),
        ),
    )
```

`handlers.py`:

```python
import sqlite3

from stario import App, Context, Writer
from stario.datastar import SSE, read_signals

from app.common.session import Session, check_password
from app.features.home.urls import HOME
from app.features.login import data
from app.features.login.urls import LOGIN, LOGIN_POST, LOGOUT
from app.features.login.views import render


async def show_login(c: Context, w: Writer) -> None:
    if c.state.get("user"):
        w.redirect(HOME.href(), status=303)
        return
    w.html(render())


def submit_login(conn: sqlite3.Connection, session: Session):
    async def handler(c: Context, w: Writer) -> None:
        signals = await read_signals(c.req)
        email = str(signals.get("email", "")).strip().lower()
        password = str(signals.get("password", ""))
        row = data.get_user_by_email(conn, email)
        sse = SSE(w)
        if row is None or not check_password(password, row[1]):
            sse.patch_elements(render(error="Wrong email or password."))
            sse.patch_signals({"password": ""})
            return
        session.issue(w, row[0])
        sse.navigate(HOME.href())

    return handler


def logout(session: Session):
    async def handler(c: Context, w: Writer) -> None:
        session.clear(w)
        SSE(w).navigate(LOGIN.href())

    return handler


def register_login(app: App, conn: sqlite3.Connection, session: Session) -> None:
    app.add(LOGIN, show_login)
    app.add(LOGIN_POST, submit_login(conn, session))
    app.add(LOGOUT, logout(session))
```

`Set-Cookie` headers go out with the first write, so `issue` and `clear` are called before `navigate`.

A sign-up screen is the same shape with `data.add_user(conn, str(uuid.uuid7()), email, hash_password(password))`.

## A logout button on any screen

```python
h.Button({"type": "button"}, data.on("click", at.post(LOGOUT.href())), "Sign out")
```

## Tests

Prove login with `TestClient`. Do not open a browser for a sign-in or a redirect.

`tests/conftest.py` sets the secret and the seed user. `bootstrap` creates that user on the empty test database.

```python
import pytest
from stario.testing import TestClient

from app.features.login.urls import LOGIN_POST
from app.main import bootstrap


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-test-secret-test-secret-test")
    monkeypatch.setenv("APP_DEV", "1")
    monkeypatch.setenv("ADMIN_EMAIL", "ada@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    async with TestClient(bootstrap) as c:
        yield c


async def login(client, email="ada@example.com", password="pw") -> None:
    r = await client.post(LOGIN_POST.href(), json={"email": email, "password": password})
    assert "window.location" in r.text
```

`tests/test_login.py`:

```python
from app.features.board.urls import BOARD
from app.features.login.urls import LOGIN, LOGIN_POST, LOGOUT
from tests.conftest import login


async def test_protected_screen_redirects_to_login(client):
    r = await client.get(BOARD.href(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == LOGIN.href()


async def test_login_then_board_loads(client):
    await login(client)
    r = await client.get(BOARD.href())
    assert r.status_code == 200


async def test_wrong_password_rerenders_with_error(client):
    r = await client.post(LOGIN_POST.href(), json={"email": "ada@example.com", "password": "no"})
    assert "Wrong email or password" in r.text
    assert "window.location" not in r.text


async def test_logout_clears_the_session(client):
    await login(client)
    await client.post(LOGOUT.href())
    r = await client.get(BOARD.href(), follow_redirects=False)
    assert r.status_code == 303
```

`TestClient` keeps a cookie jar across requests, so `login` is enough to make the next `GET` signed in. A stream under `/app` that is opened signed out replies `navigate`, not `303`: send `headers={"datastar-request": "true"}` and assert `"window.location" in r.text`.

## Rules

- The cookie holds the user id and an expiry, signed. It holds no password, no role, no row.
- `httponly=True` always. `secure=True` outside local development. `samesite="lax"`.
- Passwords are hashed with `scrypt` through `hash_password`. Never stored, logged, or put in a span attribute.
- A protected screen uses `require`. A handler never checks the cookie itself.
- A command that acts for a user reads `c.state["user"]`, not a signal or a path parameter.
- Logout is a `POST`. A `GET` that changes state is a bug.
