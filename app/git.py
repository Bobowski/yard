"""Git on the host: bare repos, checkouts, and the smart-HTTP bridge."""

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

import stario.responses as responses
from stario import Context, Writer

from app.config import Config
from app.errors import YardError
from app.values import check_origin, check_ref

_MISSING_REV = (
    "needed a single revision",
    "unknown revision",
    "ambiguous argument",
    "bad revision",
    "not a valid",
)


async def git(*args: str, cwd: Path | None = None, timeout: float | None = 60) -> str:
    """Run git with prompts off. Stderr joins stdout. A nonzero exit is a RuntimeError."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = ["git"]
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
        raise RuntimeError("git timed out") from exc
    text = (out or b"").decode(errors="replace").strip()
    if proc.returncode:
        raise RuntimeError(text or "git failed")
    return text


async def init_repo(path: Path) -> None:
    await git("init", "--bare", "--initial-branch=main", str(path))
    # A lazy clone asks for blobs. The bare repo has to allow it.
    await git("config", "uploadpack.allowFilter", "true", cwd=path)


async def track_origin(path: Path, origin: str) -> str:
    """Point the repo at the remote and fetch one level. Returns the remote default branch."""
    await git("remote", "add", "origin", origin, cwd=path)
    await git(*prepare_git(["fetch"]).argv, cwd=path)
    try:
        await git("remote", "set-head", "origin", "-a", cwd=path, timeout=30)
        head = await git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=path)
    except RuntimeError:
        return "main"
    return head.removeprefix("refs/remotes/origin/").strip() or "main"


async def resolve(path: Path, ref: str) -> str:
    """The sha for a ref, or empty when the ref does not exist."""
    if not path.exists():
        raise RuntimeError(f"repo directory {path} is missing")
    try:
        return await git("rev-parse", "--verify", "--end-of-options", ref, cwd=path)
    except RuntimeError as exc:
        # A missing branch is not a failure. The next push creates it.
        if any(phrase in str(exc).lower() for phrase in _MISSING_REV):
            return ""
        raise


async def tips(path: Path) -> dict[str, str]:
    """One ref list for the repo. Keys are branch and origin/branch names."""
    if not path.is_dir():
        return {}
    try:
        text = await git(
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            "refs/heads",
            "refs/remotes/origin",
            cwd=path,
        )
    except RuntimeError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        sha, sep, name = line.partition(" ")
        if not sep or not sha:
            continue
        if name.startswith("refs/heads/"):
            found[name.removeprefix("refs/heads/")] = sha
        elif name.startswith("refs/remotes/origin/") and not name.endswith("/HEAD"):
            short = name.removeprefix("refs/remotes/origin/")
            if short:
                found[f"origin/{short}"] = sha
    return found


async def is_ancestor(path: Path, tip: str, into: str) -> bool:
    try:
        await git("merge-base", "--is-ancestor", tip, into, cwd=path)
    except RuntimeError:
        return False
    return True


async def checkout(config: Config, repo: str, directory: str, sha: str) -> Path:
    """Extract one revision into checkouts/. A finished extract stays for the next deploy of it."""
    build = config.checkout(repo, directory, sha)
    if build.is_dir():
        return build
    tmp = build.with_name(build.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    sub = directory.strip("/")
    args = ["archive", "--format=tar", sha]
    strip = 0
    if sub:
        args.append(sub)
        strip = len([part for part in sub.split("/") if part])
    tar_args = ["tar", "-x", "-C", str(tmp)]
    if strip:
        tar_args.append(f"--strip-components={strip}")

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    read_fd, write_fd = os.pipe()
    try:
        archive = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(config.repo(repo)),
            *args,
            stdout=write_fd,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    finally:
        os.close(write_fd)
    try:
        tar = await asyncio.create_subprocess_exec(
            *tar_args,
            stdin=read_fd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:
        archive.kill()
        await archive.wait()
        raise
    finally:
        os.close(read_fd)
    assert archive.stderr is not None and tar.stderr is not None
    git_raw, tar_raw, git_code, tar_code = await asyncio.gather(
        archive.stderr.read(),
        tar.stderr.read(),
        archive.wait(),
        tar.wait(),
    )
    git_err = git_raw.decode(errors="replace")
    tar_err = tar_raw.decode(errors="replace")
    if git_code or tar_code:
        shutil.rmtree(tmp, ignore_errors=True)
        if git_code:
            detail = git_err.strip()
            low = detail.lower()
            if sub and ("pathspec" in low or "did not match" in low):
                raise RuntimeError(f"directory {directory!r} is not in the repo at this revision")
            raise RuntimeError(detail or "git archive failed")
        raise RuntimeError(tar_err.strip() or "tar failed")

    # The rename is the commit point. A stale extract is replaced whole.
    shutil.rmtree(build, ignore_errors=True)
    os.replace(tmp, build)
    return build


def slot_ref(ref: str) -> str:
    """The slot ref for a git ref. A push uses the branch. A fetch uses origin/<branch>."""
    if ref.startswith("refs/heads/"):
        name = ref.removeprefix("refs/heads/")
        if name and name != "HEAD":
            return name
        return ""
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix):
        name = ref.removeprefix(prefix)
        if name and name != "HEAD":
            return f"origin/{name}"
    return ""


def repo_name(path: str) -> str:
    name = path.removeprefix("/git/").strip("/").split("/", 1)[0].removesuffix(".git")
    if not name or ".." in name:
        return ""
    return name


def hide_secret(url: str, text: str) -> str:
    password = urlsplit(url).password
    if password:
        return text.replace(password, "*****")
    return text


_HOOK_BODY = """state="${1:-}"
if [ "$state" != "committed" ]; then
  cat >/dev/null || true
  exit 0
