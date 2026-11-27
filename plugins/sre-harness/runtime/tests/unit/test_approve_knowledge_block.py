"""`review approve` with a `knowledge` block: a person places the rule in the knowledge index
(scope, access, enforcement) and the typed hash covers it. Everything here is synthetic."""

import copy
import io
import json

import pytest

from examples.synthetic import review_registry as rr
from examples.synthetic.github_responses import THIRTY_EVALUATION, evaluation_scenario
from sre_harness import cli, paths, records
from sre_harness.errors import BoundaryError, InputError
from sre_harness.knowledge import block
from sre_harness.knowledge.adapters import review_registry as adapter
from sre_harness.review import approve, candidates, registry
from sre_harness.review import cli as review_cli
from tests.helpers import evaluate_cli, generated_run, run_cli, run_dir

REASON = "Matches the team's HTTP policy."
REPO = "example-org/demo"  # the repo of the synthetic evaluation scenario

CONFIG = {
    "schema_version": 1,
    "projects": {
        "project-demo": {
            "repos": [REPO],
            "default_access": {"classification": "internal", "readers": ["team-demo"]},
            "components": {"api": {"path_globs": ["src/**"]}},
        },
        "project-other": {
            "repos": ["example-org/other"],
            "default_access": {"classification": "confidential", "readers": ["team-other"]},
            "components": {},
        },
    },
    "principals": {
        "workspace-demo": {"readers": ["team-demo"], "classifications": ["internal"]},
        "workspace-sre": {
            "readers": ["team-demo", "team-sre"],
            "classifications": ["internal", "confidential"],
        },
        "workspace-other": {"readers": ["team-other"], "classifications": ["confidential"]},
    },
}

MANDATORY = block.Options(
    scope="component",
    project="project-demo",
    component="api",
    readers=("team-sre",),
    enforcement="mandatory",
    applies_when=("HTTP クライアントを新しく作る",),
    exceptions=("ストリーム受信",),
)


@pytest.fixture
def evaluated(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario(THIRTY_EVALUATION))
    code, summary = evaluate_cli(home, run_id, split_path)
    assert code == 0 and summary["status"] == "evaluated", summary
    return run_id


@pytest.fixture
def configured(home):
    write_config(home)
    return home


def root(env):
    return paths.state_root(env)


def write_config(env, value=None):
    target = paths.child(root(env), "knowledge", "config.json")
    paths.write_private(target, records.canonical(value or CONFIG))


def digest(env, run_id, candidate_id="IC-0001"):
    record, _ = candidates.load(run_dir(env, run_id), candidate_id)
    return record["artifact"]["content_sha256"]


def decide(env, run_id, knowledge=None, typed=None, candidate_id="IC-0001"):
    """Approve as a person who types the hash prefix the screen asks for."""
    shown, asked = [], []

    def ask(prompt):
        asked.append(prompt)
        if typed is not None:
            return typed
        screen = "\n".join(shown)
        wanted = "confirmation_sha256" if "confirmation_sha256" in prompt else "content_sha256"
        value = screen.split(f"- {wanted}: `", 1)[1].split("`", 1)[0]
        return value[: approve.PREFIX_LENGTH]

    summary = approve.approve(
        run_dir(env, run_id),
        root(env),
        candidate_id,
        "alice",
        REASON,
        ask=ask,
        show=shown.append,
        knowledge=knowledge,
    )
    return summary, "\n".join(shown), asked


def versions(env, entry="RG-0001"):
    return registry.entries(root(env))[entry]


def version_file(env, entry="RG-0001", number=1):
    return paths.child(root(env), "registry", entry, registry.version_name(number, "json"))


# Without a block nothing changes ---------------------------------------------------------------


def test_no_option_writes_the_version_as_before(evaluated, configured):
    _, screen, asked = decide(configured, evaluated, knowledge=block.Options())
    written = json.loads(version_file(configured).read_text())
    assert "knowledge" not in written
    assert "confirmation_sha256" not in screen and "Knowledge index placement" not in screen
    assert "content_sha256" in asked[0] and "confirmation_sha256" not in asked[0]
    assert version_file(configured).read_text() == records.canonical(written) + "\n"


