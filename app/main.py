"""
Yard.

```bash
STARIO_LOOP=uvloop python -m app.main
uv run yard login --url http://127.0.0.1:8000 --token <token>
```
"""

import os

from stario import App, Span, serve
from stario.http.config import server_config_from_env
from stario.http.server import resolve_loop_runner

from app.api import register_api
from app.config import Config
from app.store import Store, check_image, ensure_admin
from app.yard import Yard


async def bootstrap(app: App, span: Span):
    config = Config.from_env()
    span.attrs({"yard.root": str(config.root), "yard.boot": config.boot})
    store = Store.open(config.data_dir)
    if config.domain:
        store.set_meta("domain", config.domain)
    self_image = os.environ.get("YARD_SELF_IMAGE", "").strip()
    if self_image and not store.meta("self_image"):
        check_image(self_image)
        store.set_meta("self_image", self_image)
    token = ensure_admin(store, config.data_dir)
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
    config = server_config_from_env()
    resolve_loop_runner(config.event_loop)(serve(bootstrap, config=config))


if __name__ == "__main__":
    main()
