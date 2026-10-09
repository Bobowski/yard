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
from urllib.parse import urlsplit

import stario.responses as responses
from stario import App, Context, Writer
from stario.responses import JsonValue

from app.apply import run_args
from app.config import Config
from app.errors import YardError
from app.gitcmd import prepare_git
from app.machine import (
    JOB_RETRY,
    Paths,
    Result,
    RunState,
    _post_unix,
    caddy_document,
    deploy_slot,
    ensure_caddy,
    plan_job,
    routes_from,
    run_cmd,
    run_job,
    yard_route,
)
from app.podman import (
    HostUse,
    Listed,
    Podman,
    inspect_args,
    log_hit,
    page_logs,
    parse_info,
    parse_ps,
    parse_stats,
    ps_args,
    stats_args,
)
from app.rows import build_label, dir_size, disk_capacity, format_bytes, slot_rows, total_row
from app.store import Slot, Store, check_name, check_origin, parse_duration, public_origin
from app.text import SlotEdit, apply_slot_keys, image_sha, parse_env

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
        self._partial: set[str] = set()

    async def boot(self, app: App) -> None:
        self._app = app
        try:
            self._ensure_host_slot()
        except YardError as exc:
            log.warning("host slot: %s", exc)
        try:
            self._claim_yard_deploy()
        except YardError as exc:
            log.warning("yard deploy: %s", exc)
        try:
            self.store.interrupt_deploys()
        except YardError as exc:
            log.warning("deploys: %s", exc)
        self._install_hooks()

        try:
            await self.podman.ensure_network("yard")
        except Exception as exc:
            log.warning("network: %s", exc)
        if self.config.caddy_sock is not None:
            try:
                await ensure_caddy(self.config.root, self.config.caddy_sock)
            except Exception as exc:
                log.warning("caddy: %s", exc)
        await self.push_caddy()
        await self.start_jobs()

    def _install_hooks(self) -> None:
        repos = self.paths.root / "repos"
        if not repos.is_dir():
            return
        for path in repos.iterdir():
            if not path.is_dir() or not path.name.endswith(".git"):
                continue
            try:
                self._install_hook(path, path.name.removesuffix(".git"))
            except OSError as exc:
                log.warning("hook: %s", exc)

    def _install_hook(self, directory: Path, name: str) -> None:
        _write_hook(
            directory / "hooks" / "reference-transaction",
            name,
            self.config.token_file,
            self.config.hook_url,
        )

    def _ensure_host_slot(self) -> None:
        domain = self.config.domain
        if not domain or not self.paths.repo("yard").exists():
            return
        try:
            self.store.repo("yard")
        except YardError as exc:
            if exc.code != "not_found":
                raise
            self.store.create_repo("yard")
        try:
            slot = self.store.slot("yard")
        except YardError as exc:
            if exc.code != "not_found":
                raise
            self.store.create_slot("yard", "yard", "origin/main", domain, "", "")
            return
        if slot.domain != domain:
            self.store.patch_slot(slot.name, None, domain, None, None, slot.env)

    def _claim_yard_deploy(self) -> None:
        """The new yard process records the upgrade that replaced the previous process."""
        sha = os.environ.get("SHA", "").strip()
        image = os.environ.get("IMAGE", "").strip()
        if not sha or not image:
            return
        try:
            self.store.slot("yard")
        except YardError as exc:
            if exc.code != "not_found":
                raise
            return
        last = self.store.last_deploy("yard")
        if last is not None and last.status == "running" and last.sha == sha:
            self.store.set_image("yard", image)
            self.store.finish_deploy(last.id, "ok", "")
            return
        if last is None:
            self.store.set_image("yard", image)
            self.store.finish_deploy(self.store.begin_deploy("yard", sha), "ok", "")

    async def aclose(self) -> None:
        self._watch = False
        tasks = list(self._jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self.store.close()

    async def create_repo(self, name: str, force: bool, origin: str = "") -> JsonValue:
        name = name.strip()
        check_name(name)
        if origin:
            origin = check_origin(origin)
        async with self._repo_lock:
            try:
                self.store.repo(name)
            except YardError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise YardError(
                    409,
                    "exists",
                    "already exists",
                    context={"name": name},
                    help_text="Pick another name.",
                )

            # A leftover directory is a create that stopped after git init.
            directory = self.paths.repo(name)
            if directory.exists():
                if not force:
                    raise YardError(
                        409,
                        "directory_exists",
                        f"{directory} already exists and {name} is not recorded.",
                        context={"name": name, "directory": str(directory)},
                        help_text="A previous create may have stopped.",
                        example=f"yard repo new {name} --force",
                    )
                try:
                    shutil.rmtree(directory)
                except OSError as exc:
                    raise YardError(
                        409,
                        "directory_exists",
                        f"Yard could not remove {directory} ({exc}).",
                        context={"directory": str(directory)},
                        help_text="Remove that directory, then create the repo again.",
                        example=f"yard repo new {name}",
                    ) from exc

            directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                await run_cmd("git", "init", "--bare", "--initial-branch=main", str(directory))
                await self._allow_partial(directory)
            except RuntimeError as exc:
                raise YardError(
                    500,
                    "git_init",
                    f"git init failed: {exc}",
                    context={"directory": str(directory)},
                    help_text="If that directory is still there, finish the create with --force.",
                    example=f"yard repo new {name} --force",
                ) from exc
            ref = ""
            if origin:
                try:
                    ref = await track_origin(directory, origin)
                except YardError:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise
                except RuntimeError as exc:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise YardError(
                        500,
                        "git_fetch",
                        f"Yard could not fetch {public_origin(origin)}: {_hide_secret(origin, str(exc))}",
                        context={"name": name},
                        help_text="The address has to be a git repo that this host can read.",
                        example="yard init --remote https://github.com/org/heldso --domain heldso.example.com",
                    ) from exc
            try:
                self._install_hook(directory, name)
            except OSError as exc:
                raise YardError(
                    500,
                    "hook_failed",
                    f"Yard could not write the reference hook ({exc}).",
                    help_text="Finish the create with --force after the directory can be written.",
                    example=f"yard repo new {name} --force",
                ) from exc

            # The row is last, so a failed init does not claim the name.
            try:
                self.store.create_repo(name)
            except YardError as exc:
                raise YardError(
                    500,
                    "repo_unrecorded",
                    f"git init succeeded but saving {name} failed: {exc}",
                    context={"name": name},
                    help_text="The directory is there. Finish the create with --force.",
                    example=f"yard repo new {name} --force",
                ) from exc
            repo = self.store.repo(name)
        body: JsonValue = {"name": repo.name, "created": repo.created}
        if ref:
            body["ref"] = ref
        return body

    def delete_repo(self, name: str) -> None:
        self.store.delete_repo(name)
        directory = self.paths.repo(name)
        if directory.exists():
            shutil.rmtree(directory)

    async def create_slot(self, keys: dict[str, str]) -> tuple[Slot, list[str], JsonValue]:
        name = keys.get("SLOT_NAME", "").strip()
        repo = keys.get("SLOT_REPO", "").strip()
        if not name or not repo:
            raise YardError(
                400,
                "bad_slot",
                "SLOT_NAME and SLOT_REPO are required",
                help_text="A slot needs both a name and a repo.",
                example='{"SLOT_NAME": "heldso", "SLOT_REPO": "heldso"}',
            )
        slot = self.store.create_slot(
            name,
            repo,
            keys.get("REF", "").strip(),
            keys.get("DOMAIN", "").strip(),
            keys.get("SLOT_DIRECTORY", "").strip(),
            keys.get("SLOT_INTERVAL", "").strip(),
        )

        # The row is already saved. A later failure removes it.
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

    async def update_slot(self, name: str, edit: SlotEdit) -> tuple[Slot, JsonValue | None]:
        before = self.store.slot(name)
        slot = self.store.patch_slot(name, edit.ref, edit.domain, edit.directory, edit.every, edit.env)
        if slot.every and not before.every:
            await self._project(name, "down")
        await self.push_caddy()
        await self.sync_job(slot.name)
        # A new ref, directory, or env value is a new container. Domain and the timer are not.
        if edit.ref is not None or edit.directory is not None or edit.env != before.env:
            return slot, await self.deploy_existing(slot)
        return slot, None

    async def _project(self, name: str, command: str) -> None:
        env_path = self.paths.run_dir(name) / "compose.env"
        if not env_path.is_file():
            return
        try:
            compose = parse_env(env_path.read_text()).get("COMPOSE_FILE", "")
        except YardError:
            return
        if not compose:
            return
        try:
            args, env = run_args(
                root=str(self.paths.root),
                sock=self.config.podman_sock,
                project=name,
                compose=compose,
                env_file=str(env_path),
                command=command,
                slot=name,
            )
            await self.podman.run(*args, env=env, program="docker-compose", timeout=None)
        except YardError:
            pass

    async def delete_slot(self, name: str) -> None:
        await self._project(name, "purge")

        self.store.delete_slot(name)
        data = self.paths.data(name)
        if data.exists():
            shutil.rmtree(data)
        run = self.paths.run_dir(name)
        if run.exists():
            shutil.rmtree(run)

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
        sizes: dict[Path, str] = {}
        for row in rows:
            sha, expected, build = facts.get(row.slot, ("", "", ""))
            row.sha, row.expected, row.build = sha, expected, build
            name = row.slot or row.container
            if not name:
                continue
            path = self.paths.data(name)
            if path not in sizes:
                sizes[path] = format_bytes(dir_size(path)) if path.is_dir() else ""
            row.data = sizes[path]

        host = await self.host_use()
        rows.append(total_row(rows, host.cpus, host.mem, host.disk))
        return [asdict(row) for row in rows]

    async def host_use(self) -> HostUse:
        host = HostUse()
        try:
            host = parse_info(await self.podman.run("info", "--format", "json"))
        except YardError:
            host = HostUse()
        if not host.disk:
            host.disk = disk_capacity(self.paths.root)
        return host

    async def slot_facts(self, slots: list[Slot]) -> dict[str, tuple[str, str, str]]:
        tips: dict[str, dict[str, str]] = {}
        facts: dict[str, tuple[str, str, str]] = {}
        for slot in slots:
            if slot.repo not in tips:
                tips[slot.repo] = await self._ref_tips(slot.repo)
            expected = tips[slot.repo].get(slot.ref, "")
            last = self.store.last_deploy(slot.name)
            build = "" if last is None else build_label(last.status, last.started, last.ended)
            facts[slot.name] = (image_sha(slot.live_image), expected, build)
        return facts

    async def _ref_tips(self, repo: str) -> dict[str, str]:
        """One ref list for the repo. The key is the slot ref."""
        directory = self.paths.repo(repo)
        if not directory.is_dir():
            return {}
        try:
            text = await run_cmd(
                "git",
                "-C",
                str(directory),
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                "refs/heads",
                "refs/remotes/origin",
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

    async def deploy_existing(self, slot: Slot) -> JsonValue:
        try:
            sha, found = await resolve_ref(self.paths.repo(slot.repo), slot.ref)
        except Exception as exc:
            raise YardError(
                500,
                "deploy_failed",
                f"slot {slot.name} is saved. Yard could not read {slot.repo} {slot.ref}: {exc}",
                context={"slot": slot.name, "repo": slot.repo, "ref": slot.ref},
                help_text=f"Fix the repo, then push to {slot.ref}.",
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
                f"slot {slot.name} is saved. Deploy of {slot.ref} ({sha}) failed:\n{out.text.strip()}",
                context={"slot": slot.name, "ref": slot.ref, "sha": sha},
                help_text=f"Push to {slot.ref} to deploy again.",
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

        # Another deploy of this slot is already running. Wait for its result.
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
        deploy_id = self.store.begin_deploy(slot.name, sha)
        built_path = self.paths.run_dir(slot.name) / "apply.built"
        built_path.unlink(missing_ok=True)
        result = await deploy_slot(self.podman, self.paths, slot, sha, self.config.podman_sock)
        if result.pending:
            return Outcome(f"slot {slot.name}: upgrading {result.image}\n{result.log}\n", "")
        built = built_path.read_text().strip() if built_path.is_file() else ""
        status = "failed" if result.error else "ok"
        error = result.error

        # The live image changes only after this build is the one that is up.
        if not error and result.image:
            try:
                self.store.set_image(slot.name, result.image)
            except YardError as exc:
                error = str(exc)
                status = "failed"
                result = Result(result.image, result.log + f"{exc}\n", error)
        try:
            self.store.finish_deploy(deploy_id, status, built)
        except YardError as exc:
            result = Result(result.image, result.log + f"{exc}\n", error or str(exc))
            status = "failed"
            if not error:
                error = str(exc)

        await self.sync_job(slot.name)
        return Outcome(f"slot {slot.name}: {status} {result.image}\n{result.log}\n", error)

    async def log_page(
        self,
        slot: str,
        role: str,
        service: str,
        sha: str,
        tail: int,
        cursor: str,
    ) -> JsonValue:
        targets = await self.log_targets(slot, role, service, sha)
        return await self._log_text(targets, tail, cursor)

    async def container_log_page(self, name: str, tail: int, cursor: str) -> JsonValue:
        inspect_args(name)
        return await self._log_text([("", name)], tail, cursor)

    async def _log_text(self, targets: list[tuple[str, str]], tail: int, cursor: str) -> JsonValue:
        if not targets:
            return {"log": "", "cursor": "", "more": False}
        chunks: list[str] = []
        nxt = ""
        more = False
        for label, container in targets:
            args = ["logs", "--timestamps"]
            if cursor and len(targets) == 1:
                args.extend(("--since", cursor))
            else:
                args.extend(("--tail", str(tail)))
            args.append(container)
            text, one_next, one_more = page_logs(await self.podman.run(*args), cursor, tail)
            if len(targets) > 1 and text:
                text = "".join(f"{label}  {line}\n" for line in text.splitlines() if line)
            chunks.append(text)
            if len(targets) == 1:
                nxt, more = one_next, one_more
        return {"log": "".join(chunks), "cursor": nxt, "more": more}

    async def log_targets(self, slot: str, role: str, service: str, sha: str) -> list[tuple[str, str]]:
        if slot:
            self.store.slot(slot)
        listed = parse_ps(await self.podman.run(*ps_args()))
        found: list[tuple[str, str]] = []
        for item in listed.values():
            if not log_hit(item, slot=slot, role=role, sha=sha):
                continue
            label = item.service or item.name
            if service and label != service:
                continue
            shown = label
            if not slot and item.slot and shown and item.slot != shown:
                shown = f"{item.slot}/{shown}"
            found.append((shown or item.name, item.name))
        found.sort()
        if service and not found:
            where = slot or "this host"
            raise YardError(
                404,
                "no_service",
                f"service {service} is not in {where}",
                help_text="The service name is the name in the compose file.",
                example="yard logs --slot heldso --service app",
            )
        return found

    async def git_repo(self, name: str, args: list[str]) -> JsonValue:
        self.store.repo(name)
        directory = self.paths.repo(name)
        if not directory.exists():
            raise YardError(
                404,
                "not_found",
                f"repo {name} is not on this host",
                help_text="Create the repo, then run the git command.",
                example=f"yard repo new {name}",
            )
        call = prepare_git(args)
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            text = await run_cmd("git", "-C", str(directory), *call.argv, timeout=60, env=env)
        except RuntimeError as exc:
            raise YardError(
                400,
                "git_failed",
                str(exc) or "git failed",
                help_text="Git printed that text.",
                example=f"yard git {name} log -1",
            ) from exc
        if call.delete_branch:
            note = await self._drop_deleted(name, call.delete_branch)
            removed = "".join(f"{line}\n" for line in note.splitlines() if line.startswith("slot "))
            if removed:
                text = f"{text}\n{removed}" if text else removed
        if text and not text.endswith("\n"):
            text += "\n"
        return {"output": text}

    async def hook_push(self, repo: str, new: str, ref: str) -> tuple[int, str]:
        watched_ref = slot_ref(ref)
        if not watched_ref:
            return 200, f"skip {ref}\n"

        # A ref of all zeros deletes the branch.
        if not new.strip("0"):
            return 200, await self._drop_deleted(repo, watched_ref)
        slots = self.store.slots_for_repo(repo)
        text = ""
        if ref.startswith("refs/heads/"):
            text = await self._drop_merged(slots, watched_ref, new)
        watched = [slot for slot in slots if slot.ref == watched_ref]
        if not watched:
            if not text:
                text = f"no slot watches {repo} {watched_ref}\n"
            return 200, text

        failed = False
        for slot in watched:
            last = self.store.last_deploy(slot.name)
            if last is not None and last.sha == new and last.status == "ok":
                text += f"slot {slot.name}: current\n"
                continue
            if self.skip_deploy:
                text += f"slot {slot.name}: deploy skipped\n"
                continue
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

    async def push_caddy(self) -> None:
        sock = self.config.caddy_sock
        if sock is None:
            return
        try:
            routes = routes_from(self.store.list_slots())
            if self.config.domain and all(route.host != self.config.domain for route in routes):
                yard = yard_route(self.config.domain)
                if yard is not None:
                    routes.append(yard)
            await asyncio.to_thread(_post_unix, sock, caddy_document(sock, routes))
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

        # Cancel outside the lock. The task may be waiting on it.
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
                    await run_job(self.podman, self.paths, slot, slot.live_image, self.config.podman_sock)
                except Exception as exc:
                    log.warning("job %s: warning: %s", name, exc)
                    await _wait_job(wake, JOB_RETRY)
                    continue
                log.info("job %s: started", name)
        except asyncio.CancelledError:
            return

    async def _job_runs(self, slot: str) -> list[RunState]:
        running = await self.podman.run(
            "ps",
            "--filter",
            f"label=yard.slot={slot}",
            "--filter",
            "label=yard.role=job",
            "--filter",
            "status=running",
            "--format",
            "json",
        )
        finished = await self.podman.run(
            "ps",
            "-a",
            "--filter",
            f"label=yard.slot={slot}",
            "--filter",
            "label=yard.role=job",
            "--filter",
            "status=exited",
            "--sort",
            "created",
            "-n",
            "1",
            "--format",
            "json",
        )
        return job_states(running, finished)

    async def _lane_for(self, name: str) -> _Lane:
        async with self._lane_lock:
            lane = self._lanes.get(name)
            if lane is None:
                lane = _Lane()
                self._lanes[name] = lane
            return lane

    async def _allow_partial(self, directory: Path) -> None:
        key = str(directory)
        if key in self._partial:
            return
        try:
            await run_cmd("git", "-C", key, "config", "uploadpack.allowFilter", "true")
        except RuntimeError as exc:
            log.warning("partial clone: %s", exc)
            return
        self._partial.add(key)

    async def serve_git(self, c: Context, w: Writer) -> None:
        token = self.store.token_ok(_basic_password(c.req.headers.get("authorization") or "") or "")
        if token is None:
            w.headers.set("www-authenticate", 'Basic realm="yard"')
            responses.text(w, "unauthorized\n", 401)
            return
        repo = git_repo(c.req.path)
        directory = self.paths.repo(repo)
        if not repo or not directory.is_dir():
            responses.text(w, "not found\n", 404)
            return
        await self._allow_partial(directory)
        backend = _git_backend()
        if not backend:
            responses.text(w, "git-http-backend not found\n", 500)
            return

        # git-http-backend is a CGI program. The request becomes its environment.
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


async def track_origin(directory: Path, origin: str) -> str:
    await run_cmd("git", "-C", str(directory), "remote", "add", "origin", origin)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    await run_cmd("git", "-C", str(directory), *prepare_git(["fetch"]).argv, timeout=60, env=env)
    try:
        await run_cmd("git", "-C", str(directory), "remote", "set-head", "origin", "-a", timeout=30, env=env)
        head = await run_cmd("git", "-C", str(directory), "symbolic-ref", "refs/remotes/origin/HEAD")
    except RuntimeError:
        return "main"
    name = head.removeprefix("refs/remotes/origin/").strip()
    return name or "main"


def _hide_secret(url: str, text: str) -> str:
    password = urlsplit(url).password
    if password:
        return text.replace(password, "*****")
    return text


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
        # A missing branch is not a failure. The next push creates it.
        if any(phrase in text.lower() for phrase in _MISSING_REV):
            return "", False
        raise RuntimeError(text or "rev-parse failed")
    return (text, True) if text else ("", False)


def job_states(running_raw: str, finished_raw: str) -> list[RunState]:
    """The running jobs, plus the newest finished job."""
    running = [item for item in parse_ps(running_raw).values() if item.id]
    finished = [item for item in parse_ps(finished_raw).values() if item.id]
    states = [RunState(item.id, True, _real_time(item.started), None) for item in running]
    seen = {state.id for state in states}
    newest = _newest_finished(finished)
    if newest is not None and newest.id not in seen:
        states.append(RunState(newest.id, False, _real_time(newest.started), _real_time(newest.exited)))
    return states


def _newest_finished(items: list[Listed]) -> Listed | None:
    found = [item for item in items if item.id]
    if not found:
        return None
    empty = datetime.min.replace(tzinfo=UTC)
    return max(found, key=lambda item: item.exited or item.created or empty)


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
    text = header + "\n" + _HOOK_BODY
    if path.is_file() and path.read_text() == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
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
