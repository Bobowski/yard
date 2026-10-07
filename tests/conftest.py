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
        token = (tmp_path / "data" / "yard" / "admin.token").read_text().strip()
        yield client, token, tmp_path


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}
