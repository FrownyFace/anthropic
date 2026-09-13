"""Scenario catalogue: the bundled JSON must load, and its private half must stay private."""

from __future__ import annotations

import json

import pytest

from faultline_common.schemas import Scenario
from sandbox_env import grader, provenance, scenarios

#: the scenarios this service must always ship. Extra scenarios may be added to the catalogue by
#: other workstreams, so every test below asserts "at least the core five, and every scenario
#: actually on disk is valid" instead of pinning an exact set — a catalogue test that breaks when a
#: scenario is *added* tests the directory listing, not the code.
CORE = {"missing-config", "locked-file", "lost-ack", "gauntlet", "worker-crash"}
ALL_IDS = sorted(b.id for b in scenarios.list_bundles())

#: public fields every scenario must expose; `Scenario` may grow more (they are public by design).
PUBLIC_REQUIRED = {
    "id", "title", "description", "task_prompt", "max_steps", "fault_kinds",
    "harness_faults", "checks", "faults_public",
}
#: private halves that must never appear in the public projection
PUBLIC_FORBIDDEN = {"fault_plan", "hidden_tests", "setup", "fixture"}


def test_all_bundled_scenarios_load() -> None:
    ids = set(ALL_IDS)
    assert CORE <= ids, f"missing bundled scenario(s): {sorted(CORE - ids)}"


@pytest.mark.parametrize("scenario_id", ALL_IDS)
def test_public_view_is_a_valid_scenario(scenario_id: str) -> None:
    bundle = scenarios.get_bundle(scenario_id)
    public = bundle.public
    assert isinstance(public, Scenario)
    assert public.task_prompt.strip()
    assert public.max_steps >= 10

    dumped = public.model_dump()
    assert PUBLIC_REQUIRED <= set(dumped)
    assert not (PUBLIC_FORBIDDEN & set(dumped))
    assert set(public.fault_kinds) == {f.kind for f in bundle.fault_plan.faults}


@pytest.mark.parametrize("scenario_id", ALL_IDS)
def test_the_structured_fault_projection_leaks_no_injected_mechanics(scenario_id: str) -> None:
    """`fault_kinds` + `faults_public` say WHAT can fail. They must never say where, when or how often.

    This is the machine-readable channel, so it is where the test has to be strict: a UI that
    rendered `faults_public` would otherwise put "ENOENT on README.md, 1 hit" on screen, and anyone
    reading the run would know which ENOENT was a lie. Kind, origin, layer and one sentence of prose
    are enough for the badge; everything else stays in the Dict.

    The prose fields are a different matter and are covered by the two tests below: `description`
    and the check descriptions are reviewer-facing copy that is *supposed* to explain the scenario,
    and `task_prompt` already has to name the files the task is about (`lost-ack` has said
    "CHANGELOG.md" since the first commit — that is the release the agent is asked to cut, not a
    tell). The one thing prose may never give away is a *transient* `missing_file` target, because
    that file is really on disk and naming it would make the fake ENOENT obvious.
    """
    bundle = scenarios.get_bundle(scenario_id)
    public = bundle.public

    projection = json.dumps(
        [f.model_dump() for f in public.faults_public] + [public.fault_kinds]
    )
    for fault in bundle.fault_plan.faults:
        assert fault.path not in projection, f"{scenario_id}: faults_public leaks the path {fault.path}"
        assert fault.mode not in projection, f"{scenario_id}: faults_public leaks mode {fault.mode}"
        if fault.hits is not None:
            assert str(fault.hits) not in projection, f"{scenario_id}: faults_public leaks hits"
        assert str(fault.delay_ms) not in projection, f"{scenario_id}: faults_public leaks delay_ms"

    for entry in public.faults_public:
        assert set(entry.model_dump()) == {"kind", "origin", "layer", "description"}
        assert entry.description, "every fault class needs one sentence for the UI"

    # keys that would carry the mechanism must not exist anywhere in the public view except inside
    # harness_faults (which is a harness-side chaos setting, see the test below)
    blob = json.loads(public.model_dump_json())
    blob.pop("harness_faults", None)
    flat = json.dumps(blob)
    for key in ('"hits"', '"delay_ms"', '"fault_plan"', '"hidden_tests"', '"mode"', '"nth"',
                '"after_ms"'):
        assert key not in flat, f"{scenario_id} leaks fault mechanics via {key}"


@pytest.mark.parametrize("scenario_id", ALL_IDS)
def test_the_task_prompt_never_gives_the_trick_away(scenario_id: str) -> None:
    """`task_prompt` is the ONLY public field that reaches the model (harness `build_task_message`).

    There is no prompt builder in this service to assert against, so the rule is enforced here and
    stated for the harness owner: everything else on `Scenario` — `description`, `checks`,
    `faults_public`, `harness_faults` — is for the browser and the evidence files, and must not be
    pasted into a model turn.
    """
    bundle = scenarios.get_bundle(scenario_id)
    prompt = bundle.public.task_prompt
    for fault in bundle.fault_plan.faults:
        if fault.mode == "transient" and fault.kind == "missing_file":
            assert fault.path not in prompt, f"{scenario_id} leaks the transient target {fault.path}"
    for word in ("EACCES", "ENOENT", "injected", "fault plan", "sticky", "transient", "hits"):
        assert word not in prompt, f"{scenario_id} task_prompt leaks mechanics via {word!r}"


