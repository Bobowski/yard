from datetime import UTC, datetime, timedelta

import pytest
from stario.testing import TestClient

from app.main import bootstrap


@pytest.fixture
async def yard(tmp_path, monkeypatch):
    monkeypatch.setenv("YARD_ROOT", str(tmp_path))
    monkeypatch.setenv("YARD_SKIP_DEPLOY", "1")
    monkeypatch.setenv("YARD_BOOT", "0")
    monkeypatch.setenv("YARD_HOOK_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_PODMAN_BIN", "yard-podman-missing")
    async with TestClient(bootstrap) as client:
        token = (tmp_path / "tokens" / "admin.token").read_text().strip()
        yield client, token, tmp_path


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def local(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "host": "127.0.0.1"}


NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


def stamp(**delta) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_report() -> dict:
    return {
        "host": {
            "cpus": 4,
            "mem": 4_000_000_000,
            "disk": 200_000_000_000,
            "cpu_used": 2.0,
            "mem_used": 30_000_000,
            "data_used": 10_000_000,
        },
        "rows": [
            {
                "project": "heldso",
                "container": "heldso-app",
                "service": "app",
                "state": "running",
                "running": True,
                "health": "healthy",
                "exit_code": 0,
                "started": stamp(hours=2),
                "exited": "",
                "image": "localhost/heldso:abc1234",
                "sha": "abc1234",
                "tip": "f00ba12",
                "domain": "heldso.example.com",
                "cpu": "1.5%",
                "mem": "20MB / 1GB",
                "data": 10_000_000,
                "deploy": {
                    "id": 3,
                    "status": "ok",
                    "sha": "abc1234",
                    "started": stamp(minutes=120),
                    "built": stamp(minutes=119),
                    "ended": stamp(minutes=118),
                },
            },
            {
                "project": "shop",
                "container": "shop-app",
                "service": "app",
                "state": "exited",
                "running": False,
                "health": "",
                "exit_code": 1,
                "started": "",
                "exited": stamp(hours=3),
                "image": "localhost/shop:def5678",
                "sha": "def5678",
                "tip": "",
                "domain": "",
                "cpu": "",
                "mem": "",
                "data": 0,
                "deploy": {
                    "id": 2,
                    "status": "failed",
                    "sha": "def5678",
                    "started": stamp(hours=3),
                    "built": "",
                    "ended": stamp(hours=3),
                },
            },
            {
                "project": "nightly",
                "container": "",
                "service": "",
                "state": "absent",
                "running": False,
                "health": "idle",
                "exit_code": 0,
                "started": "",
                "exited": "",
                "image": "",
                "sha": "",
                "tip": "",
                "domain": "",
                "trigger": "every 5m",
                "cpu": "",
                "mem": "",
                "data": 0,
                "deploy": None,
            },
            {
                "project": "",
                "container": "caddy",
                "service": "",
                "state": "running",
                "running": True,
                "health": "",
                "exit_code": 0,
                "started": stamp(days=1),
                "exited": "",
                "image": "docker.io/library/caddy:2",
                "sha": "",
                "tip": "",
                "domain": "",
                "cpu": "0.5%",
                "mem": "10MB / 1GB",
                "data": 0,
                "deploy": None,
            },
        ],
    }
