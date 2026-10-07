"""Process settings. The yard command keeps its own client file."""

import os
from dataclasses import dataclass
from pathlib import Path

YARD_IMAGE = "ghcr.io/bobowski/yard:latest"


def hook_url(host: str, port: str) -> str:
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    caddy_sock: Path | None
    podman_sock: str
    podman_bin: str
    public_port: int
    hook_url: str
    domain: str
    skip_deploy: bool
    boot: bool
    remote: bool

    @property
    def data_dir(self) -> Path:
        return self.root / "data" / "yard"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "admin.token"

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.environ.get("YARD_ROOT", "yard-data")).resolve()
        boot = os.environ.get("YARD_BOOT", "1") != "0"
        host = os.environ.get("STARIO_HOST", "127.0.0.1") or "127.0.0.1"
        port = os.environ.get("STARIO_PORT", "8000") or "8000"
        sock = os.environ.get("YARD_CADDY_SOCK") or None
        if sock is None and boot:
            sock = str(root / "run" / "caddy.sock")
        podman_sock = os.environ.get("YARD_PODMAN_SOCK", "")
        remote = bool(podman_sock or os.environ.get("CONTAINER_HOST"))
        public = os.environ.get("YARD_PUBLIC_PORT") or port
        return cls(
            root=root,
            caddy_sock=Path(sock) if sock else None,
            podman_sock=podman_sock,
            podman_bin=os.environ.get("YARD_PODMAN_BIN", "podman"),
            public_port=int(public),
            hook_url=os.environ.get("YARD_HOOK_URL") or hook_url(host, port),
            domain=os.environ.get("YARD_DOMAIN", ""),
            skip_deploy=os.environ.get("YARD_SKIP_DEPLOY", "") == "1",
            boot=boot,
            remote=remote,
        )
