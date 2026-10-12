"""One pass before Yard and Caddy take their names. Compose keeps this process up afterwards."""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from app.values import format_env

NAMES = (
    "bobowski-site",
    "engratia",
    "honorata",
    "kran-fundament",
    "stario-site",
    "caddy",
    "yard",
)


def main() -> None:
    root = Path(os.environ["YARD_ROOT"])
    _env_files(root)
    _caddy_listen(root)
    for name in ("build", "src"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
            print(f"removed {path}", flush=True)
    for name in NAMES:
        code = subprocess.call(["podman", "rm", "-f", "-t", "3", name])
        print(f"remove {name} {code}", flush=True)
    Path("/tmp/ready").write_text("ok\n")
    print("ready", flush=True)
    while True:
        time.sleep(3600)


def _env_files(root: Path) -> None:
    env_dir = root / "env"
    env_dir.mkdir(mode=0o700, exist_ok=True)
    env_dir.chmod(0o700)
    db_path = root / "data" / "yard" / "yard.sqlite3"
    if not db_path.is_file():
        return
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(slots)")}
    if "env_json" not in columns:
        db.close()
        return
    for row in db.execute("SELECT name, env_json FROM slots"):
        name = str(row["name"])
        path = env_dir / f"{name}.env"
        if path.is_file() and path.stat().st_size:
            print(f"env {name} kept", flush=True)
            continue
        try:
            raw = json.loads(str(row["env_json"] or "{}"))
        except json.JSONDecodeError:
            raw = {}
        env: dict[str, str] = {}
        if isinstance(raw, dict):
            env = {
                str(key): str(value) for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)
            }
        path.write_text(format_env(env))
        path.chmod(0o600)
        print(f"env {name} {len(env)}", flush=True)
    db.close()


def _caddy_listen(root: Path) -> None:
    path = root / "data" / "caddy" / "autosave.json"
    if not path.is_file():
        return
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError:
        print("caddy autosave is not json", flush=True)
        return
    if not isinstance(doc, dict):
        return
    admin = doc.get("admin")
    if not isinstance(admin, dict):
        admin = {}
        doc["admin"] = admin
    admin["listen"] = "unix//run/caddy/caddy.sock|0600"
    path.write_text(json.dumps(doc))
    print("caddy listen updated", flush=True)


if __name__ == "__main__":
    main()
