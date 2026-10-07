"""Yard operations. HTTP handlers call this. Podman and git stay behind it."""

import asyncio
import base64
import logging
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import stario.responses as responses
from stario import App, Context, Writer
from stario.responses import JsonValue

from app.config import YARD_IMAGE, Config
from app.errors import YardError
from app.machine import (
    JOB_RETRY,
    Paths,
    Result,
    RunState,
    build_image,
    deploy_slot,
    ensure_caddy,
    load_caddy,
    plan_job,
    routes_from,
    run_cmd,
    run_job,
    yard_route,
)
from app.podman import (
    Podman,
    ids_for_slot,
    latest_for_slot,
    page_logs,
    parse_inspect,
    parse_ps,
    parse_stats,
    ps_args,
    stats_args,
)
from app.rows import slot_rows
from app.store import Slot, Store, check_name, parse_duration
from app.text import SlotEdit, apply_slot_keys, image_sha

log = logging.getLogger("yard")

_MISSING_REV = (
    "needed a single revision",
    "unknown revision",
    "ambiguous argument",
    "bad revision",
    "not a valid",
)


@dataclass(slots=True)
class Outcome:
    text: str
    error: str = ""


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    running: bool = False
    pending: str = ""
    waiters: list[asyncio.Future[Outcome]] = field(default_factory=list)


