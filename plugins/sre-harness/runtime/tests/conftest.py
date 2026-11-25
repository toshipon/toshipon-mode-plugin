import json
from types import SimpleNamespace

import pytest

from sre_harness import guard
from sre_harness.github.fixture import FixtureTransport, graphql_key
from sre_harness.review import cli as review_cli
from tests.helpers import FakeClock, envelope, prompt_parts


@pytest.fixture
def home(tmp_path):
    """An environment whose state home is a fresh directory outside any repository."""
    return {"AI_HARNESS_HOME": str(tmp_path / "state"), "PATH": "/usr/bin:/bin"}


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(review_cli, "clock", fake)
    monkeypatch.setattr(review_cli, "sleep", fake.sleep)
    return fake


@pytest.fixture
def fake_gh(monkeypatch):
    """Answer gh's argv from a fixture directory, recording every process that would start."""
    seen = []
    bundled = {
        guard.graphql_query(path.stem): path.stem for path in guard.QUERY_DIR.glob("*.graphql")
    }

    def install(directory):
        served = FixtureTransport(directory)

        def run(argv, **options):
            seen.append(SimpleNamespace(argv=list(argv), **options))
            if argv[2] == "graphql":
                pairs = argv[4:]
                variables, name = {}, None
                for flag, value in zip(pairs[::2], pairs[1::2]):
                    key, _, text = value.partition("=")
                    if key == "query":
                        name = bundled[text]
                    else:
                        variables[key] = int(text) if flag == "-F" else text
                key = graphql_key(name, variables)
            else:
                key = argv[-1]
            return SimpleNamespace(returncode=0, stdout=served.answer(key), stderr="noise")

        monkeypatch.setattr(guard, "_run", run)
        monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
        return seen

    return install


@pytest.fixture
def fake_claude(monkeypatch):
    """Answer claude's argv with an envelope instead of starting it. `answer` is the structured
    output, or a function of the prompt's DATA value. Returns the list of calls."""
    seen = []

    def install(answer, cost=0.01, returncode=0, raw=None):
        def run(argv, **options):
            prompt = options["input"]
            seen.append(SimpleNamespace(argv=list(argv), prompt=prompt, **options))
            if raw is not None:
                stdout = raw
            else:
                output = answer(prompt_parts(prompt)[1]) if callable(answer) else answer
                stdout = json.dumps(envelope(output, cost))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="secret noise")

        monkeypatch.setattr(guard, "_run", run)
        monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
        return seen

    return install
