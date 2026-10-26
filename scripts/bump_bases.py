"""Refresh detect/bases.json with the current index digests. Run: uv run scripts/bump_bases.py"""

import asyncio
import json
from pathlib import Path

from app.server import sources
from app.server.detect.base import BASES

FILE = Path(__file__).resolve().parents[1] / "app" / "server" / "detect" / "bases.json"


async def main() -> None:
    found = {}
    for ref in sorted(BASES):
        found[ref] = await sources.digest(ref)
        print(f"{ref} -> {found[ref]}")
    FILE.write_text(json.dumps(found, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
