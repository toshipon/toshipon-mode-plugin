"""Static checks of the container image's definition (container/Dockerfile, .dockerignore). They
read the text; building and running the image is the CI job `container`."""

import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[3]
DOCKERFILE = PLUGIN / "container" / "Dockerfile"
DOCKERIGNORE = PLUGIN / ".dockerignore"
DIGEST = re.compile(r"[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")


def instructions(text):
    """(INSTRUCTION, arguments) with continuation lines joined and comments dropped."""
    joined, current = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        joined.append(current + stripped)
        current = ""
    pairs = []
    for line in joined:
        name, _, arguments = line.partition(" ")
        pairs.append((name.upper(), arguments.strip()))
    return pairs


STEPS = instructions(DOCKERFILE.read_text(encoding="utf-8"))


def of(name):
    return [arguments for instruction, arguments in STEPS if instruction == name]


def test_every_base_is_pinned_by_tag_and_digest():
    bases = of("FROM")
    assert bases
    for base in bases:
        assert DIGEST.fullmatch(base.split()[0]), base


def test_the_image_does_not_run_as_root():
    users = of("USER")
    assert users, "no USER: the image would run as root"
    user = users[-1].split(":")[0]
    assert user not in {"root", "0"}


def test_nothing_is_fetched_with_add_or_piped_into_a_shell():
    assert of("ADD") == [], "ADD can fetch a URL or unpack an archive; COPY is used instead"
    piped = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(ba|z|da)?sh\b")
    assert [run for run in of("RUN") if piped.search(run)] == []


def test_the_entrypoint_is_the_isolated_launcher():
    (entrypoint,) = of("ENTRYPOINT")
    assert entrypoint == '["python", "-I", "/opt/sre-harness/runtime/sre_harness_main.py"]'
    assert of("ENV") == ["AI_HARNESS_HOME=/state"]


def test_the_build_fails_without_fts5():
    assert [run for run in of("RUN") if "store.fts5_available()" in run and "sys.exit" in run]


@pytest.mark.parametrize("tool", ["gh", "claude", "opa", "sqlite-vec", "sqlite_vec", "sentence"])
def test_no_tool_that_reaches_the_network_or_a_model_is_installed(tool):
    installs = " ".join(run for run in of("RUN") if "install" in run)
    assert not re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", installs)


def test_the_build_context_lets_only_the_runtime_in():
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    allowed = [line[1:] for line in lines if line.startswith("!")]
    assert not [path for path in allowed if "tests" in path or "examples" in path]
    copied = " ".join(of("COPY")).split()
    sources = [path for path in copied if not path.startswith("/")]
    for source in sources:
        assert any(source.rstrip("/") == path.rstrip("/") for path in allowed), source
