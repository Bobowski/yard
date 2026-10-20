"""A Linux runner has a real journald; every test config must point the journal at nowhere."""

from pathlib import Path

from app.server.store import Config
from tests.server.conftest import make_yard


def _points_at_nowhere(config: Config, tmp_path: Path) -> None:
    assert config.journal_dir != Path("/var/log/journal")
    assert tmp_path in config.journal_dir.parents
    assert config.journal_socket != Path("/run/systemd/journal/socket")
    assert tmp_path in config.journal_socket.parents
    assert config.journalctl_bin != "journalctl"


async def test_the_http_fixture_isolates_the_journal(yard, tmp_path):
    _client, _token, _root = yard
    _points_at_nowhere(Config.from_env(), tmp_path)


def test_make_yard_isolates_the_journal(tmp_path):
    _points_at_nowhere(make_yard(tmp_path).config, tmp_path)
