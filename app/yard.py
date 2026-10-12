"""Yard operations. HTTP handlers call this. Podman and git stay behind it."""

import asyncio
import logging
import os
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app import caddy, compose, deploy, git, podman
from app.config import Config
from app.errors import YardError
from app.podman import Podman
from app.store import Slot, Store
from app.values import check_name, check_origin, format_env, parse_env, public_origin

if TYPE_CHECKING:
    from stario import App
    from stario.responses import JsonValue

log = logging.getLogger("yard")


class Yard:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.podman = Podman(config.podman_bin, remote=config.remote)
        self.skip_deploy = config.skip_deploy
        self._repo_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._apply_task: asyncio.Task[None] | None = None
        self._app: App | None = None

    async def boot(self, app: App) -> None:
        self._app = app
        (self.config.root / "env").mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.ensure_host_slot()
        except YardError as exc:
            log.warning("host slot: %s", exc)
        try:
            await self._claim_yard_deploy()
        except YardError as exc:
            log.warning("yard deploy: %s", exc)
        try:
            self.store.interrupt_deploys()
        except YardError as exc:
            log.warning("deploys: %s", exc)
        await self._prepare_repos()

        await self.ensure_network()
        # The apply image build pulls a base image. Health must answer before that finishes.
        # The 0.1 swap waits 60 seconds for /health.
        self._apply_task = app.create_task(self._build_apply_image(), name="apply-image")
        if self.config.caddy_sock is not None:
            try:
                (self.config.root / "data" / "caddy" / "lib").mkdir(parents=True, exist_ok=True)
                self.config.caddy_sock.parent.mkdir(parents=True, exist_ok=True)
                await caddy.wait_socket(self.config.caddy_sock)
            except Exception as exc:
                log.warning("caddy: %s", exc)
        await self.push_caddy()
        await self.start_jobs()

    async def _build_apply_image(self) -> None:
        try:
            code, text = await compose.build_apply(self.config)
            if code:
                log.warning("apply image: %s", text.strip().splitlines()[-1] if text.strip() else code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("apply image: %s", exc)

    async def aclose(self) -> None:
        tasks = list(self._jobs.values())
        if self._apply_task is not None:
            tasks.append(self._apply_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self.store.close()

    async def ensure_network(self) -> None:
        """The `yard` bridge: apply containers and apps join it, Caddy resolves them on it."""
        try:
            await self.podman.run("network", "exists", "yard")
        except YardError:
            try:
                await self.podman.run("network", "create", "yard")
            except YardError as exc:
                log.warning("network: %s", exc)

    async def _prepare_repos(self) -> None:
        """Hooks and the partial-clone flag on every repo."""
        repos = self.config.root / "repos"
        if not repos.is_dir():
            return
        for path in repos.iterdir():
            if not path.is_dir() or not path.name.endswith(".git"):
                continue
            name = path.name.removesuffix(".git")
            try:
                # A lazy clone asks for blobs. The bare repo has to allow it.
                await git.git("config", "uploadpack.allowFilter", "true", cwd=path)
            except RuntimeError as exc:
                log.warning("partial clone: %s", exc)
            try:
                git.write_hook(
                    path / "hooks" / "reference-transaction",
                    name,
                    self.config.token_file,
                    self.config.hook_url,
                )
            except OSError as exc:
                log.warning("hook: %s", exc)

    def ensure_host_slot(self) -> None:
        """The yard slot: a slot with no domain is valid, it just has no route."""
        domain = self.config.domain
        if not self.config.repo("yard").exists():
            return
        compose.ensure_env(self.config.env("yard"))
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
            self.store.create_slot("yard", "yard", "origin/main", "", domain, "")
            return
        if slot.domain != domain:
            self.store.update_slot(slot.name, domain=domain)

    async def _claim_yard_deploy(self) -> None:
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
        if last is not None and last.status == "running":
            if last.sha == sha:
                self.store.set_image("yard", image)
                self.store.finish_deploy(last.id, "ok", "")
            else:
                # The upgrade rolled back and the old process is speaking.
                # The apply log says why the new build never came up.
                try:
                    text = await self.podman.run("logs", "yard-apply-yard")
                except Exception:
                    text = ""
                self.store.finish_deploy(last.id, "failed", text)
            return
        if last is None:
            self.store.set_image("yard", image)
            self.store.finish_deploy(self.store.begin_deploy("yard", sha, image), "ok", "")

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
            directory = self.config.repo(name)
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
                await git.init_repo(directory)
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
                    ref = await git.track_origin(directory, origin)
                except RuntimeError as exc:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise YardError(
                        500,
                        "git_fetch",
                        f"Yard could not fetch {public_origin(origin)}: {git.hide_secret(origin, str(exc))}",
                        context={"name": name},
                        help_text="The address has to be a git repo that this host can read.",
                        example="yard init --remote https://github.com/org/heldso --domain heldso.example.com",
                    ) from exc
            try:
                git.write_hook(
                    directory / "hooks" / "reference-transaction",
                    name,
                    self.config.token_file,
                    self.config.hook_url,
                )
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
        directory = self.config.repo(name)
        if directory.exists():
            shutil.rmtree(directory)

    async def create_slot(
        self,
        name: str,
        repo: str,
        ref: str = "",
        directory: str = "",
        domain: str = "",
        trigger: str = "",
        env: dict[str, str] | None = None,
    ) -> tuple[Slot, deploy.Outcome]:
        slot = self.store.create_slot(name, repo, ref, directory, domain, trigger)
        self.write_env(slot.name, env or {})
        await self.push_caddy()
        try:
            outcome = await self.deploy_ref(slot)
        finally:
            await self.sync_job(slot.name)
        return slot, outcome

    async def update_slot(
        self,
        name: str,
        *,
        ref: str | None = None,
        directory: str | None = None,
        domain: str | None = None,
        trigger: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[Slot, deploy.Outcome | None]:
        before = self.store.slot(name)
        slot = self.store.update_slot(name, ref=ref, dir=directory, domain=domain, trigger=trigger)
        env_changed = env is not None and env != self.read_env(name)
        if env is not None:
            self.write_env(name, env)
        if slot.job and not before.job:
            await self._down(name)
        await self.push_caddy()
        await self.sync_job(slot.name)

        # A new ref, directory, or env value is a new container. Domain and the timer are not.
        changed = slot.ref != before.ref or slot.directory != before.directory or env_changed
        if not changed:
            return slot, None
        return slot, await self.deploy_ref(slot)

    async def delete_slot(self, name: str) -> None:
        slot = self.store.slot(name)
        project = compose.saved_project(self.config, slot)
        if project is not None:
            try:
                await compose.apply(self.config, name, "purge", project)
            except Exception as exc:
                log.warning("purge %s: %s", name, exc)

        self.store.delete_slot(name)
        self.config.env(name).unlink(missing_ok=True)
        data = self.config.data(name)
        if data.exists():
            shutil.rmtree(data)

        await self.push_caddy()
        await self.sync_job(name)

    def read_env(self, name: str) -> dict[str, str]:
        path = self.config.env(name)
        if not path.is_file():
            return {}
        return parse_env(path.read_text())

    def write_env(self, name: str, env: dict[str, str]) -> None:
        path = self.config.env(name)
        compose.ensure_env(path)
        path.write_text(format_env(env))

    async def _down(self, name: str) -> None:
        try:
            slot = self.store.slot(name)
        except YardError:
            return
        project = compose.saved_project(self.config, slot)
        if project is None:
            return
        try:
            await compose.apply(self.config, name, "down", project)
        except Exception:
            pass

    async def deploy_ref(self, slot: Slot, sha: str = "") -> deploy.Outcome:
        """Deploy one revision. An empty sha resolves the slot ref."""
        if not sha:
            try:
                sha = await git.resolve(self.config.repo(slot.repo), slot.ref)
            except RuntimeError as exc:
                raise YardError(
                    500,
                    "deploy_failed",
                    f"slot {slot.name} is saved. Yard could not read {slot.repo} {slot.ref}: {exc}",
                    context={"slot": slot.name, "repo": slot.repo, "ref": slot.ref},
                    help_text=f"Fix the repo, then push to {slot.ref}.",
                ) from exc
            if not sha:
                return deploy.Outcome(
                    "waiting",
                    log=f"branch {slot.ref} has no revision yet. The next push to {slot.ref} deploys this slot.\n",
                )
        image = compose.image_tag(slot.repo, slot.directory, sha)
        if self.skip_deploy:
            return deploy.Outcome("skipped", image, f"revision {sha} is on {slot.ref}. Deploy did not run.\n")
        outcome = await self.run_deploy(slot.name, sha)
        if outcome.status == "failed":
            raise YardError(
                500,
                "deploy_failed",
                f"slot {slot.name} is saved. Deploy of {slot.ref} ({sha}) failed:\n{outcome.log.strip()}",
                context={"slot": slot.name, "ref": slot.ref, "sha": sha},
                help_text=f"Push to {slot.ref} to deploy again.",
            )
        return outcome

    async def run_deploy(self, name: str, sha: str) -> deploy.Outcome:
        async with self._locks.setdefault(name, asyncio.Lock()):
            try:
                slot = self.store.slot(name)
            except YardError as exc:
                return deploy.Outcome("failed", error=str(exc))
            last = self.store.last_deploy(name)
            if last is not None and last.sha == sha and last.status == "ok":
                return deploy.Outcome("current", last.image or compose.image_tag(slot.repo, slot.directory, sha))
            try:
                tip = await git.resolve(self.config.repo(slot.repo), slot.ref)
            except RuntimeError as exc:
                return deploy.Outcome("failed", error=str(exc))
            if tip and tip != sha:
                # A newer push already arrived. The caller for the tip builds it.
                return deploy.Outcome("superseded", compose.image_tag(slot.repo, slot.directory, tip))
            outcome = await deploy.deploy(self, slot, sha)
            await self.sync_job(name)
            return outcome

    async def hook_push(self, repo: str, new: str, ref: str) -> tuple[int, str]:
        watched_ref = git.slot_ref(ref)
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
            if self.skip_deploy:
                text += f"slot {slot.name}: deploy skipped\n"
                continue
            out = await self.run_deploy(slot.name, new)
            text += _line(slot.name, out)
            failed = failed or out.status == "failed"
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
            tip = await git.resolve(self.config.repo(slot.repo), slot.ref)
        except RuntimeError:
            return False, ""
        if not tip or not into_sha:
            return False, ""
        if await git.is_ancestor(self.config.repo(slot.repo), tip, into_sha):
            return True, f"branch {slot.ref} is in {into_branch}"
        return False, ""

    async def push_caddy(self) -> None:
        sock = self.config.caddy_sock
        if sock is None:
            return
        try:
            found = caddy.routes(self.store.list_slots(), self.config.domain)
            # Keep the admin socket the running Caddy already uses.
            # A 0.1 host listens on /srv/yard/run/caddy.sock, not /run/caddy/caddy.sock.
            listen = caddy.admin_listen(self.config.root / "data" / "caddy" / "autosave.json")
            await asyncio.to_thread(caddy.load, sock, caddy.document(found, listen))
        except Exception as exc:
            log.warning("caddy: %s", exc)

    async def start_jobs(self) -> None:
        for slot in self.store.list_slots():
            if slot.every_seconds is not None:
                await self.sync_job(slot.name)

    async def sync_job(self, name: str) -> None:
        if self._app is None:
            return
        task: asyncio.Task[None] | None = None
        try:
            slot = self.store.slot(name)
        except YardError:
            slot = None
        if slot is None or slot.every_seconds is None:
            task = self._jobs.pop(name, None)
            self._wakes.pop(name, None)
        elif name not in self._jobs:
            wake = asyncio.Event()
            self._wakes[name] = wake
            self._jobs[name] = self._app.create_task(self._job_loop(name, wake), name=f"job-{name}")
            return
        else:
            self._wakes[name].set()
            return

        # Cancel outside the dict work. The task may be waiting on the wake.
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _last_run(self, name: str) -> datetime | None:
        """The created time of the last job-run container; None when ps has no answer."""
        try:
            listed = podman.parse_ps(await self.podman.run("ps", "-a", "--format", "json"))
        except Exception as exc:
            log.warning("job %s: ps: %s", name, exc)
            return None
        item = listed.get(f"yard-apply-{name}-job")
        return item.created if item is not None else None

    async def _job_loop(self, name: str, wake: asyncio.Event) -> None:
        # due = last run + every. The wake reschedules; it is not a trigger.
        log.info("job %s: watching", name)
        started = _now()
        # The time of the last attempt. After a 409 (still running) it moves, so
        # the loop waits a full interval instead of retrying at once.
        attempted: datetime | None = None
        try:
            while True:
                try:
                    slot = self.store.slot(name)
                except YardError:
                    return
                every = slot.every_seconds
                if every is None:
                    return
                since = await self._last_run(name) or started
                if attempted is not None:
                    since = max(since, attempted)
                left = every - (_now() - since).total_seconds()
                if left > 0:
                    # Sleep first: a Yard restart does not fire every job at once.
                    await _sleep(wake, min(left, every))
                    continue
                try:
                    await self.run_slot(name)
                except YardError as exc:
                    # Already running or no image yet: not errors.
                    log.info("job %s: %s", name, exc)
                except Exception as exc:
                    log.warning("job %s: %s", name, exc)
                attempted = _now()
        except asyncio.CancelledError:
            return

    async def run_slot(self, name: str) -> str:
        """Run a slot once: a job's run, or a service's start. Returns the output."""
        async with self._locks.setdefault(name, asyncio.Lock()):
            return await self.run_slot_locked(name)

    async def run_slot_locked(self, name: str) -> str:
        """run_slot for a caller that already holds the slot lock."""
        slot = self.store.slot(name)
        listed = podman.parse_ps(await self.podman.run("ps", "--format", "json"))
        if any(item.running and item.slot == name for item in listed.values()):
            raise YardError(
                409,
                "running",
                f"{name} is already running",
                help_text="One run at a time. The log is live.",
                example=f"yard logs --slot {name} --follow",
            )
        if not slot.live_image:
            raise YardError(
                409,
                "no_image",
                f"{name} has no build yet",
                example=f"yard deploy {name}",
            )
        try:
            if slot.job:
                return await deploy.run_job(self, slot)
            return await deploy.start_service(self, slot)
        except RuntimeError as exc:
            raise YardError(500, "run_failed", str(exc)) from exc

    async def git_repo(self, name: str, args: list[str]) -> JsonValue:
        self.store.repo(name)
        directory = self.config.repo(name)
        if not directory.exists():
            raise YardError(
                404,
                "not_found",
                f"repo {name} is not on this host",
                help_text="Create the repo, then run the git command.",
                example=f"yard repo new {name}",
            )
        call = git.prepare_git(args)
        try:
            text = await git.git(*call.argv, cwd=directory)
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


def _line(name: str, out: deploy.Outcome) -> str:
    line = f"slot {name}: {out.status}"
    if out.image:
        line += f" {out.image}"
    line += "\n"
    if out.log:
        line += out.log if out.log.endswith("\n") else out.log + "\n"
    return line


def _now() -> datetime:
    return datetime.now(UTC)


async def _sleep(wake: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(wake.wait(), max(seconds, 0))
    except TimeoutError:
        return
    wake.clear()
