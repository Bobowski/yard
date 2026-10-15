"""Yard. `python -m app.main` serves; `yard login --token <token>` talks to it."""

import uvloop
from stario import App, Span, serve

from app.api import register_api
from app.config import Config
from app.store import Store, ensure_admin
from app.yard import Yard


async def bootstrap(app: App, span: Span):
    config = Config.from_env()
    span.attrs({"yard.root": str(config.root), "yard.boot": config.boot})
    store = Store.open(config.root)
    token = ensure_admin(store)
    if token:
        span.event("yard.admin_token", {"path": str(config.token_file)})
    yard = Yard(config, store)
    if config.boot:
        await yard.boot(app)
    register_api(app, yard)

    try:
        yield
    finally:
        await yard.aclose()


def main() -> None:
    uvloop.run(serve(bootstrap))


if __name__ == "__main__":
    main()
