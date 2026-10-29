"""Repo creation on a forge. `yard repo new` and `yard app new --create-repo` land here."""

from app.server import journal
from app.server.yard import Yard
from app.shared import YardError, check_app_name


async def create_repo(yard: Yard, forge_name: str, name: str, owner: str = "", *, actor: str) -> dict:
    """A private repo on the named forge. Returns {"repo", "grant_url"}."""
    check_app_name(name)
    forge = yard.forge_named(forge_name)
    if not forge.connected():
        raise YardError(
            502,
            forge.name,
            f"{forge.name} is not connected. Run yard forge setup {forge.name}.",
        )
    if not owner:
        if len(forge.installations) == 1:
            owner = str(forge.installations[0].get("account") or "")
        if not owner:
            accounts = ", ".join(str(entry.get("account") or "?") for entry in forge.installations)
            raise YardError(
                400,
                "owner_required",
                "name the owner account",
                help_text=f"The installations: {accounts or 'none'}.",
                example="yard repo new --owner ACCOUNT",
            )
    repo, grant_url = await forge.create_repo(owner, name)
    journal.send(yard.config, "repo.create", f"repo {repo}", forge=forge.name, repo=repo, actor=actor)
    return {"repo": repo, "grant_url": grant_url}
