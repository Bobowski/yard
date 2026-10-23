"""Every recipes/*.yml meets the app contract: a port note, service app on yard, pinned images."""

import re
from pathlib import Path

from app.server import apps

RECIPES = Path(__file__).resolve().parents[1] / "recipes"


def _check(recipe: Path) -> list[str]:
    text = recipe.read_text()
    problems = []
    if not re.search(r"^#\s*port:\s*\d{1,5}\s*$", text, re.M):
        problems.append("needs a `# port: N` line")
    if "services:" not in text or not re.search(r"^  app:", text, re.M):
        problems.append("needs a services: block with an app service")
    if not re.search(r"networks:\s*\n\s*yard:\s*\n\s*external:\s*true", text):
        problems.append("network yard must be declared external")
    if "${APP_ENV}" not in text or "${DATA_DIR}" not in text:
        problems.append("needs ${APP_ENV} and ${DATA_DIR}")
    for match in re.finditer(r"image:\s*(\S+)", text):
        if not re.search(r"@sha256:[0-9a-f]{64}$", match.group(1)):
            problems.append(f"image {match.group(1)} is not pinned @sha256:<64 hex>")
    return problems


def test_every_recipe_meets_the_contract():
    found = sorted(RECIPES.glob("*.yml"))
    assert found, "no recipes"
    for recipe in found:
        problems = _check(recipe)
        assert not problems, f"{recipe.name}: {'; '.join(problems)}"
        port = apps.recipe_port(recipe.read_bytes())
        assert port, f"{recipe.name}: recipe_port found no port"
