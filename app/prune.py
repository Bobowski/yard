"""yard prune: remove localhost images no container uses."""

import time
from typing import TYPE_CHECKING, Any

from app import podman

if TYPE_CHECKING:
    from app.yard import Yard


async def run(yard: Yard, days: int, dry: bool) -> dict[str, Any]:
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    keep_images = {item.image for item in listed.values() if item.image.startswith("localhost/")}

    cutoff = time.time() - days * 86400
    freed = 0
    images: list[dict[str, Any]] = []
    raw = await yard.podman.run("images", "--format", "json")
    for image in podman.parse_images(raw):
        if not image.name.startswith("localhost/") or image.name == "localhost/yard-apply":
            continue
        if image.name in keep_images or (image.created and image.created.timestamp() >= cutoff):
            continue
        kept = ""
        if not dry:
            try:
                await yard.podman.run("rmi", image.name)
            except Exception as exc:
                # Podman refuses an image a container uses. The refusal is a kept reason.
                kept = str(exc).removeprefix(f"podman rmi {image.name}: ").splitlines()[-1].strip()
        if not kept:
            freed += image.size
        images.append({"tag": image.name, "bytes": image.size, "kept": kept})
    # podman image prune does not run: the build cache is worth keeping.
    return {"images": images, "freed": freed, "days": days, "dry": dry}
