import asyncio
import json
import os
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


def _checkout(root: Path, name: str, size: int, age_days: float) -> Path:
    path = root / "checkouts" / "app" / name
    path.mkdir(parents=True)
    (path / "blob").write_bytes(b"x" * size)
    stamp = NOW - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _yard(tmp_path: Path) -> Yard:
    config = Config(
        root=tmp_path,
        caddy_sock=None,
        podman_sock="",
        podman_bin="podman",
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=True,
        boot=False,
        remote=False,
    )
    yard = Yard(config, Store.open(tmp_path / "db"))
    yard.store.create_repo("app")
    yard.store.create_slot("heldso", "app", "main", "", "", "")
    yard.store.set_image("heldso", "localhost/app:ne11")
    old = yard.store.begin_deploy("heldso", "0ld", "localhost/app:0ld")
    yard.store.finish_deploy(old, "ok", "")
    new = yard.store.begin_deploy("heldso", "ne11", "localhost/app:ne11")
    yard.store.finish_deploy(new, "ok", "")
    # live, old, recent, and a crashed extract
    _checkout(tmp_path, "ne11", 10, 30)
    _checkout(tmp_path, "0ld", 20, 30)
    _checkout(tmp_path, "fr3sh", 30, 2)
    _checkout(tmp_path, "aa.tmp", 40, 1)
    return yard


class _Fake(Podman):
    def __init__(self) -> None:
        super().__init__("podman", False)
        self.removed: list[str] = []

    async def run(self, *args: str, timeout=None, env=None) -> str:
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


def _names(found: dict) -> set:
    return {Path(row["path"]).name for row in found["checkouts"]}


def test_prune_removes_what_no_slot_needs(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    try:
        found = asyncio.run(prune.run(yard, 7, False))
        # The live and recent checkouts stay; the old one and the .tmp go.
        assert _names(found) == {"0ld", "aa.tmp"}
        assert not (tmp_path / "checkouts" / "app" / "0ld").exists()
        assert (tmp_path / "checkouts" / "app" / "ne11").is_dir()
        assert (tmp_path / "checkouts" / "app" / "fr3sh").is_dir()

        images = {row["tag"]: row["kept"] for row in found["images"]}
        assert images == {
            "localhost/app:st4le": "",
            "localhost/busy:t4g": "image is in use by container heldso-app-1",
        }
        assert fake.removed == ["localhost/app:st4le", "localhost/busy:t4g"]
        assert found["freed"] == 20 + 40 + 400
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
        assert _names(found) == {"0ld", "aa.tmp"}
        assert (tmp_path / "checkouts" / "app" / "0ld").is_dir()
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
        assert _names(found) == {"0ld", "aa.tmp", "fr3sh"}
        assert "localhost/app:fr3sh" in fake.removed
        tags = {row["tag"] for row in found["images"]}
        assert tags == {"localhost/app:st4le", "localhost/app:fr3sh", "localhost/busy:t4g"}
    finally:
        yard.store.close()