def test_a_version_without_a_block_is_read_as_before(evaluated, configured):
    decide(configured, evaluated)
    assert "knowledge" not in versions(configured)[-1]
    assert registry.active_rules(root(configured), REPO)[0]["id"] == "RG-0001"


# The block is what the person confirms ----------------------------------------------------------


def test_the_block_is_recorded_and_the_screen_shows_it(evaluated, configured):
    summary, screen, asked = decide(configured, evaluated, knowledge=MANDATORY)
    assert summary["registry_id"] == "RG-0001"
    recorded = versions(configured)[-1]["knowledge"]
    assert recorded == {
        "scope": {
            "level": "component",
            "project_id": "project-demo",
            "component_id": "api",
            "task_id": None,
        },
        "access": {"classification": "internal", "readers": ["team-sre"]},
        "enforcement": "mandatory",
        "applies_when": ["HTTP クライアントを新しく作る"],
        "exceptions": ["ストリーム受信"],
    }
    assert "## Knowledge index placement" in screen
    assert "- scope: component project-demo api" in screen
    assert "- access: internal; readers team-sre" in screen
    assert "**MANDATORY**" in screen and "mandatory_overflow" in screen
    assert "HTTP クライアントを新しく作る" in screen and "ストリーム受信" in screen
    assert "confirmation_sha256" in asked[0]


def test_the_content_hash_alone_does_not_approve_a_block(evaluated, configured):
    typed = digest(configured, evaluated)[: approve.PREFIX_LENGTH]
    with pytest.raises(BoundaryError, match="typed hash"):
        decide(configured, evaluated, knowledge=MANDATORY, typed=typed)
    assert registry.entries(root(configured)) == {}


def test_changing_the_block_changes_the_hash_to_type():
    content = "a" * 64
    first = {**copy.deepcopy(rr_block()), "enforcement": "advisory"}
    second = {**copy.deepcopy(rr_block()), "enforcement": "mandatory"}
    third = {**copy.deepcopy(rr_block()), "exceptions": ["別の例外"]}
    hashes = {block.confirmation(content, value) for value in (first, second, third)}
    assert len(hashes) == 3 and content not in hashes
    assert block.confirmation(content, None) == content


def rr_block():
    return {
        "scope": {
            "level": "project",
            "project_id": "project-demo",
            "component_id": None,
            "task_id": None,
        },
        "access": {"classification": "internal", "readers": ["team-demo"]},
        "enforcement": "advisory",
        "applies_when": [],
        "exceptions": [],
    }


def test_options_left_out_come_from_the_config(evaluated, configured):
    decide(configured, evaluated, knowledge=block.Options(enforcement="mandatory"))
    assert versions(configured)[-1]["knowledge"] == {**rr_block(), "enforcement": "mandatory"}


# Refusals: exit 2 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options, message",
    [
        (block.Options(scope="organization", project="project-demo"), "must leave project_id"),
        (block.Options(scope="project"), "needs project_id"),
        (block.Options(scope="component", project="project-demo"), "needs component_id"),
        (block.Options(project="project-demo"), "need --knowledge-scope"),
        (block.Options(scope="project", project="project-nowhere"), "not in knowledge"),
        (block.Options(scope="component", project="project-demo", component="db"), "component"),
        (block.Options(readers=("team-nobody",)), "team-nobody"),
        (block.Options(applies_when=("改行を\n含む",)), "one line"),
        (block.Options(exceptions=("x" * 201,)), "invalid"),
    ],
    ids=[
        "organization with a project",
        "project without its id",
        "component without its id",
        "ids without a scope",
        "project not in the config",
        "component not in the project",
        "reader no principal holds",
        "two lines",
        "too long",
    ],
)
def test_an_inconsistent_block_is_refused_with_exit_2(evaluated, configured, options, message):
    with pytest.raises(InputError, match=message) as refused:
        decide(configured, evaluated, knowledge=options)
    assert refused.value.exit_code == 2
    assert registry.entries(root(configured)) == {}


