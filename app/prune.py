"""yard prune: remove the checkouts and images no slot needs. Deploy rows stay; they are the history."""

import shutil
import time
from typing import TYPE_CHECKING, Any

from app import podman, status
from app.values import image_sha

if TYPE_CHECKING:
    from app.yard import Yard


async def run(yard: Yard, days: int, dry: bool) -> dict[str, Any]:
    config, store = yard.config, yard.store
    slots = store.list_slots()
    # A slot keeps its live image, its live checkout, and the image its last
    # ok deploy would revert to. localhost/yard-apply is never a candidate.
    keep_checkouts = {
        config.checkout(slot.repo, slot.directory, image_sha(slot.live_image)) for slot in slots if slot.live_image
    }
    keep_images = {slot.live_image for slot in slots if slot.live_image}
    for slot in slots:
        for row in store.list_deploys(slot.name, 100):
            if row.status == "ok" and row.image and row.image != slot.live_image:
                keep_images.add(row.image)
                break

    cutoff = time.time() - days * 86400
    freed = 0
    checkouts: list[dict[str, Any]] = []
    base = config.root / "checkouts"
    for repo_dir in sorted(base.glob("*")):
        if not repo_dir.is_dir():
            continue
        for path in sorted(repo_dir.iterdir()):
            if not path.is_dir() or path in keep_checkouts:
                continue
            # A .tmp directory is a crashed extract; its age does not matter.
            if not path.name.endswith(".tmp") and path.stat().st_mtime >= cutoff:
                continue
            size = status.dir_size(path)
            checkouts.append({"path": str(path), "bytes": size})
            freed += size
            if not dry:
                shutil.rmtree(path, ignore_errors=True)

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
    return {"checkouts": checkouts, "images": images, "freed": freed, "days": days, "dry": dry}
