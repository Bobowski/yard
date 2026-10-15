"""Process settings. The yard command keeps its own client file."""

import os
from dataclasses import dataclass
from pathlib import Path


def hook_url(host: str, port: str) -> str:
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    podman_sock: str
    podman_bin: str
    hook_url: str
    domain: str
    skip_deploy: bool
    boot: bool
    remote: bool

    @property
    def token_file(self) -> Path:
        return self.root / "tokens" / "admin.token"

    def repo(self, name: str) -> Path:
        return self.root / "repos" / f"{name}.git"

    def project_dir(self, slot: str) -> Path:
        return self.root / "projects" / slot

    def data(self, slot: str) -> Path:
        return self.project_dir(slot) / "data"

    def env(self, slot: str) -> Path:
        return self.project_dir(slot) / "env"

    def worktree(self, slot: str) -> Path:
        """One worktree for the slot. The next deploy checks out a new SHA here."""
        return self.project_dir(slot) / "worktree"

    def autosave(self) -> Path:
        """Caddy writes this after an admin API change. The yard project owns it."""
        return self.project_dir("yard") / "caddy" / "config" / "caddy" / "autosave.json"

    def caddy_data(self) -> Path:
        return self.project_dir("yard") / "caddy" / "data"

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.environ.get("YARD_ROOT", "yard-data")).resolve()
        boot = os.environ.get("YARD_BOOT", "1") != "0"
        host = os.environ.get("STARIO_HOST", "127.0.0.1") or "127.0.0.1"
        port = os.environ.get("STARIO_PORT", "8000") or "8000"
        podman_sock = os.environ.get("YARD_PODMAN_SOCK", "")
        remote = bool(podman_sock or os.environ.get("CONTAINER_HOST"))
        return cls(
            root=root,
            podman_sock=podman_sock,
            podman_bin=os.environ.get("YARD_PODMAN_BIN", "podman"),
            hook_url=os.environ.get("YARD_HOOK_URL") or hook_url(host, port),
            domain=os.environ.get("YARD_DOMAIN", ""),
            skip_deploy=os.environ.get("YARD_SKIP_DEPLOY", "") == "1",
            boot=boot,
            remote=remote,
        )