def test_a_block_needs_the_knowledge_config(evaluated, home):
    with pytest.raises(InputError, match="needs knowledge/config.json") as refused:
        decide(home, evaluated, knowledge=block.Options(enforcement="mandatory"))
    assert refused.value.exit_code == 2
    assert registry.entries(root(home)) == {}


def test_an_organization_rule_of_a_repo_in_no_project_needs_its_access(evaluated, home):
    write_config(
        home, {**CONFIG, "projects": {"project-other": CONFIG["projects"]["project-other"]}}
    )
    with pytest.raises(InputError, match="no project gives"):
        decide(home, evaluated, knowledge=block.Options(scope="organization"))
    with pytest.raises(InputError, match="gives no project"):
        decide(home, evaluated, knowledge=block.Options(enforcement="mandatory"))
    decide(
        home,
        evaluated,
        knowledge=block.Options(
            scope="organization", classification="confidential", readers=("team-other",)
        ),
    )
    assert versions(home)[-1]["knowledge"]["scope"]["level"] == "organization"


@pytest.mark.parametrize(
    "argv",
    [
        ["review", "reject", "--run", "{run}", "--candidate", "IC-0002"],
        ["review", "retire", "--entry", "RG-0001"],
    ],
    ids=["reject", "retire"],
)
@pytest.mark.parametrize(
    "option",
    [
        ["--knowledge-enforcement", "mandatory"],
        ["--knowledge-scope", "project"],
        ["--knowledge-reader", "team-demo"],
    ],
)
def test_reject_and_retire_take_no_block(evaluated, configured, monkeypatch, argv, option):
    decide(configured, evaluated)
    monkeypatch.setattr(review_cli, "_is_terminal", lambda stream: True)
    command = [part.replace("{run}", evaluated) for part in argv]
    code, _ = run_cli([*command, "--reviewer", "bob", "--reason", "No.", *option], configured)
    assert code == 2
    assert [v["status"] for v in versions(configured)] == ["approved"]


def test_the_schema_keeps_the_block_on_approved_versions_only(evaluated, configured):
    decide(configured, evaluated, knowledge=MANDATORY)
    approved = versions(configured)[-1]
    retired = {**approved, "status": "retired", "evaluation": None}
    retired["decision"] = {**approved["decision"], "via": "command"}
    assert records.violations("review-registry", approved) == []
    assert records.violations("review-registry", retired) != []


# The command line --------------------------------------------------------------------------------


def test_the_command_line_passes_the_block(evaluated, configured, monkeypatch):
    monkeypatch.setattr(review_cli, "_is_terminal", lambda stream: True)
    out = io.StringIO()

    def ask(prompt):
        screen = out.getvalue()
        return screen.split("- confirmation_sha256: `", 1)[1][: approve.PREFIX_LENGTH]

    monkeypatch.setattr("builtins.input", ask)
    argv = ["review", "approve", "--run", evaluated, "--candidate", "IC-0001"]
    argv += ["--reviewer", "alice", "--reason", REASON]
    argv += ["--knowledge-scope", "component", "--knowledge-project", "project-demo"]
    argv += ["--knowledge-component", "api", "--knowledge-classification", "internal"]
    argv += ["--knowledge-reader", "team-sre", "--knowledge-reader", "team-demo"]
    argv += ["--knowledge-enforcement", "mandatory", "--knowledge-applies-when", "条件 1"]
    argv += ["--knowledge-applies-when", "条件 2", "--knowledge-exception", "例外 1"]
    assert cli.main(argv, env=configured, out=out) == 0
    recorded = versions(configured)[-1]["knowledge"]
    assert recorded["access"] == {
        "classification": "internal",
        "readers": ["team-demo", "team-sre"],
    }
    assert recorded["applies_when"] == ["条件 1", "条件 2"] and recorded["exceptions"] == ["例外 1"]
    assert recorded["enforcement"] == "mandatory"


# Retiring and the adapter -------------------------------------------------------------------------