class Yard:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.podman = Podman(config.podman_bin, remote=config.remote)
        self.paths = Paths(config.root)
        self.skip_deploy = config.skip_deploy
        self._repo_lock = asyncio.Lock()
        self._lanes: dict[str, _Lane] = {}
        self._lane_lock = asyncio.Lock()
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._job_lock = asyncio.Lock()
        self._watch = False
        self._app: App | None = None

    async def boot(self, app: App) -> None:
        self._app = app
        self._note_swap_boot()
        try:
            self.store.interrupt_deploys()
        except YardError as exc:
            log.warning("deploys: %s", exc)
        try:
            await self.podman.ensure_network("yard")
        except Exception as exc:
            log.warning("network: %s", exc)
        if self.config.caddy_sock is not None:
            try:
                await ensure_caddy(self.podman, self.config.root, self.config.caddy_sock)
            except Exception as exc:
                log.warning("caddy: %s", exc)
        await self.push_caddy()
        await self.start_jobs()

    async def aclose(self) -> None:
        self._watch = False
        tasks = list(self._jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self.store.close()

    async def create_repo(self, name: str, force: bool) -> JsonValue:
        name = name.strip()
        check_name(name)
        async with self._repo_lock:
            try:
                self.store.repo(name)
            except YardError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise YardError(409, "exists", "already exists")
            directory = self.paths.repo(name)
            if directory.exists():
                if not force:
                    raise YardError(
                        409,
                        "directory_exists",
                        f"{directory} already exists and {name} is not recorded. "
                        f"A previous create may have stopped. Run: yard repo new {name} --force",
                    )
                try:
                    shutil.rmtree(directory)
                except OSError as exc:
                    raise YardError(
                        409,
                        "directory_exists",
                        f"Yard could not remove {directory} ({exc}). "
                        f"Remove that directory, then run: yard repo new {name}",
                    ) from exc
            directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                await run_cmd("git", "init", "--bare", "--initial-branch=main", str(directory))
            except RuntimeError as exc:
                raise YardError(
                    500,
                    "git_init",
                    f"git init failed: {exc}. If {directory} is still there, run: yard repo new {name} --force",
                ) from exc
            try:
                _write_hook(directory / "hooks" / "post-receive", name, self.config.token_file, self.config.hook_url)
            except OSError as exc:
                raise YardError(
                    500,
                    "hook_failed",
                    f"Yard could not write the receive hook ({exc}). Run: yard repo new {name} --force",
                ) from exc
            try:
                self.store.create_repo(name)
            except YardError as exc:
                raise YardError(
                    500,
                    "repo_unrecorded",
                    f"git init succeeded but saving {name} failed: {exc}. Run: yard repo new {name} --force",
                ) from exc
            repo = self.store.repo(name)
        return {"name": repo.name, "created": repo.created}

    def delete_repo(self, name: str) -> None:
        self.store.delete_repo(name)
        directory = self.paths.repo(name)
        if directory.exists():
            shutil.rmtree(directory)

    async def create_slot(self, keys: dict[str, str]) -> tuple[Slot, list[str], JsonValue]:
        name = keys.get("SLOT_NAME", "").strip()
        repo = keys.get("SLOT_REPO", "").strip()
        if not name or not repo:
            raise YardError(400, "bad_slot", "SLOT_NAME and SLOT_REPO are required")
        slot = self.store.create_slot(
            name,
            repo,
            keys.get("SLOT_BRANCH", "").strip(),
            keys.get("SLOT_DOMAIN", "").strip(),
            keys.get("SLOT_DIRECTORY", "").strip(),
            keys.get("SLOT_INTERVAL", "").strip(),
        )
        try:
            edit = apply_slot_keys(slot, keys)
            slot = self.store.patch_slot(name, edit.ref, edit.domain, edit.directory, edit.every, edit.env)
        except Exception:
            try:
                self.store.delete_slot(name)
            except YardError:
                pass
            raise
        await self.push_caddy()
        try:
            note = await self.deploy_existing(slot)
        finally:
            await self.sync_job(slot.name)
        return slot, edit.warn, note

    async def update_slot(self, name: str, edit: SlotEdit) -> Slot:
        slot = self.store.patch_slot(name, edit.ref, edit.domain, edit.directory, edit.every, edit.env)
        if slot.every:
            try:
                await self.podman.remove(slot.name)
            except YardError:
                pass
        await self.push_caddy()
        await self.sync_job(slot.name)
        return slot

    async def delete_slot(self, name: str) -> None:
        try:
            await self.podman.run("inspect", "--type", "container", name, timeout=5)
        except YardError:
            pass
        else:
            await self.podman.remove(name)
        self.store.delete_slot(name)
        data = self.paths.data(name)
        if data.exists():
            shutil.rmtree(data)
        await self.push_caddy()
        await self.sync_job(name)

    async def list_rows(self, running_only: bool) -> list[JsonValue]:
        slots = self.store.list_slots()
        listed = parse_ps(await self.podman.run(*ps_args()))
        try:
            samples = parse_stats(await self.podman.run(*stats_args()))
        except YardError:
            samples = {}
        rows = slot_rows(slots, listed, samples, running_only=running_only)
        facts = await self.slot_facts(slots)
        for row in rows:
            sha, expected, build = facts.get(row.slot, ("", "", ""))
            row.sha, row.expected, row.build = sha, expected, build
        return [asdict(row) for row in rows]

    async def slot_facts(self, slots: list[Slot]) -> dict[str, tuple[str, str, str]]:
        facts: dict[str, tuple[str, str, str]] = {}
        for slot in slots:
            expected = ""
            try:
                sha, found = await resolve_ref(self.paths.repo(slot.repo), slot.ref)
            except Exception:
                sha, found = "", False
            if found:
                expected = sha
            last = self.store.last_deploy(slot.name)
            facts[slot.name] = (image_sha(slot.live_image), expected, last.status if last else "")
        return facts

    async def deploy_existing(self, slot: Slot) -> JsonValue:
        try:
            sha, found = await resolve_ref(self.paths.repo(slot.repo), slot.ref)
        except Exception as exc:
            raise YardError(
                500,
                "deploy_failed",
                f"slot {slot.name} is saved. Yard could not read {slot.repo} {slot.ref}: {exc}. "
                f"Fix the repo, then push to {slot.ref}",
            ) from exc
        if not found:
            return {
                "status": "waiting",
                "message": f"Branch {slot.ref} has no revision yet. The next push to {slot.ref} deploys this slot.",
            }
        if self.skip_deploy:
            return {
                "status": "skipped",
                "sha": sha,
                "message": f"Revision {sha} is on {slot.ref}. Deploy did not run.",
            }
        out = await self.run_deploy(slot.name, sha)
        if out.error:
            raise YardError(
                500,
                "deploy_failed",
                f"slot {slot.name} is saved. Deploy of {slot.ref} ({sha}) failed:\n{out.text.strip()}\n"
                f"Push to {slot.ref} to deploy again.",
            )
        return {"status": "ok", "sha": sha, "message": out.text.strip()}

    async def run_deploy(self, name: str, sha: str) -> Outcome:
        lane = await self._lane_for(name)
        async with lane.lock:
            lane.pending = sha
            if lane.running:
                waiter: asyncio.Future[Outcome] | None = asyncio.get_running_loop().create_future()
                lane.waiters.append(waiter)
            else:
                lane.running = True
                waiter = None
        if waiter is not None:
            return await waiter
        last = Outcome("")
        try:
            while True:
                async with lane.lock:
                    sha = lane.pending
                    lane.pending = ""
                last = await self.deploy_once(name, sha)
                async with lane.lock:
                    if lane.pending:
                        continue
                    lane.running = False
                    for fut in lane.waiters:
                        if not fut.done():
                            fut.set_result(last)
                    lane.waiters.clear()
                    return last
        except Exception as exc:
            async with lane.lock:
                lane.running = False
                lane.pending = ""
                for fut in lane.waiters:
                    if not fut.done():
                        fut.set_exception(exc)
                lane.waiters.clear()
            raise

    async def deploy_once(self, name: str, sha: str) -> Outcome:
        try:
            slot = self.store.slot(name)
        except YardError as exc:
            return Outcome(f"{name}: {exc}\n", str(exc))
        deploy_id = self.store.begin_deploy(slot.name, slot.repo, sha)
        result = await deploy_slot(self.podman, self.paths, slot, sha)
        status = "failed" if result.error else "ok"
        error = result.error
        if not error and result.image:
            try:
                self.store.set_image(slot.name, result.image)
            except YardError as exc:
                error = str(exc)
                status = "failed"
                result = Result(result.image, result.log + f"{exc}\n", error)
        try:
            self.store.finish_deploy(deploy_id, status, result.image, result.log)
        except YardError as exc:
            result = Result(result.image, result.log + f"{exc}\n", error or str(exc))
            status = "failed"
            if not error:
                error = str(exc)
        await self.sync_job(slot.name)
        return Outcome(f"slot {slot.name}: {status} {result.image}\n{result.log}\n", error)

    async def log_page(self, name: str, tail: int, cursor: str, container: str) -> JsonValue:
        self.store.slot(name)
        target = container or await self.log_target(name)
        args = ["logs", "--timestamps"]
        if cursor:
            args.extend(("--since", cursor))
        else:
            args.extend(("--tail", str(tail)))
        args.append(target)
        text, nxt, more = page_logs(await self.podman.run(*args), cursor, tail)
        return {"log": text, "cursor": nxt, "more": more}

    async def log_target(self, slot: str) -> str:
        item = latest_for_slot(parse_ps(await self.podman.run(*ps_args())), slot)
        if item is None or not item.name:
            return slot
        return item.name

    async def hook_push(self, repo: str, old: str, new: str, ref: str) -> tuple[int, str]:
        del old
        branch = ref.removeprefix("refs/heads/")
        if branch == ref or not branch:
            return 200, f"skip {ref}\n"
        if not new.strip("0"):
            return 200, await self._drop_deleted(repo, branch)
        slots = self.store.slots_for_repo(repo)
        text = await self._drop_merged(slots, branch, new)
        watched = [slot for slot in slots if slot.ref == branch]
        if not watched:
            if not text:
                text = f"no slot watches {repo} {branch}\n"
            return 200, text
        if self.skip_deploy:
            for slot in watched:
                text += f"slot {slot.name}: deploy skipped\n"
            return 200, text
        failed = False
        for slot in watched:
            out = await self.run_deploy(slot.name, new)
            text += out.text
            failed = failed or bool(out.error)
        return (500 if failed else 200), text

    async def _drop_deleted(self, repo: str, branch: str) -> str:
        text = ""
        for slot in self.store.slots_for_repo(repo):
            if slot.ref == branch:
                text += await self._drop_slot(slot, f"branch {branch} was deleted")
        return text or f"branch {branch} deleted\n"

    async def _drop_merged(self, slots: list[Slot], branch: str, sha: str) -> str:
        text = ""
        for slot in slots:
            if slot.ref == branch:
                continue
            merged, why = await self._branch_merged(slot, branch, sha)
            if merged:
                text += await self._drop_slot(slot, why)
        return text

    async def _drop_slot(self, slot: Slot, why: str) -> str:
        try:
            await self.delete_slot(slot.name)
        except Exception as exc:
            return f"slot {slot.name}: {exc}\n"
        return f"slot {slot.name} removed: {why}\n"

    async def _branch_merged(self, slot: Slot, into_branch: str, into_sha: str) -> tuple[bool, str]:
        try:
            tip, found = await resolve_ref(self.paths.repo(slot.repo), slot.ref)
        except Exception:
            return False, ""
        if not found or not into_sha:
            return False, ""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.paths.repo(slot.repo)),
            "merge-base",
            "--is-ancestor",
            tip,
            into_sha,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() == 0:
            return True, f"branch {slot.ref} is in {into_branch}"
        return False, ""

    async def save_settings(
        self,
        domain: str | None,
        email: str | None,
        self_repo: str | None,
        self_ref: str | None,
        self_image: str | None,
    ) -> JsonValue:
        current = self.store.settings()
        if domain is not None:
            current.domain = domain.strip()
        if email is not None:
            current.email = email.strip()
        if self_repo is not None and self_repo.strip() and self_image is not None and self_image.strip():
            raise YardError(400, "one_source", "set self_image or self_repo")
        if self_ref is not None:
            current.self_ref = self_ref.strip()
        if self_repo is not None and self_repo.strip():
            current.self_repo = self_repo.strip()
            current.self_image = ""
        elif self_image is not None and self_image.strip():
            current.self_image = self_image.strip()
            current.self_repo = ""
        elif self_repo is not None or self_image is not None:
            current.self_repo = ""
            current.self_image = YARD_IMAGE
        self.store.save_settings(current)
        saved = self.store.settings()
        await self.push_caddy()
        return {
            "domain": saved.domain,
            "email": saved.email,
            "self_repo": saved.self_repo,
            "self_ref": saved.self_ref,
            "self_image": saved.self_image,
        }

    async def update_yard(self) -> JsonValue:
        settings = self.store.settings()
        if not settings.self_repo:
            image = YARD_IMAGE
            sha = ""
            self.store.set_meta("swap_image", image)
            self.store.set_meta("swap_status", "pulling")
            try:
                await self.podman.pull(image)
            except Exception as exc:
                self.store.set_meta("swap_status", "failed")
                raise YardError(500, "pull_failed", str(exc)) from exc
            built = f"pull {image}"
        else:
            try:
                sha = await run_cmd(
                    "git", "-C", str(self.paths.repo(settings.self_repo)), "rev-parse", settings.self_ref
                )
            except RuntimeError as exc:
                raise YardError(400, "no_rev", str(exc)) from exc
            image = f"localhost/yard:{sha}"
            self.store.set_meta("swap_image", image)
            self.store.set_meta("swap_status", "building")
            try:
                built = await build_image(self.podman, self.paths, settings.self_repo, "", sha, image)
            except Exception as exc:
                self.store.set_meta("swap_status", "failed")
                raise YardError(500, "build_failed", str(exc)) from exc
        try:
            current = (await self.podman.run("inspect", "--format", "{{.Image}}", "yard")).strip()
            if current:
                await self.podman.run("tag", current, "localhost/yard:previous")
        except YardError:
            pass
        self.store.set_meta("swap_status", "swapping")
        try:
            await self.swap(image)
        except Exception as exc:
            self.store.set_meta("swap_status", "failed")
            raise YardError(500, "swap_failed", str(exc)) from exc
        return {"image": image, "sha": sha, "status": "swapping", "log": built, "container": "yard-swap"}

    async def rollback(self) -> None:
        await self.swap("localhost/yard:previous")

    async def swap(self, image: str) -> None:
        sock = self.config.podman_sock or "/run/podman/podman.sock"
        await self.podman.run(
            "run",
            "-d",
            "--rm",
            "--name",
            "yard-swap",
            "--log-driver",
            "journald",
            "-v",
            f"{sock}:/run/podman/podman.sock",
            "-e",
            "CONTAINER_HOST=unix:///run/podman/podman.sock",
            "-e",
            f"YARD_IMAGE={image}",
            "-e",
            f"YARD_ROOT={self.paths.root}",
            "-e",
            f"YARD_PODMAN_SOCK={sock}",
            "-e",
            f"YARD_CADDY_SOCK={self.config.caddy_sock or ''}",
            "-e",
            f"YARD_PUBLIC_PORT={self.config.public_port}",
            "quay.io/podman/stable",
            "sh",
            "-c",
            _SWAP,
            timeout=None,
        )

    def swap_status(self) -> JsonValue:
        return {
            "image": self.store.meta("swap_image"),
            "status": self.store.meta("swap_status"),
            "container": "yard-swap",
        }

    def _note_swap_boot(self) -> None:
        if self.store.meta("swap_status") != "swapping":
            return
        got = os.environ.get("YARD_IMAGE", "")
        if not got:
            return
        self.store.set_meta("swap_status", "ready" if got == self.store.meta("swap_image") else "rolled_back")

    async def push_caddy(self) -> None:
        sock = self.config.caddy_sock
        if sock is None:
            return
        try:
            routes = routes_from(self.store.list_slots())
            yard = yard_route(self.store.settings().domain)
            if yard is not None:
                routes.append(yard)
            await load_caddy(sock, self.store.settings().email, routes)
        except Exception as exc:
            log.warning("caddy: %s", exc)

    async def start_jobs(self) -> None:
        async with self._job_lock:
            self._watch = True
        for slot in self.store.list_slots():
            if slot.every:
                await self.sync_job(slot.name)

    async def sync_job(self, name: str) -> None:
        task: asyncio.Task[None] | None = None
        async with self._job_lock:
            if not self._watch or self._app is None:
                return
            try:
                slot = self.store.slot(name)
            except YardError:
                slot = None
            if slot is None or not slot.every:
                task = self._jobs.pop(name, None)
                self._wakes.pop(name, None)
            elif name not in self._jobs:
                wake = asyncio.Event()
                self._wakes[name] = wake
                self._jobs[name] = self._app.create_task(self._watch_job(name, wake), name=f"job-{name}")
                return
            else:
                self._wakes[name].set()
                return
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _watch_job(self, name: str, wake: asyncio.Event) -> None:
        log.info("job %s: watching", name)
        try:
            while True:
                try:
                    slot = self.store.slot(name)
                except YardError:
                    return
                if not slot.every:
                    return
                try:
                    every = parse_duration(slot.every)
                except ValueError as exc:
                    log.warning("job %s: warning: %s", name, exc)
                    await _wait_job(wake, JOB_RETRY)
                    continue
                try:
                    runs = await self._job_runs(name)
                except Exception as exc:
                    log.warning("job %s: warning: %s", name, exc)
                    await _wait_job(wake, JOB_RETRY)
                    continue
                plan = plan_job(every, slot.live_image, runs, datetime.now(UTC))
                if plan.warn:
                    log.warning("job %s: warning: %s", name, plan.warn)
                if plan.wait_id:
                    try:
                        await self.podman.run("wait", plan.wait_id, timeout=None)
                    except Exception as exc:
                        log.warning("job %s: warning: %s", name, exc)
                        await _wait_job(wake, JOB_RETRY)
                    continue
                if plan.sleep > 0:
                    await _wait_job(wake, plan.sleep)
                    continue
                if not plan.start:
                    await _wait_job(wake, JOB_RETRY)
                    continue
                try:
                    started = await run_job(self.podman, self.paths, slot, slot.live_image)
                except Exception as exc:
                    log.warning("job %s: warning: %s", name, exc)
                    await _wait_job(wake, JOB_RETRY)
                    continue
                log.info("job %s: started %s", name, started)
        except asyncio.CancelledError:
            return

    async def _job_runs(self, slot: str) -> list[RunState]:
        raw = await self.podman.run("ps", "-a", "--filter", f"label=yard.slot={slot}", "--format", "json")
        ids = ids_for_slot(raw, slot)
        if not ids:
            return []
        rows = parse_inspect(await self.podman.run("inspect", *ids))
        return [RunState(row.id, row.running, _real_time(row.started), _real_time(row.finished)) for row in rows]

    async def _lane_for(self, name: str) -> _Lane:
        async with self._lane_lock:
            lane = self._lanes.get(name)
            if lane is None:
                lane = _Lane()
                self._lanes[name] = lane
            return lane

    async def serve_git(self, c: Context, w: Writer) -> None:
        token = self.store.token_ok(_basic_password(c.req.headers.get("authorization") or "") or "")
        if token is None:
            w.headers.set("www-authenticate", 'Basic realm="yard"')
            responses.text(w, "unauthorized\n", 401)
            return
        repo = git_repo(c.req.path)
        if not repo or not self.paths.repo(repo).is_dir():
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
                "GIT_PROJECT_ROOT": str(self.paths.root / "repos"),
                "GIT_HTTP_EXPORT_ALL": "",
                "REMOTE_USER": token.name,
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