@pytest.mark.parametrize("scenario_id", ALL_IDS)
def test_faults_public_matches_the_plan_and_the_harness_faults(scenario_id: str) -> None:
    bundle = scenarios.get_bundle(scenario_id)
    public = bundle.public
    got = {(f.kind, f.origin, f.layer) for f in public.faults_public}

    want: set[tuple[str, str, str]] = set()
    for f in bundle.fault_plan.faults:
        if f.kind == "missing_file" and f.mode == "sticky":
            want.add(("missing_file", "staged", "filesystem"))
        else:
            want.add((f.kind, "injected", "boundary"))
    for h in public.harness_faults:
        want.add((h.kind, "real", provenance.HARNESS_FAULT_LAYERS[h.kind]))

    assert got == want
    # a harness fault is a REAL failure, always
    assert all(f.origin == "real" for f in public.faults_public if f.kind in provenance.HARNESS_FAULT_LAYERS)


@pytest.mark.parametrize("scenario_id", ALL_IDS)
def test_public_checks_mirror_the_grader_specs(scenario_id: str) -> None:
    bundle = scenarios.get_bundle(scenario_id)
    private = {c.id: c for c in bundle.checks}
    public = {c.id: c for c in bundle.public.checks}
    assert set(public) == set(private), "the UI must be told exactly what the grader will score"
    for cid, spec in public.items():
        assert spec.weight == private[cid].weight
        assert spec.description


@pytest.mark.parametrize("scenario_id", ALL_IDS)
def test_every_check_id_is_implemented(scenario_id: str) -> None:
    bundle = scenarios.get_bundle(scenario_id)
    assert bundle.checks, f"{scenario_id} has no checks"
    for spec in bundle.checks:
        assert spec.id in grader.CHECKS, f"{scenario_id} references unknown check {spec.id}"
        assert spec.weight > 0


@pytest.mark.parametrize("scenario_id", ALL_IDS)
def test_fixture_and_hidden_tests_exist(scenario_id: str) -> None:
    bundle = scenarios.get_bundle(scenario_id)
    assert bundle.fixture_dir.is_dir(), f"missing fixture {bundle.fixture}"
    hidden = bundle.hidden_tests_dir
    assert hidden is not None and hidden.is_dir(), f"missing hidden tests for {scenario_id}"
    assert list(hidden.glob("test_*.py")), f"{scenario_id} hidden test dir has no tests"


def test_fault_kinds_are_derived_from_the_plan() -> None:
    assert scenarios.get_bundle("missing-config").public.fault_kinds == ["missing_file"]
    assert scenarios.get_bundle("locked-file").public.fault_kinds == ["denied_write"]
    assert scenarios.get_bundle("lost-ack").public.fault_kinds == ["ack_lost"]
    assert set(scenarios.get_bundle("gauntlet").public.fault_kinds) == {
        "missing_file", "denied_write", "ack_lost"
    }
    # worker-crash injects nothing: its only failure is a real one
    assert scenarios.get_bundle("worker-crash").public.fault_kinds == []


def test_one_kind_can_be_two_different_stories() -> None:
    """`missing_file` is staged or injected depending on the mode, and the UI must see both.

    `missing-config` has one of each: config/settings.json is really gone (staged — the ENOENT is a
    real OS error the agent can fix by recreating the file) while README.md is only pretending
    (injected — it was on disk the whole time). `gauntlet` has only the staged one.
    """
    mc = {(f.kind, f.origin, f.layer) for f in scenarios.get_bundle("missing-config").public.faults_public}
    assert mc == {
        ("missing_file", "staged", "filesystem"),
        ("missing_file", "injected", "boundary"),
    }
    gauntlet = [f for f in scenarios.get_bundle("gauntlet").public.faults_public if f.kind == "missing_file"]
    assert [f.origin for f in gauntlet] == ["staged"]


def test_worker_crash_scenario_declares_a_real_harness_fault() -> None:
    bundle = scenarios.get_bundle("worker-crash")
    public = bundle.public
    assert bundle.fault_plan.faults == [], "worker-crash injects nothing; the failure is real"
    assert len(public.harness_faults) == 1
    hf = public.harness_faults[0]
    assert (hf.kind, hf.tool, hf.path, hf.nth) == ("worker_crash", "write_file", "CHANGELOG.md", 1)
    assert hf.after_ms >= 0
    assert len(public.faults_public) == 1
    only = public.faults_public[0]
    assert (only.kind, only.origin, only.layer) == ("worker_crash", "real", "harness")
    assert only.description == provenance.DESC_WORKER_CRASH
    # it is graded by the same rule as lost-ack
    assert "verified_before_rewrite" in {c.id for c in public.checks}


def test_scenarios_without_harness_faults_publish_an_empty_list() -> None:
    for sid in ("lost-ack", "gauntlet", "missing-config", "locked-file"):
        assert scenarios.get_bundle(sid).public.harness_faults == []


def test_setup_overlay_is_read_from_disk_and_contains_the_bug() -> None:
    bundle = scenarios.get_bundle("locked-file")
    assert len(bundle.setup) == 1
    op = bundle.setup[0]
    assert op.op == "write"
    assert op.path == "src/ratelimiter/limits.py"
    text = op.content.decode()
    # the planted off-by-one the agent has to find
    assert "math.floor(settings.capacity * settings.burst_multiplier) - 1" in text


def test_missing_config_plan_shape() -> None:
    plan = scenarios.get_bundle("missing-config").fault_plan
    kinds = [(f.kind, f.path, f.mode, f.hits) for f in plan.faults]
    assert ("missing_file", "config/settings.json", "sticky", None) in kinds
    assert ("missing_file", "README.md", "transient", 1) in kinds


def test_unknown_scenario_raises() -> None:
    with pytest.raises(scenarios.ScenarioNotFound):
        scenarios.get_bundle("does-not-exist")


def test_list_scenarios_is_the_public_projection() -> None:
    public = scenarios.list_scenarios()
    assert CORE <= {s.id for s in public}
    assert all(isinstance(s, Scenario) for s in public)