def test_a_retired_rule_keeps_its_placement_and_is_advisory(evaluated, configured):
    decide(configured, evaluated, knowledge=MANDATORY)
    approve.retire(root(configured), "RG-0001", "bob", "Replaced.")
    found = versions(configured)
    assert [v["status"] for v in found] == ["approved", "retired"]
    assert "knowledge" not in found[-1]
    project = CONFIG["projects"]["project-demo"]
    decided = adapter.norms(found, "project-demo", project)
    assert decided["scope"]["component_id"] == "api"
    assert decided["access"]["readers"] == ["team-sre"]
    assert decided["enforcement"] == "advisory"


@pytest.mark.parametrize("status", ["rejected", "retired"])
def test_a_block_on_a_version_that_is_not_approved_never_makes_it_mandatory(status):
    # Hand-made: the schema already refuses such a file. The adapter does not rely on that.
    stored = {"status": status, "knowledge": {**rr_block(), "enforcement": "mandatory"}}
    decided = adapter.norms([stored], "project-demo", CONFIG["projects"]["project-demo"])
    assert decided["enforcement"] == "advisory"
    assert decided["access"] == rr_block()["access"]


def test_an_entry_without_a_block_keeps_the_defaults():
    stored = {"status": "approved"}
    decided = adapter.norms([stored], "project-alpha", rr.CONFIG["projects"]["project-alpha"])
    assert decided == {
        "scope": {
            "level": "project",
            "project_id": "project-alpha",
            "component_id": None,
            "task_id": None,
        },
        "access": {"classification": "internal", "readers": ["team-alpha"]},
        "enforcement": "advisory",
        "applies_when": [],
        "exceptions": [],
    }


# Through the index -----------------------------------------------------------------------------


def search(env, principal, *extra):
    argv = ["knowledge", "search", "--principal", principal, "--project", "project-demo"]
    return run_cli([*argv, "--component", "api", *extra], env)


def test_a_mandatory_block_is_mandatory_in_search_for_its_readers_only(evaluated, configured):
    decide(configured, evaluated, knowledge=MANDATORY)
    code, updated = run_cli(["knowledge", "update", "--source", "review-registry"], configured)
    assert code == 0 and updated["counts"]["indexed"] == 1, updated

    code, found = search(configured, "workspace-sre", "--max-tokens", "6000")
    assert code == 0 and found["status"] == "ok"
    assert found["mandatory"]["count"] == 1
    (shown,) = found["records"]
    assert shown["enforcement"] == "mandatory" and "mandatory" in shown["why_selected"]
    assert shown["scope"] == {
        "level": "component",
        "project_id": "project-demo",
        "component_id": "api",
    }
    assert shown["applies_when"] == ["HTTP クライアントを新しく作る"]

    code, other = search(configured, "workspace-demo", "--max-tokens", "6000")
    assert code == 0 and other["records"] == [] and other["mandatory"]["count"] == 0
    assert other["truncated"]["count"] == 0
    assert shown["id"] not in json.dumps(other)

    code, project_only = run_cli(
        ["knowledge", "search", "--principal", "workspace-sre", "--project", "project-demo"],
        configured,
    )
    assert code == 0 and project_only["mandatory"]["count"] == 0

    code, overflow = search(configured, "workspace-sre", "--max-tokens", "10")
    assert code == 1 and overflow["status"] == "mandatory_overflow"
    assert overflow["records"] == [] and overflow["mandatory"]["ids"] == [shown["id"]]


def test_migrate_plans_and_checks_entries_with_a_block(evaluated, configured):
    decide(configured, evaluated, knowledge=MANDATORY)
    code, planned = run_cli(["knowledge", "migrate", "--dry-run"], configured)
    assert code == 0
    (row,) = planned["plan"]["records"]
    assert row["scope"]["component_id"] == "api" and row["enforcement"] == "mandatory"
    assert row["access"] == {"classification": "internal", "readers": ["team-sre"]}
    code, migrated = run_cli(["knowledge", "migrate"], configured)
    assert code == 0 and migrated["check"]["ok"] and migrated["replaced"]
