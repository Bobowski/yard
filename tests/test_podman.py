from datetime import UTC, datetime

from app.podman import format_stamp, ok_name, page_logs, parse_images, parse_info, parse_ps, parse_stats


def _unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_parse_stats_and_ps():
    samples = parse_stats('[{"Name":"heldso","CPUPerc":"1.5%","MemUsage":"20MB / 1GB"}]')
    assert samples["heldso"].cpu == "1.5%"
    assert samples["heldso"].mem == "20MB / 1GB"
    assert samples["heldso"].cpu_percent == 1.5
    assert samples["heldso"].mem_bytes == 20_000_000
    raw = """[
        {"Names":["heldso"],"State":"running","Status":"Up 2 minutes (healthy)",
         "StartedAt":1710000000,"ExitCode":0,"Restarts":3},
        {"Names":["shop"],"State":"exited","Status":"Exited (1) 4 minutes ago","ExitedAt":1710000200,"ExitCode":1}
    ]"""
    got = parse_ps(raw)
    assert got["heldso"].running
    assert got["heldso"].restarts == 3
    assert got["shop"].restarts == 0
    assert got["heldso"].health == "healthy"
    assert got["heldso"].started == _unix(1710000000)
    assert not got["shop"].running
    assert got["shop"].exit_code == 1
    assert got["shop"].health == ""
    assert got["shop"].exited == _unix(1710000200)


def test_parse_ps_reads_name_and_labels():
    raw = """[
        {"Id":"app","Names":["heldso-app-1"],
         "Labels":{"com.docker.compose.project":"heldso","com.docker.compose.service":"app"}},
        {"Id":"run","Names":["yard-apply-backup-job"]},
        {"Id":"build","Names":["yard-apply-backup"]},
        {"Id":"bare","Names":["caddy"]}
    ]"""
    got = parse_ps(raw)

    # An app container: compose's project is the slot, its service is the service.
    assert got["heldso-app-1"].slot == "heldso"
    assert got["heldso-app-1"].service == "app"
    assert got["heldso-app-1"].role == "app"

    # The apply containers are known by name: -job runs a job, the rest deploy.
    assert got["yard-apply-backup-job"].slot == "backup"
    assert got["yard-apply-backup-job"].service == ""
    assert got["yard-apply-backup-job"].role == "job"
    assert got["yard-apply-backup"].slot == "backup"
    assert got["yard-apply-backup"].role == "deploy"

    # A container with no compose labels has no slot.
    assert got["caddy"].slot == ""
    assert got["caddy"].service == ""
    assert got["caddy"].role == "app"
    assert ok_name("heldso-app")
    assert not ok_name("../caddy")
    assert not ok_name("bad name")


def test_parse_info():
    raw = (
        '{"host":{"cpus":12,"memTotal":67320864768},'
        '"store":{"graphRootAllocated":467909804032},"version":{"Version":"5.3.1"}}'
    )
    host = parse_info(raw)
    assert host.cpus == 12
    assert host.version == "5.3.1"
    assert host.mem == 67320864768
    assert host.disk == 467909804032
    assert parse_info("nope").cpus == 0


def test_page_logs_advances_the_cursor():
    raw = "2026-10-05T12:00:00.1Z one\n2026-10-05T12:00:01.2Z two\n2026-10-05T12:00:02.3Z three\n"
    text, nxt, more = page_logs(raw, "2026-10-05T12:00:00.1Z", 1)
    assert text == "2026-10-05T12:00:01.2Z two"
    assert more
    assert nxt == "2026-10-05T12:00:01.2Z"
    text, nxt, more = page_logs(raw, nxt, 10)
    assert text == "2026-10-05T12:00:02.3Z three"
    assert not more
    text, _nxt, more = page_logs(raw, nxt, 10)
    assert text == ""
    assert not more


def test_parse_images():
    raw = (
        '[{"Names": ["localhost/app:abc", "localhost/app:latest"], "Created": 1710000000, "Size": 400},'
        ' {"Names": ["<none>"], "Created": "bad", "Size": "x"}]'
    )
    found = parse_images(raw)
    assert [image.name for image in found] == ["localhost/app:abc", "localhost/app:latest", "<none>"]
    assert found[0].created == _unix(1710000000)
    assert found[0].size == 400
    assert found[2].created is None
    assert found[2].size == 0


def test_format_stamp():
    assert format_stamp(datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)) == "2026-10-05T12:00:00Z"
    assert format_stamp(datetime(2026, 10, 5, 12, 0, 0, 120000, tzinfo=UTC)) == "2026-10-05T12:00:00.12Z"
