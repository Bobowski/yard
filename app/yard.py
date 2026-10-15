"""Yard operations. HTTP handlers call this. Podman and git stay behind it."""

import asyncio
import json
import logging
import re
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

from app import caddy, compose, deploy, git, podman
from app.config import Config
from app.errors import YardError
from app.podman import Podman
from app.store import Slot, Store
from app.values import check_domain, check_name, check_origin, format_env, parse_env, public_origin

if TYPE_CHECKING:
    from stario import App
    from stario.json import JsonValue

log = logging.getLogger("yard")
_EXEC_ARG = re.compile(r"^[A-Za-z0-9_.=:/*+,@%-]+$")


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
        (self.config.root / "projects").mkdir(parents=True, exist_ok=True)
        (self.config.root / "repos").mkdir(parents=True, exist_ok=True)
        (self.config.root / "tokens").mkdir(mode=0o700, parents=True, exist_ok=True)
        caddy_root = self.config.project_dir("yard") / "caddy"
        (caddy_root / "config").mkdir(parents=True, exist_ok=True)
        (caddy_root / "data").mkdir(parents=True, exist_ok=True)
        try:
            self.ensure_host_slot()
        except YardError as exc:
            log.warning("host project: %s", exc)
        await self._prepare_repos()

        await self.ensure_network()
        # The apply image build pulls a base image. The server answers while that build runs.
        self._apply_task = app.create_task(self._build_apply_image(), name="apply-image")
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
        """The yard project. Its compose runs the app and Caddy."""
        if not self.config.repo("yard").exists():
            return
        compose.ensure_env(self.config.env("yard"))
        caddy_root = self.config.project_dir("yard") / "caddy"
        (caddy_root / "config").mkdir(parents=True, exist_ok=True)
        (caddy_root / "data").mkdir(parents=True, exist_ok=True)
        try:
            self.store.slot("yard")
        except YardError as exc:
            if exc.code != "not_found":
                raise
            self.store.create_slot("yard", "yard", "main")

    async def create_repo(self, name: str, force: bool, origin: str = "") -> JsonValue:
        name = name.strip()
        check_name(name)
        if origin:
            origin = check_origin(origin)
        async with self._repo_lock:
            directory = self.config.repo(name)
            if directory.exists():
                # --force replaces a stray directory. A git repo stays.
                if (directory / "HEAD").is_file() or not force:
                    raise YardError(
                        409,
                        "exists",
                        "already exists",
                        context={"name": name},
                        help_text="Pick another name, or pass --force to replace a directory that is not a repo.",
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
            if origin and name == "yard":
                # A push to main deploys this slot. Fetch moves origin/main, then main follows.
                note = await self._follow_origin()
                if note:
                    log.info("yard repo: %s", note.strip())
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
        trigger: str = "",
        env: dict[str, str] | None = None,
    ) -> Slot:
        slot = self.store.create_slot(name, repo, ref, directory, trigger)
        self.write_env(slot.name, env or {})
        await self.sync_job(slot.name)
        return slot

    async def update_slot(
        self,
        name: str,
        *,
        ref: str | None = None,
        directory: str | None = None,
        trigger: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Slot:
        before = self.store.slot(name)
        slot = self.store.update_slot(name, ref=ref, dir=directory, trigger=trigger)
        if env is not None:
            self.write_env(name, env)
        if slot.job and not before.job:
            await self._down(name)
        await self.sync_job(slot.name)
        return slot

    async def delete_slot(self, name: str) -> None:
        slot = self.store.slot(name)
        project = compose.saved_project(self.config, slot)
        if project is not None:
            try:
                await compose.apply(self.config, name, "purge", project)
            except Exception as exc:
                log.warning("purge %s: %s", name, exc)

        self.store.delete_slot(name)
        await git.remove_worktree(self.config, name, slot.repo)
        directory = self.config.project_dir(name)
        if directory.exists():
            shutil.rmtree(directory)

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
                    f"project {slot.name} is saved. Yard could not read {slot.repo} {slot.ref}: {exc}",
                    context={"project": slot.name, "repo": slot.repo, "ref": slot.ref},
                    help_text=f"Fix the repo, then push to {slot.ref}.",
                ) from exc
            if not sha:
                return deploy.Outcome(
                    "waiting",
                    log=f"branch {slot.ref} has no revision yet. The next push to {slot.ref} deploys this project.\n",
                )
        image = compose.image_tag(slot.repo, slot.directory, sha)
        if self.skip_deploy:
            return deploy.Outcome("skipped", image, f"revision {sha} is on {slot.ref}. Deploy did not run.\n")
        outcome = await self.run_deploy(slot.name, sha)
        if outcome.status == "failed":
            raise YardError(
                500,
                "deploy_failed",
                f"project {slot.name} is saved. Deploy of {slot.ref} ({sha}) failed:\n{outcome.log.strip()}",
                context={"project": slot.name, "ref": slot.ref, "sha": sha},
                help_text=f"Push to {slot.ref} to deploy again.",
            )
        return outcome

    async def run_deploy(self, name: str, sha: str) -> deploy.Outcome:
        async with self._locks.setdefault(name, asyncio.Lock()):
            try:
                slot = self.store.slot(name)
            except YardError as exc:
                return deploy.Outcome("failed", error=str(exc))
            image = compose.image_tag(slot.repo, slot.directory, sha)
            if await _image_in_use(self, name, image):
                return deploy.Outcome("current", image)
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
                text = f"no project watches {repo} {watched_ref}\n"
            return 200, text

        failed = False
        for slot in watched:
            if self.skip_deploy:
                text += f"project {slot.name}: deploy skipped\n"
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
            return f"project {slot.name}: {exc}\n"
        return f"project {slot.name} removed: {why}\n"

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
        item = listed.get(f"up-{name}-job")
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
                example=f"yard podman logs up-{name}",
            )
        if compose.saved_project(self.config, slot) is None:
            raise YardError(
                409,
                "no_image",
                f"{name} has no build yet",
                example=f"yard up {name}",
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
        _check_git_args(args)
        try:
            text = await git.git(*args, cwd=directory)
        except RuntimeError as exc:
            raise YardError(
                400,
                "git_failed",
                str(exc) or "git failed",
                help_text="Git printed that text.",
                example=f"yard git {name} log -1",
            ) from exc
        if name == "yard" and args[:1] == ["fetch"]:
            note = await self._follow_origin()
            if note:
                text = f"{text}\n{note}" if text else note
        deleted = _deleted_branch(args)
        if deleted:
            note = await self._drop_deleted(name, deleted)
            removed = "".join(f"{line}\n" for line in note.splitlines() if line.startswith("project "))
            if removed:
                text = f"{text}\n{removed}" if text else removed
        if text and not text.endswith("\n"):
            text += "\n"
        return {"output": text}

    async def caddy_routes(self) -> list[dict[str, str]]:
        return caddy.routes_from_autosave(self.config.autosave())

    async def caddy_config(self) -> JsonValue:
        _code, raw = await self._caddy("GET", caddy.CONFIG_PATH)
        try:
            found = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise YardError(502, "caddy", "caddy config was not json") from exc
        return found if isinstance(found, dict) else {}

    async def caddy_put(self, host: str, dial: str, window: str) -> dict[str, str]:
        host = _public_host(host)
        target = caddy.normalize_dial(dial)
        span = caddy.check_window(window)
        body = json.dumps(caddy.proxy_route(host, target, span)).encode()
        found = caddy.find_route(caddy.autosave_route_json(self.config.autosave()), host)
        if found is None:
            await self._caddy("POST", caddy.ROUTES_PATH, body)
        else:
            # A stored id is one object. An older route has no id, so the write uses its index.
            index, ident = found
            path = f"/id/{quote(ident, safe='')}" if ident else f"{caddy.ROUTES_PATH}/{index}"
            await self._caddy("PUT", path, body)
        return {"host": host, "dial": target, "id": caddy.route_id(host)}

    async def caddy_delete(self, host: str) -> dict[str, str]:
        host = _public_host(host)
        found = caddy.find_route(caddy.autosave_route_json(self.config.autosave()), host)
        if found is None:
            raise YardError(
                404,
                "not_found",
                "route does not exist",
                context={"host": host},
                help_text="Run yard bind ls to see the binds.",
                example=f"yard bind rm {host}",
            )
        index, ident = found
        path = f"/id/{quote(ident, safe='')}" if ident else f"{caddy.ROUTES_PATH}/{index}"
        await self._caddy("DELETE", path)
        return {"host": host}

    async def caddy_cli(self, args: list[str]) -> dict[str, str]:
        if not args:
            raise YardError(
                400,
                "bad_args",
                "the command needs words",
                help_text="Pass the caddy command.",
                example="yard caddy version",
            )
        text = await self._service_exec("caddy", ["caddy", *args])
        return {"output": text if text.endswith("\n") or not text else text + "\n"}

    async def route_list(self) -> list[dict[str, str]]:
        rows = await self.caddy_routes()
        try:
            listed = podman.parse_ps(await self.podman.run("ps", "-a", "--format", "json"))
        except YardError:
            listed = {}
        for row in rows:
            name = row["dial"].split(":", 1)[0] if row["dial"] else ""
            row["state"] = _route_state(name, listed)
        return rows

    async def route_put(self, host: str, dial: str, window: str) -> dict[str, str]:
        """One public route for this container. A second add replaces that record."""
        host = _public_host(host)
        target = caddy.normalize_dial(dial)
        span = caddy.check_window(window)
        name = target.split(":", 1)[0]
        for row in await self.caddy_routes():
            if row["dial"].split(":", 1)[0] == name and row["host"] != host:
                await self.caddy_delete(row["host"])
        saved = await self.caddy_put(host, target, span)
        listed = podman.parse_ps(await self.podman.run("ps", "-a", "--format", "json"))
        saved["state"] = _route_state(name, listed)
        return saved

    async def route_delete(self, dial: str) -> dict[str, str]:
        target = caddy.normalize_dial(dial)
        name = target.split(":", 1)[0]
        rows = [row for row in await self.caddy_routes() if row["dial"].split(":", 1)[0] == name]
        if not rows:
            raise YardError(
                404,
                "not_found",
                "route does not exist",
                context={"dial": name},
                help_text="Run yard bind ls to see the binds.",
                example=f"yard bind rm {name}",
            )
        for row in rows:
            await self.caddy_delete(row["host"])
        return {"host": rows[0]["host"], "dial": rows[0]["dial"]}

    async def podman_command(self, args: list[str]) -> dict[str, str]:
        if not args or len(args) > 64 or any(_bad_word(arg) for arg in args):
            raise YardError(
                400,
                "bad_args",
                "the command needs words",
                help_text="Pass the podman command.",
                example="yard podman ps",
            )
        if "-f" in args or "--follow" in args:
            raise YardError(
                400,
                "bad_args",
                "follow stays open",
                help_text="The logs command streams. Leave the other commands as one reply.",
                example="yard podman logs -f heldso-app-1",
            )
        text = await self.podman.run(*args, timeout=None)
        return {"output": text if text.endswith("\n") or not text else text + "\n"}

    async def _caddy(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
        """One admin call. curl runs inside the caddy container."""
        try:
            listed = podman.parse_ps(await self.podman.run("ps", "--format", "json"))
        except YardError as exc:
            raise YardError(
                400,
                "no_caddy",
                "caddy is not running",
                help_text="Start the yard project. Caddy is one of its containers.",
                example="yard up yard",
            ) from exc
        name = next((item.name for item in listed.values() if item.running and _is_service(item, "caddy")), "")
        if not name:
            raise YardError(
                400,
                "no_caddy",
                "caddy is not running",
                help_text="Start the yard project. Caddy is one of its containers.",
                example="yard up yard",
            )
        try:
            text = await self.podman.run(
                "exec",
                "-i",
                name,
                *caddy.curl_words(method, path, bool(body)),
                stdin=body or None,
                timeout=20,
            )
        except YardError as exc:
            raise YardError(502, "caddy", str(exc), help_text="curl inside the caddy container failed.") from exc
        code, raw = caddy.split_curl(text)
        if code >= 300:
            raise YardError(502, "caddy", f"caddy {method} {path}: {code}")
        return code, raw

    async def _service_exec(self, service: str, argv: list[str]) -> str:
        if not argv or len(argv) > 32 or any(not _EXEC_ARG.fullmatch(arg) for arg in argv):
            raise YardError(
                400,
                "bad_args",
                "bad command",
                help_text="Pass the command words. Do not put a space inside one word.",
                example=f"yard {service} version",
            )
        listed = podman.parse_ps(await self.podman.run("ps", "--format", "json"))
        name = next((item.name for item in listed.values() if item.running and _is_service(item, service)), "")
        if not name:
            raise YardError(
                404,
                "not_found",
                f"{service} is not running",
                help_text=f"Start the {service} container on the yard network.",
                example=f"yard {service} ls",
            )
        return await self.podman.run("exec", name, *argv, timeout=30)

    async def _follow_origin(self) -> str:
        """Point main at origin/main when that move is a fast-forward. A diverged main stays."""
        repo = self.config.repo("yard")
        try:
            origin = (
                await git.git("rev-parse", "--verify", "--end-of-options", "refs/remotes/origin/main", cwd=repo)
            ).strip()
        except RuntimeError:
            return ""
        try:
            local = (await git.git("rev-parse", "--verify", "--end-of-options", "refs/heads/main", cwd=repo)).strip()
        except RuntimeError:
            local = ""
        if local == origin:
            return ""
        if local:
            try:
                await git.git("merge-base", "--is-ancestor", local, origin, cwd=repo)
            except RuntimeError:
                return "main and origin/main have diverged\n"
        await git.git("update-ref", "refs/heads/main", origin, cwd=repo)
        return f"main {origin}\n"


_GIT_BLOCK = {"-C", "--git-dir", "--work-tree", "--exec-path", "--namespace"}


def _bad_word(arg: str) -> bool:
    return not arg or "\n" in arg or "\x00" in arg or len(arg) > 4096


def _check_git_args(args: list[str]) -> None:
    if not args or len(args) > 64 or any(_bad_word(arg) for arg in args):
        raise YardError(
            400,
            "bad_args",
            "the command needs words",
            help_text="Pass the git command.",
            example="yard git heldso log -1",
        )
    for arg in args:
        if arg.split("=", 1)[0] in _GIT_BLOCK:
            raise YardError(
                400,
                "bad_args",
                "that flag leaves the repo",
                help_text="The command runs inside the bare repo.",
                example="yard git heldso log -1",
            )


def _deleted_branch(args: list[str]) -> str:
    if not args or args[0] != "branch" or "-D" not in args and "-d" not in args:
        return ""
    words = [arg for arg in args[1:] if not arg.startswith("-")]
    return words[-1] if words else ""


def _route_state(name: str, listed: dict[str, podman.Container]) -> str:
    item = listed.get(name) or listed.get(f"{name}-1")
    if item is None:
        return "absent"
    return "up" if item.running else "down"


def _public_host(host: str) -> str:
    host = host.strip()
    check_domain(host)
    if host:
        return host
    raise YardError(
        400,
        "bad_domain",
        "domain must be a hostname",
        help_text="Use a lowercase hostname.",
        example="heldso.example.com",
    )


def _is_service(item: podman.Container, service: str) -> bool:
    return item.service == service or item.name == service or item.name == f"yard-{service}-1"


async def _image_in_use(yard: Yard, name: str, image: str) -> bool:
    """True when a container for this slot already runs that image."""
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    except Exception:
        return False
    return any(item.slot == name and item.image == image for item in listed.values())


def _line(name: str, out: deploy.Outcome) -> str:
    line = f"project {name}: {out.status}"
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
