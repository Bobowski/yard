"""Git on the host: one plain clone per app, synced from Origin."""

import asyncio
import os
import subprocess
from pathlib import Path

from app.shared import YardError

# The fetch token never lands in argv or .git/config. The helper reads it from the env.
_CREDENTIAL = 'credential.helper=!f() { echo username=x-access-token; echo "password=$YARD_GIT_TOKEN"; }; f'


async def git(*args: str, cwd: Path | None = None, timeout: float | None = 60, token: str = "") -> str:
    """Run git with prompts off. Stderr joins stdout. A nonzero exit is a YardError."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = ["git"]
    if token:
        env["YARD_GIT_TOKEN"] = token
        cmd += ["-c", "credential.helper=", "-c", _CREDENTIAL]
    if cwd is not None:
        cmd.extend(("-C", str(cwd)))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        if timeout is None:
            out, _ = await proc.communicate()
        else:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        proc.kill()
        raise YardError(502, "git", "git timed out") from exc
    text = (out or b"").decode(errors="replace").strip()
    if proc.returncode:
        raise YardError(502, "git", text or "git failed")
    return text


async def sync(clone: Path, url: str, ref: str, token: str = "") -> str:
    """Reset the clone to the remote tip of ref. Returns the new HEAD sha.

    The clone is never deleted: the yard app's Caddy mounts a file from it.
    """
    if not (clone / ".git").exists():
        clone.mkdir(parents=True, exist_ok=True)
        await git("init", cwd=clone)
        await git("remote", "add", "origin", url, cwd=clone)
    await git("remote", "set-url", "origin", url, cwd=clone)
    await git("fetch", "--depth", "1", "origin", ref, cwd=clone, token=token)
    await git("reset", "--hard", "FETCH_HEAD", cwd=clone)
    await git("clean", "-ffdx", cwd=clone)
    return await git("rev-parse", "HEAD", cwd=clone)


async def remote_tip(clone: Path | None, url: str, ref: str, token: str = "") -> str:
    """The remote sha for a branch. Empty when the branch is not on the remote."""
    cwd = clone if clone is not None and (clone / ".git").exists() else None
    text = await git("ls-remote", url, f"refs/heads/{ref}", cwd=cwd, token=token)
    want = f"refs/heads/{ref}"
    for line in text.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == want:
            return sha.strip()
    return ""


def head(clone: Path) -> str:
    """HEAD of a clone, or empty when there is none."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if proc.returncode:
        return ""
    return proc.stdout.strip()
