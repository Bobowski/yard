"""The example compose file classifies as compose with no build: line."""

from pathlib import Path

from app.server import detect

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "uptime-kuma.compose.yml"


def test_the_example_is_a_compose_file():
    text = EXAMPLE.read_text()
    assert detect.classify(text) == "compose"
    import re

    assert not re.search(r"^\s+build:", text, re.M)
    assert 'expose:\n      - "3001"' in text or '"3001"' in text
