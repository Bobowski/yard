"""Refresh detect/bases.json and downloads.json with current digests. Run: uv run scripts/bump_bases.py"""

import asyncio
import json
import time
import urllib.request
from pathlib import Path

from app.server import sources
from app.server.detect.base import BASES, DOWNLOADS

HERE = Path(__file__).resolve().parents[1] / "app" / "server" / "detect"

MIN_AGE = 7 * 86400


def _github_json(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


async def _bases() -> None:
    found = {}
    for ref in sorted(BASES):
        found[ref] = await sources.digest(ref)
        print(f"{ref} -> {found[ref]}")
    (HERE / "bases.json").write_text(json.dumps(found, indent=2) + "\n")


async def _downloads() -> None:
    found = {}
    for name, download in DOWNLOADS.items():
        releases = _github_json(f"https://api.github.com/repos/{download['repo']}/releases")
        now = time.time()
        release = next(
            (
                r
                for r in releases
                if not r.get("prerelease") and r.get("published_at") and now - _epoch(r["published_at"]) >= MIN_AGE
            ),
            None,
        )
        if release is None:
            raise SystemExit(f"{name}: no release older than 7 days")
        sums = {}
        for asset in release["assets"]:
            for arch in ("amd64", "arm64"):
                if f"linux-{arch}" in asset["name"]:
                    algo, _, digest = asset["digest"].partition(":")
                    assert algo == "sha256", asset["digest"]
                    sums[arch] = digest
        assert set(sums) == {"amd64", "arm64"}, (name, sums)
        found[name] = {
            "repo": download["repo"],
            "version": release["tag_name"].lstrip("v"),
            "sha256": sums,
        }
        print(f"{name} -> {found[name]['version']}")
    (HERE / "downloads.json").write_text(json.dumps(found, indent=2) + "\n")


def _epoch(published: str) -> float:
    from datetime import datetime

    return datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").timestamp()


async def main() -> None:
    await _bases()
    await _downloads()


if __name__ == "__main__":
    asyncio.run(main())
