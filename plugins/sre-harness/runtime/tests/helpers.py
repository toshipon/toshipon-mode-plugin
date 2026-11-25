"""Helpers shared by the review tests. Fixtures that use them live in conftest.py."""

import io
import json
from pathlib import Path

from examples.synthetic.github_responses import NOW
from sre_harness import cli

TOKEN = "synthetic-token-value-0123456789"
TOKEN_ENV = "REVIEW_READ_TOKEN"
PERIOD = ["--since", "2026-01-01T00:00:00Z", "--until", "2026-01-31T00:00:00Z"]


class FakeClock:
    """Epoch seconds that move only when the harness sleeps."""

    def __init__(self, now=NOW):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def run_cli(argv, env):
    """(exit code, parsed stdout or None)."""
    out = io.StringIO()
    code = cli.main(argv, env=env, out=out)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


def collect_fixture(directory, env, *extra, period=PERIOD):
    argv = ["review", "collect", "--repo", "example-org/demo", *period, "--fixture"]
    return run_cli([*argv, str(directory), *extra], env)


def run_dir(env, run_id):
    return Path(env["AI_HARNESS_HOME"]) / "sre-harness" / "runs" / run_id


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


LLM_ARGS = ["--auth-mode", "subscription", "--max-run-budget-usd", "1"]


def with_home(env, tmp_path):
    """The environment a subscription-mode claude call needs (HOME), still with no network."""
    return {**env, "HOME": str(tmp_path / "home")}


def prepared_run(tmp_path, env, fixture, name="fx"):
    """Collect and normalize a synthetic fixture. Returns the run id."""
    code, summary = collect_fixture(fixture.write(tmp_path / name), env)
    assert code == 0, summary
    code, result = run_cli(["review", "normalize", "--run", summary["run_id"]], env)
    assert code == 0 and result["rejected"] == 0
    return summary["run_id"]


def envelope(output, cost=0.01, **changes):
    """The result envelope `claude -p --output-format json` prints (fields as observed)."""
    value = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": output,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "modelUsage": {
            "claude-haiku-4-5": {
                "inputTokens": 1200,
                "outputTokens": 300,
                "costUSD": cost,
                "costBasis": "api",
            }
        },
        "num_turns": 1,
        "duration_ms": 900,
        "duration_api_ms": 800,
    }
    value.update(changes)
    return value


def prompt_parts(prompt):
    """(instructions, DATA value) of a prompt the harness built."""
    from sre_harness import llm

    head, _, rest = prompt.partition(llm.DATA_BEGIN)
    body, _, _ = rest.partition(llm.DATA_END)
    return head, json.loads(body)


RULES_TEXT = "# HTTP\n\n- Every outbound HTTP client call must set a timeout.\n"


def generated_run(
    tmp_path, env, fixture, *, split=True, t1=None, name="fx", generate=True, untrusted=False
):
    """collect -> normalize -> [split] -> analyze -> classify -> generate, all with stub.

    Returns (run id, split path or None)."""
    from examples.synthetic.github_responses import SPLIT_T1

    run_id = prepared_run(tmp_path, env, fixture, name=name)
    split_path = None
    if split:
        code, made = run_cli(["review", "split", "--run", run_id, "--t1", t1 or SPLIT_T1], env)
        assert code == 0, made
        split_path = made["split"]
    extra = ["--split", split_path] if split_path else []
    stub = ["--backend", "stub"]
    code, result = run_cli(["review", "analyze", "--run", run_id, *stub, *extra], env)
    assert code == 0, result
    rules = tmp_path / f"{name}-rules"
    rules.mkdir(exist_ok=True)
    (rules / "http.md").write_text(RULES_TEXT)
    argv = ["review", "classify", "--run", run_id, "--existing-rules", str(rules), *stub]
    code, result = run_cli(argv, env)
    assert code == 0, result
    if generate:
        flag = ["--include-untrusted"] if untrusted else []
        argv = ["review", "generate", "--run", run_id, *stub, *extra, *flag]
        code, result = run_cli(argv, env)
        assert code == 0, result
    return run_id, split_path


def evaluate_cli(env, run_id, split_path, *extra):
    argv = ["review", "evaluate", "--run", run_id, "--split", split_path, "--backend", "stub"]
    return run_cli([*argv, *extra], env)
