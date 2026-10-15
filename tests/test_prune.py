import asyncio
import json
import time
from pathlib import Path

from app import prune
from app.config import Config
from app.errors import YardError
from app.podman import Podman
from app.store import Store
from app.yard import Yard

NOW = time.time()

IMAGES = json.dumps(
    [
        {"Names": ["localhost/app:ne11"], "Created": int(NOW - 30 * 86400), "Size": 100},
        {"Names": ["localhost/app:0ld"], "Created": int(NOW - 30 * 86400), "Size": 110},
        {"Names": ["localhost/app:st4le"], "Created": int(NOW - 30 * 86400), "Size": 400},
        {"Names": ["localhost/app:fr3sh"], "Created": int(NOW - 2 * 86400), "Size": 500},
        {"Names": ["localhost/busy:t4g"], "Created": int(NOW - 30 * 86400), "Size": 600},
        {"Names": ["docker.io/library/caddy:2"], "Created": int(NOW - 30 * 86400), "Size": 700},
        {"Names": ["localhost/yard-apply"], "Created": int(NOW - 30 * 86400), "Size": 800},
    ]
)


def _yard(tmp_path: Path) -> Yard:
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=True,
        boot=False,
        remote=False,
    )
    yard = Yard(config, Store.open(tmp_path))
    yard.store.create_repo("app")
    yard.store.create_slot("heldso", "app", "main")
    return yard


class _Fake(Podman):
    def __init__(self) -> None:
        super().__init__("podman", False)
        self.removed: list[str] = []

    async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
        if args[:1] == ("ps",):
            return (
                '[{"Names":["heldso-app-1"],"State":"running","Image":"localhost/app:ne11",'
                '"Labels":{"com.docker.compose.project":"heldso","com.docker.compose.service":"app"}}]'
            )
        if args[:1] == ("images",):
            return IMAGES
        if args[:1] == ("rmi",):
            self.removed.append(args[1])
            if args[1] == "localhost/busy:t4g":
                raise YardError(
                    502,
                    "podman",
                    "podman rmi localhost/busy:t4g: image is in use by container heldso-app-1",
                )
            return ""
        return "[]"


def test_prune_removes_what_no_slot_needs(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    try:
        found = asyncio.run(prune.run(yard, 7, False))
        images = {row["tag"]: row["kept"] for row in found["images"]}
        assert images == {
            "localhost/app:0ld": "",
            "localhost/app:st4le": "",
            "localhost/busy:t4g": "image is in use by container heldso-app-1",
        }
        assert fake.removed == ["localhost/app:0ld", "localhost/app:st4le", "localhost/busy:t4g"]
        assert found["freed"] == 110 + 400
        assert "checkouts" not in found
        assert found["days"] == 7
        assert not found["dry"]
    finally:
        yard.store.close()


def test_prune_dry_run_removes_nothing(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    try:
        found = asyncio.run(prune.run(yard, 7, True))
        assert fake.removed == []
        assert found["dry"]
    finally:
        yard.store.close()


def test_prune_one_day_removes_the_recent_ones(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    try:
        found = asyncio.run(prune.run(yard, 1, False))
        assert "localhost/app:fr3sh" in fake.removed
        tags = {row["tag"] for row in found["images"]}
        assert tags == {"localhost/app:0ld", "localhost/app:st4le", "localhost/app:fr3sh", "localhost/busy:t4g"}
    finally:
        yard.store.close()