def git_repo(path: str) -> str:
    name = path.removeprefix("/git/").strip("/").split("/", 1)[0].removesuffix(".git")
    if not name or ".." in name:
        return ""
    return name


async def resolve_ref(directory: Path, ref: str) -> tuple[str, bool]:
    if not directory.exists():
        raise RuntimeError(f"repo directory {directory} is missing")
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(directory),
        "rev-parse",
        "--verify",
        "--end-of-options",
        ref,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = (out or b"").decode(errors="replace").strip()
    if proc.returncode:
        if any(phrase in text.lower() for phrase in _MISSING_REV):
            return "", False
        raise RuntimeError(text or "rev-parse failed")
    return (text, True) if text else ("", False)


def _real_time(stamp: datetime | None) -> datetime | None:
    if stamp is None or stamp.year < 2:
        return None
    return stamp


async def _wait_job(wake: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(wake.wait(), max(seconds, 0))
    except TimeoutError:
        return
    wake.clear()


_HOOK_BODY = """token=$(cat "$YARD_TOKEN_FILE")
url="$YARD_HOOK_URL"
while read old new ref; do
  body=$(printf '{"repo":"%s","old":"%s","new":"%s","ref":"%s"}' "$YARD_REPO" "$old" "$new" "$ref")
  resp=$(curl -sS -H "Authorization: Bearer $token" -H "Content-Type: application/json" \\
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

_SWAP = """set -eu
run() {
  podman --remote run -d --name yard --restart=always --network yard --log-driver journald \\
    --group-add keep-groups \\
    -p "127.0.0.1:${YARD_PUBLIC_PORT}:8000" \\
    -v "$YARD_ROOT:$YARD_ROOT" \\
    -v "$YARD_PODMAN_SOCK:/run/podman/podman.sock" \\
    -e CONTAINER_HOST=unix:///run/podman/podman.sock \\
    -e "YARD_IMAGE=$1" \\
    -e "YARD_ROOT=$YARD_ROOT" \\
    -e "YARD_PODMAN_SOCK=$YARD_PODMAN_SOCK" \\
    -e "YARD_CADDY_SOCK=$YARD_CADDY_SOCK" \\
    -e "YARD_PUBLIC_PORT=$YARD_PUBLIC_PORT" \\
    "$1"
}
podman --remote rm -f yard || true
run "$YARD_IMAGE"
sleep 2
if ! podman --remote exec yard /workspace/.venv/bin/python -c \\
  'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health")'; then
  podman --remote rm -f yard || true
  run localhost/yard:previous
  exit 1
fi
"""


def _write_hook(path: Path, name: str, token_file: Path, hook_url: str) -> None:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + _HOOK_BODY)
    path.chmod(0o755)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _basic_password(header: str) -> str | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip()).decode()
    except Exception:
        return None
    _user, sep, password = raw.partition(":")
    return password if sep else None


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