fi
token=$(cat "$YARD_TOKEN_FILE")
url="$YARD_HOOK_URL"
while read _old new ref; do
  body=$(printf '{"repo":"%s","new":"%s","ref":"%s"}' "$YARD_REPO" "$new" "$ref")
  resp=$(curl -sS --connect-timeout 5 -H "Authorization: Bearer $token" -H "Content-Type: application/json" \\
    -d "$body" -w "\\n%{http_code}" "$url") || {
    printf '%s\\n' "$resp" >&2
    exit 1
  }
  code=$(printf '%s\\n' "$resp" | tail -n 1)
  text=$(printf '%s\\n' "$resp" | sed '$d')
  printf '%s\\n' "$text"
  case "$code" in
    2*) ;;
    *) exit 1 ;;
  esac
done
"""


def write_hook(path: Path, name: str, token_file: Path, hook_url: str) -> None:
    header = "\n".join(
        [
            "#!/bin/sh",
            "set -eu",
            f"YARD_TOKEN_FILE={_quote(str(token_file))}",
            f"YARD_HOOK_URL={_quote(hook_url + '/api/v1/hooks/push')}",
            f"YARD_REPO={_quote(name)}",
            "export YARD_TOKEN_FILE YARD_HOOK_URL YARD_REPO",
        ]
    )
    text = header + "\n" + _HOOK_BODY
    if path.is_file() and path.read_text() == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


async def http_backend(root: Path, token_name: str, c: Context, w: Writer) -> None:
    """git-http-backend is a CGI program. The request becomes its environment."""
    repo = repo_name(c.req.path)
    directory = root / "repos" / f"{repo}.git"
    if not repo or not directory.is_dir():
        responses.text(w, "not found\n", 404)
        return
    backend = _git_backend()
    if not backend:
        responses.text(w, "git-http-backend not found\n", 500)
        return

    path_info = c.req.path.removeprefix("/git") or "/"
    query = c.req.query_bytes.decode("latin-1")
    body = await c.req.body()
    env = os.environ.copy()
    env.update(
        {
            "GIT_PROJECT_ROOT": str(root / "repos"),
            "GIT_HTTP_EXPORT_ALL": "",
            "REMOTE_USER": token_name,
            "AUTH_TYPE": "Basic",
            "REQUEST_METHOD": c.req.method,
            "PATH_INFO": path_info,
            "QUERY_STRING": query,
            "CONTENT_TYPE": c.req.headers.get("content-type") or "",
            "CONTENT_LENGTH": str(len(body)),
            "GATEWAY_INTERFACE": "CGI/1.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "SCRIPT_NAME": "/git",
            "REQUEST_URI": c.req.path + (f"?{query}" if query else ""),
        }
    )
    proc = await asyncio.create_subprocess_exec(
        backend,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, _err = await proc.communicate(body)
    status, headers, payload = _split_cgi(out or b"")
    content_type = "text/plain"
    for key, value in headers:
        if key.lower() == "content-type":
            content_type = value
        else:
            w.headers.add(key, value)
    w.respond(payload, content_type.encode(), status)


def _git_backend() -> str:
    found = shutil.which("git-http-backend")
    if found:
        return found
    try:
        path = subprocess.check_output(["git", "--exec-path"], text=True).strip()
    except Exception:
        return ""
    candidate = str(Path(path) / "git-http-backend")
    return candidate if Path(candidate).is_file() else ""


def _split_cgi(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = raw.partition(b"\n\n")
    status = 200
    headers: list[tuple[str, str]] = []
    for line in head.decode("latin-1", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if key.lower() == "status":
            status = int(value.split()[0])
            continue
        headers.append((key.strip(), value))
    return status, headers, body if sep else raw


# --- the allowed commands for yard git <repo> ---

_COMMANDS = {
    "log",
    "show",
    "rev-parse",
    "ls-tree",
    "cat-file",
    "for-each-ref",
    "branch",
    "remote",
    "fetch",
    "gc",
}
_FETCH = ["fetch", "--depth", "1", "origin", "+refs/heads/*:refs/remotes/origin/*"]
_FLAGS = {
    "-1",
    "-5",
    "-10",
    "-20",
    "--oneline",
    "--stat",
    "--name-only",
    "--name-status",
    "--decorate",
    "--graph",
    "--all",
    "--verify",
    "--short",
    "--abbrev-ref",
    "--symbolic-full-name",
    "--quiet",
    "-r",
    "-d",
    "-l",
    "-t",
    "-s",
    "-p",
    "-e",
    "--long",
    "-v",
    "-vv",
    "-a",
    "--list",
    "-D",
}
_FLAG_WITH_VALUE = re.compile(
    r"^--(format|pretty|count|sort|max-count|since|until|author|grep)=[%()A-Za-z0-9:_. ,/@^{}~<>-]*$"
)
_WORD = re.compile(r"^[A-Za-z0-9._/@^{}~:+-]+$")  # refs, paths, ranges like HEAD~3..main
_EXAMPLE = {
    "log": "yard git heldso log -1",
    "show": "yard git heldso log -1",
    "rev-parse": "yard git heldso rev-parse HEAD",
    "ls-tree": "yard git heldso ls-tree HEAD",
    "cat-file": "yard git heldso cat-file -t HEAD",
    "for-each-ref": "yard git heldso for-each-ref refs/heads",
}


@dataclass(frozen=True, slots=True)
class GitCall:
    argv: list[str]
    delete_branch: str = ""


def prepare_git(args: list[str]) -> GitCall:
    """One flat rule: a known command, then flags or words that never start with a dash."""
    if not args or len(args) > 16 or any(_bad_token(arg) for arg in args):
        _unknown()
    command, *rest = args
    if command not in _COMMANDS:
        _unknown()
    if command == "fetch":
        if rest not in ([], ["origin"]):
            _bad_args("yard git heldso fetch")
        return GitCall(list(_FETCH))
    if command == "gc":
        if rest and rest != ["--prune=now"]:
            _bad_args("yard git heldso gc")
        return GitCall(["gc", *rest])
    if command == "branch":
        if rest[:1] in (["-d"], ["-D"]):
            if len(rest) != 2:
                _bad_args("yard git heldso branch -D feature")
            check_ref(rest[1])
            return GitCall(["branch", rest[0], rest[1]], delete_branch=rest[1])
        if all(arg in _FLAGS for arg in rest):
            return GitCall(["branch", *rest])
        _bad_args("yard git heldso branch -a")
    if command == "remote":
        if not rest or rest == ["-v"]:
            return GitCall(["remote", *rest])
        if len(rest) == 3 and rest[0] == "set-url" and rest[1] == "origin":
            return GitCall(["remote", "set-url", "origin", check_origin(rest[2])])
        _bad_args("yard git heldso remote set-url origin https://github.com/org/heldso")
    for arg in rest:
        if arg in _FLAGS or _FLAG_WITH_VALUE.match(arg):
            continue
        if not arg.startswith("-") and _WORD.match(arg):
            continue
        _bad_args(_EXAMPLE[command])
    return GitCall(args)


def _bad_token(arg: str) -> bool:
    return not arg or any(ch in arg for ch in "\n\r\x00")


def _unknown() -> NoReturn:
    names = ", ".join(sorted(_COMMANDS))
    raise YardError(
        400,
        "bad_git",
        "that git command is not available on a bare repo",
        help_text=f"The commands are {names}.",
        example="yard git heldso log -1",
    )


def _bad_args(example: str) -> NoReturn:
    raise YardError(
        400,
        "bad_git",
        "that git command does not take those arguments",
        help_text="Use the arguments from the example.",
        example=example,
    )
