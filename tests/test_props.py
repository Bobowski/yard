"""Properties for the Yard rules. The example tests keep the cases that failed."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.cli import judge_swap, parse_slot_sets, slot_patch, split_args
from app.errors import YardError
from app.machine import JOB_RETRY, RunState, image_tag, job_due, plan_job, routes_from, slug, yard_route
from app.podman import format_stamp, page_log_rows, page_logs
from app.store import Slot, check_domain, check_name, check_ref, check_slot_name, clean_dir, parse_every
from app.text import (
    Block,
    ExpLabel,
    apply_slot_file,
    format_blocks,
    format_values,
    parse_blocks,
    parse_env,
    process_defaults,
    slot_values,
)
from app.yard import git_repo
from tests.strategies import (
    bad_every,
    bad_repos,
    body_lines,
    env_keys,
    env_values,
    exp_labels,
    hostnames,
    rel_dirs,
    repo_names,
    slot_names,
    titles,
    words,
)

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


@st.composite
def _runs(draw):
    found = []
    for _ in range(draw(st.integers(0, 4))):
        age = draw(st.integers(0, 20))
        started = NOW - timedelta(minutes=age)
        running = draw(st.booleans())
        finished = None if running else started + timedelta(minutes=draw(st.integers(0, max(age, 0))))
        found.append(RunState(draw(st.from_regex(r"[a-z]{1,4}", fullmatch=True)), running, started, finished))
    return found


@st.composite
def _blocks(draw):
    count = draw(st.integers(0, 3))
    ids = draw(st.lists(st.integers(1, 9000), min_size=count, max_size=count, unique=True))
    found = []
    for exp_id in ids:
        has_id = draw(st.booleans())
        labels = [ExpLabel(key, value) for key, value in draw(st.lists(exp_labels(), max_size=3))]
        body = "\n".join(draw(st.lists(body_lines(), max_size=3)))
        found.append(Block(draw(titles()), body, labels, exp_id if has_id else 0, has_id))
    return found


def _set_args(pairs: list[tuple[str, str]]) -> list[str]:
    args: list[str] = []
    for key, value in pairs:
        if value == "":
            args.extend(["--set", f"{key}="])
        else:
            args.extend(["--set", key, value])
    return args


@example("heldso")
@example("a")
@example("a-")
@given(repo_names())
def test_repo_names_pass(name: str) -> None:
    check_name(name)


@example("heldso")
@given(slot_names())
def test_slot_names_pass(name: str) -> None:
    check_slot_name(name)


@example("")
@example("yard")
@example("yard-sampler")
@example("Heldso")
@given(bad_repos())
def test_repo_names_fail(name: str) -> None:
    with pytest.raises(YardError) as caught:
        check_name(name)
    assert caught.value.code == "bad_name"


def test_caddy_is_a_repo_name_and_not_a_slot_name() -> None:
    check_name("caddy")
    with pytest.raises(YardError) as caught:
        check_slot_name("caddy")
    assert caught.value.code == "bad_name"


_REF = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,16}", fullmatch=True).filter(
    lambda ref: ".." not in ref and not ref.startswith("/")
)


@example("main")
@example("feature/login")
@given(_REF)
def test_refs_pass(ref: str) -> None:
    check_ref(ref)


@example("")
@example("../x")
@example("/main")
@example("a b")
@example("a\tb")
@example("a\\b")
@given(
    st.one_of(
        st.just(""),
        st.just("../x"),
        st.just("/main"),
        st.just("a b"),
        st.from_regex(r"/[a-z]{1,8}", fullmatch=True),
        st.from_regex(r"[a-z]{0,4}\.\.[a-z]{0,4}", fullmatch=True),
        st.from_regex(r"[a-z]{0,4} [a-z]{0,4}", fullmatch=True),
    )
)
def test_refs_fail(ref: str) -> None:
    with pytest.raises(YardError) as caught:
        check_ref(ref)
    assert caught.value.code == "bad_ref"


@example("")
@example("heldso.example.com")
@given(st.one_of(st.just(""), hostnames()))
def test_domains_pass(domain: str) -> None:
    check_domain(domain)


@example("..")
@example("heldso..com")
@example("-bad")
@example("Bad.com")
@given(
    st.one_of(
        st.just(".."),
        st.just("heldso..com"),
        st.from_regex(r"[A-Z][A-Za-z]{1,6}", fullmatch=True),
        st.from_regex(r"-[a-z]{1,6}", fullmatch=True),
        hostnames().map(lambda host: host + ".."),
    )
)
def test_domains_fail(domain: str) -> None:
    with pytest.raises(YardError) as caught:
        check_domain(domain)
    assert caught.value.code == "bad_domain"


@example("")
@example(".")
@example("./")
@given(st.sampled_from(["", ".", "./", " . "]))
def test_blank_directories_clear(path: str) -> None:
    assert clean_dir(path) == ""


@example("projects/mono")
@given(rel_dirs())
def test_directories_stay_inside_the_repo(path: str) -> None:
    got = clean_dir(path)
    assert clean_dir(got) == got
    assert not got.startswith("/")
    assert ".." not in got.split("/")


_ESCAPE = st.one_of(
    st.just(".."),
    st.just("/etc"),
    st.from_regex(r"\.\./[a-z]{1,6}", fullmatch=True),
    st.from_regex(r"/[a-z]{1,6}", fullmatch=True),
)


@example("..")
@example("../x")
@example("/etc")
@example("a/../../b")
@given(_ESCAPE)
def test_directories_reject_an_escape(path: str) -> None:
    with pytest.raises(YardError) as caught:
        clean_dir(path)
    assert caught.value.code == "bad_dir"


@example(1)
@example(60)
@example(61)
@given(st.integers(1, 5000))
def test_every_seconds(n: int) -> None:
    if n % 3600 == 0:
        expect = f"{n // 3600}h"
    elif n % 60 == 0:
        expect = f"{n // 60}m"
    else:
        expect = f"{n}s"
    assert parse_every(f"{n}s") == expect
    assert parse_every(expect) == expect


@example(5)
@example(60)
@given(st.integers(1, 240))
def test_every_minutes(n: int) -> None:
    expect = f"{n // 60}h" if n % 60 == 0 else f"{n}m"
    assert parse_every(f"{n}m") == expect
    assert parse_every(f"every {n}m") == expect


@example(1)
@given(st.integers(1, 48))
def test_every_hours(n: int) -> None:
    assert parse_every(f"{n}h") == f"{n}h"
    assert parse_every(f"every {n}h") == f"{n}h"


@example("")
@example("off")
@example("-")
@example("OFF")
@example("every off")
@given(st.sampled_from(["", "off", "-", "OFF", "Off", "  -  ", "every off"]))
def test_every_clears(text: str) -> None:
    assert parse_every(text) == ""


@example("tomorrow")
@example("0s")
@example("500ms")
@given(bad_every())
def test_every_rejects(text: str) -> None:
    with pytest.raises(YardError) as caught:
        parse_every(text)
    assert caught.value.code == "bad_every"


@example({"API_URL": "https://x", "SECRET_KEY": "abc"})
@given(st.dictionaries(env_keys(), env_values(), max_size=5))
def test_env_lines_round_trip(env: dict[str, str]) -> None:
    text = "".join(f"{key}={env[key]}\n" for key in sorted(env))
    assert parse_env(text) == env


@example("ab c")
@given(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 ]{0,12}", fullmatch=True))
def test_quoted_env_values(value: str) -> None:
    assert parse_env(f'SECRET="{value}"\n') == {"SECRET": value}


@example({"API_TOKEN": "secret", "PORT": "9000"})
@given(st.dictionaries(env_keys(), env_values(), max_size=4))
def test_slot_file_round_trip(env: dict[str, str]) -> None:
    slot = Slot(
        "heldso",
        "heldso",
        "main",
        "projects/mono",
        "heldso.example.com",
        "localhost/heldso:abc",
        "5m",
        env,
        "",
    )
    edit = apply_slot_file(slot, format_values(slot_values(slot)))
    assert edit.ref is None
    assert edit.domain is None
    assert edit.directory is None
    assert edit.every is None
    assert edit.warn == []
    defaults = process_defaults(slot.name)
    assert edit.env == {key: value for key, value in env.items() if defaults.get(key) != value}


@example("other")
@given(repo_names().filter(lambda name: name != "heldso"))
def test_slot_repo_key_stays(name: str) -> None:
    slot = Slot("heldso", "heldso", "main", "", "", "", "", {}, "")
    text = format_values(slot_values(slot)).replace("SLOT_REPO=heldso", f"SLOT_REPO={name}", 1)
    edit = apply_slot_file(slot, text)
    assert edit.warn == ["SLOT_REPO stays heldso"]
    assert edit.ref is None


@example({"SLOT_DOMAIN": "heldso.com", "API_TOKEN": "secret"}, {"API_TOKEN": "secret", "PORT": "9000"})
@given(
    st.dictionaries(env_keys() | st.sampled_from(["SLOT_DOMAIN", "SLOT_BRANCH", "PORT"]), env_values(), max_size=5),
    st.dictionaries(env_keys() | st.sampled_from(["SLOT_DOMAIN", "SLOT_BRANCH", "PORT"]), env_values(), max_size=5),
)
def test_slot_patch_writes_only_the_changes(before: dict[str, str], after: dict[str, str]) -> None:
    patch = slot_patch(before, after)
    for key, value in after.items():
        if before.get(key) != value:
            assert patch[key] == value
        else:
            assert key not in patch
    for key in before:
        if key not in after:
            assert patch[key] == ""


@example([("SLOT_DOMAIN", "heldso.com"), ("API_TOKEN", "secret"), ("OLD", "")])
@given(st.lists(st.tuples(env_keys(), env_values()), min_size=1, max_size=4))
def test_slot_sets_round_trip(pairs: list[tuple[str, str]]) -> None:
    assert parse_slot_sets(_set_args(pairs)) == pairs


@example(["--domain", "heldso.com"])
@example([])
@example(["--set"])
@given(st.lists(words(), min_size=1, max_size=3))
def test_slot_sets_need_the_set_flag(args: list[str]) -> None:
    with pytest.raises(ValueError):
        parse_slot_sets(args)


@example(["slot", "create", "heldso", "--repo", "heldso", "--domain", "heldso.example.com"])
@given(st.lists(words(), max_size=6))
def test_json_flag_stays_out_of_the_command(words_in: list[str]) -> None:
    opt = split_args([*words_in[:2], "--json", *words_in[2:]])
    assert opt.json is True
    assert opt.rest == words_in


@given(st.from_regex(r"http://[a-z]{1,8}", fullmatch=True), st.lists(words(), max_size=3))
def test_url_flag_consumes_the_next_word(url: str, words_in: list[str]) -> None:
    opt = split_args(["--url", url, *words_in])
    assert opt.url == url
    assert opt.rest == words_in


@example("ready", True)
@example("ready", False)
@example("rolled_back", True)
@example("failed", False)
@example("swapping", False)
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", max_size=12), st.booleans())
def test_judge_swap(status: str, health: bool) -> None:
    done, ok, message = judge_swap(status, health)
    if status == "ready" and health:
        assert (done, ok) == (True, True)
    elif status == "ready":
        assert done is False
    elif status in {"rolled_back", "failed"}:
        assert done is True and ok is False and message
    else:
        assert done is False and ok is False


@example("heldso")
@given(repo_names())
def test_git_repo_reads_the_first_segment(name: str) -> None:
    assert git_repo(f"/git/{name}.git/info/refs") == name


@example("../x.git")
@given(st.text(max_size=24))
def test_git_repo_drops_a_parent_segment(text: str) -> None:
    got = git_repo("/git/" + text)
    assert ".." not in got
    assert "/" not in got


@example("projects/Mono_App")
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ/_.", max_size=24))
def test_slug_is_a_tag_piece(text: str) -> None:
    got = slug(text)
    assert got == got.lower()
    assert "/" not in got and "_" not in got and "." not in got
    assert slug(got) == got


@example("mono", "", "abc")
@example("mono", "projects/Mono_App", "abc")
@given(repo_names(), st.one_of(st.just(""), rel_dirs()), st.from_regex(r"[0-9a-f]{4}", fullmatch=True))
def test_image_tag(repo: str, directory: str, sha: str) -> None:
    if directory == "":
        assert image_tag(repo, directory, sha) == f"localhost/{repo}:{sha}"
    else:
        assert image_tag(repo, directory, sha) == f"localhost/{repo}-{slug(directory)}:{sha}"


@example([("heldso", "heldso.example.com", False), ("plain", "", False), ("nightly", "night.example.com", True)])
@given(st.lists(st.tuples(slot_names(), st.one_of(st.just(""), hostnames()), st.booleans()), max_size=5))
def test_caddy_routes_skip_jobs_and_blank_domains(rows: list[tuple[str, str, bool]]) -> None:
    slots = [Slot(name, "repo", "main", "", domain, "", "5m" if job else "", {}, "") for name, domain, job in rows]
    routes = routes_from(slots)
    expect = [(domain, f"{name}:8000") for name, domain, job in rows if domain and not job]
    assert [(route.host, route.dial) for route in routes] == expect


@example("")
@example("yard.example.com")
@given(st.one_of(st.just(""), hostnames()))
def test_yard_route(domain: str) -> None:
    route = yard_route(domain)
    if domain == "":
        assert route is None
        return
    assert route is not None
    assert route.host == domain
    assert route.dial == "yard:8000"


@given(st.lists(st.integers(0, 20_000), min_size=0, max_size=8, unique=True).map(sorted))
def test_log_pages_cover_every_line(seconds: list[int]) -> None:
    base = datetime(2026, 10, 5, tzinfo=UTC)
    lines = [f"{format_stamp(base + timedelta(seconds=second))} line{index}" for index, second in enumerate(seconds)]
    raw = "\n".join(lines) + ("\n" if lines else "")
    got: list[str] = []
    cursor = ""
    more = False
    for _ in range(len(lines) + 2):
        text, cursor, more = page_logs(raw, cursor, 1)
        if text:
            got.append(text)
        if not more:
            break
    assert got == lines
    assert more is False


@given(
    st.lists(st.integers(0, 400), min_size=1, max_size=6, unique=True),
    st.integers(0, 400),
    st.integers(1, 80),
)
def test_log_window_is_half_open(seconds: list[int], start: int, span: int) -> None:
    base = datetime(2026, 10, 5, tzinfo=UTC)
    stop = start + span
    lines = []
    expect = []
    for second in sorted(seconds):
        stamp = format_stamp(base + timedelta(seconds=second))
        line = f"{stamp} m"
        lines.append(line)
        if start <= second < stop:
            expect.append(line)
    rows, _cursor, more = page_log_rows(
        "\n".join(lines) + "\n",
        "",
        format_stamp(base + timedelta(seconds=start)),
        format_stamp(base + timedelta(seconds=stop)),
        20,
    )
    assert more is False
    assert [row.raw for row in rows] == expect


@given(st.integers(0, 3600), st.one_of(st.just(""), st.just("localhost/heldso:abc")), _runs())
def test_job_plan(every: int, image: str, runs: list[RunState]) -> None:
    plan = plan_job(float(every), image, runs, NOW)
    running = [run for run in runs if run.running]
    assert plan.sleep >= 0
    if plan.start:
        assert image != "" and every > 0 and not running
        assert job_due(runs, float(every), NOW)
    if plan.wait_id:
        assert any(run.running and run.id == plan.wait_id for run in runs)
        assert plan.start is False
    if image != "" and every > 0 and not running and job_due(runs, float(every), NOW):
        assert plan.start is True
    if image == "" and every > 0:
        assert plan.start is False
        assert plan.warn == "no image yet"
        assert plan.sleep == JOB_RETRY


@given(_blocks())
def test_experiment_document_round_trip(blocks: list[Block]) -> None:
    assert parse_blocks(format_blocks(blocks)) == blocks


@example("no title\n")
@example("#\n\nbody\n")
@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz \n", min_size=1, max_size=40).filter(
        lambda text: bool(text.strip()) and not text.lstrip(" \n").startswith("#")
    )
)
def test_experiment_document_needs_a_title(text: str) -> None:
    with pytest.raises(YardError) as caught:
        parse_blocks(text)
    assert caught.value.code == "bad_title"
