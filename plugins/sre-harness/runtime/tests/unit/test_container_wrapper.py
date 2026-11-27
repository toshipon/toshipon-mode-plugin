"""scripts/sre-harness-container with a fake `docker` first on PATH: the argv it builds, what it
refuses before starting anything, and that stdout and the exit code are the container's."""

import io
import json
import os
import pty
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[3]
WRAPPER = PLUGIN / "scripts" / "sre-harness-container"
VERSION = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
IMAGE = f"sre-harness:{VERSION}"
ANSWER = '{"status": "ok"}'

FAKE_DOCKER = """#!{python}
import json, os, sys
argv = sys.argv[1:]
record = {{"argv": argv}}
if argv[:1] == ["run"] and "-i" in argv and "-t" not in argv:
    record["stdin"] = sys.stdin.buffer.read().hex()
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
command = argv[:2]
if command == ["image", "inspect"]:
    sys.exit(int(os.environ.get("FAKE_IMAGE_EXIT", "0")))
if command[:1] == ["version"]:
    sys.exit(int(os.environ.get("FAKE_VERSION_EXIT", "0")))
if command[:1] == ["build"]:
    print("Step 1/1 : progress")
    sys.exit(0)
if command[:1] == ["run"]:
    print("container noise", file=sys.stderr)
    sys.stdout.write(os.environ.get("FAKE_RUN_STDOUT", ""))
    sys.exit(int(os.environ.get("FAKE_RUN_EXIT", "0")))
sys.exit(0)
"""


@pytest.fixture
def docker(tmp_path):
    """A fake docker on PATH. Returns run(argv, **env) -> (result, [recorded calls])."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER.format(python=sys.executable))
    fake.chmod(0o755)
    log = tmp_path / "docker.log"
    work = tmp_path / "work"
    work.mkdir()

    def run(argv, path=f"{bin_dir}:/usr/bin:/bin", stdin=None, cwd=work, **extra):
        log.write_text("")
        env = {
            "PATH": path,
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_RUN_STDOUT": ANSWER,
            **extra,
        }
        result = subprocess.run(
            [str(WRAPPER), *argv],
            env=env,
            cwd=cwd,
            input=stdin,
            capture_output=True,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    run.work = work
    return run


def run_call(calls):
    (found,) = [call["argv"] for call in calls if call["argv"][:1] == ["run"]]
    return found


def options(argv, flag):
    return [argv[index + 1] for index, value in enumerate(argv) if value == flag]


SEARCH = ["knowledge", "search", "--principal", "workspace-a", "--query", "queue の保持期間"]


def test_a_search_runs_in_a_throwaway_container_without_network(docker):
    result, calls = docker(SEARCH)
    assert result.returncode == 0, result.stderr
    argv = run_call(calls)
    for flag in ("--rm", "--read-only"):
        assert flag in argv
    assert options(argv, "--network") == ["none"]
    assert options(argv, "--cap-drop") == ["ALL"]
    assert options(argv, "--security-opt") == ["no-new-privileges"]
    assert options(argv, "--pull") == ["never"]
    assert options(argv, "--pids-limit") and options(argv, "--memory") == ["512m"]
    assert [value.split(":")[0] for value in options(argv, "--tmpfs")] == ["/tmp"]
    # The harness argv follows the image unchanged.
    assert argv[argv.index(IMAGE) + 1 :] == SEARCH


def test_stdout_is_the_containers_and_nothing_else(docker):
    result, _ = docker(SEARCH)
    assert result.stdout.decode() == ANSWER
    assert b"container noise" in result.stderr


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_the_exit_code_is_the_containers(docker, code):
    result, _ = docker(SEARCH, FAKE_RUN_EXIT=str(code))
    assert result.returncode == code


def test_the_state_is_the_named_volume_and_no_bind_mount_is_writable(docker):
    result, calls = docker(SEARCH, SRE_HARNESS_VOLUME="team-state")
    assert result.returncode == 0, result.stderr
    argv = run_call(calls)
    assert "--mount" not in argv and not any(value.startswith("--mount=") for value in argv)
    mounts = options(argv, "-v")
    writable = [value for value in mounts if not value.endswith(":ro")]
    assert writable == ["team-state:/state"]
    assert f"{docker.work.resolve()}:/work:ro" in mounts
    assert options(argv, "-w") == ["/work"]


def test_the_default_volume_and_image(docker):
    _, calls = docker(SEARCH)
    argv = run_call(calls)
    assert "sre-harness-state:/state" in options(argv, "-v")
    assert IMAGE in argv


@pytest.mark.parametrize(
    "volume", ["/tmp/state", "./state", "~/state", "state/x", "..", ".state", "a:b", ""]
)
def test_a_host_path_for_the_state_is_refused_before_docker_runs(docker, volume):
    result, calls = docker(SEARCH, SRE_HARNESS_VOLUME=volume)
    if volume == "":  # empty means the default
        assert result.returncode == 0
        return
    assert result.returncode == 2
    assert b"volume name" in result.stderr
    assert calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["review", "collect", "--repo", "example-org/demo"],
        ["spec", "analyze"],
        ["iac", "validate"],
        ["runs"],
        ["runs", "show"],
        ["nonsense"],
    ],
)
def test_groups_outside_knowledge_and_runs_list_are_refused_without_docker(docker, argv):
    result, calls = docker(argv)
    assert result.returncode == 2
    assert result.stdout == b""
    assert calls == []


def test_a_network_group_points_at_the_host_launcher(docker):
    result, _ = docker(["review", "collect"])
    assert b"scripts/sre-harness" in result.stderr


def test_runs_list_runs(docker):
    result, calls = docker(["runs", "list"])
    assert result.returncode == 0
    assert run_call(calls)[-2:] == ["runs", "list"]


def test_no_docker_is_exit_2(docker, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result, calls = docker(SEARCH, path=f"{empty}:/usr/bin:/bin")
    if calls == [] and b"not installed" not in result.stderr:
        pytest.skip("a docker is installed in /usr/bin or /bin")
    assert result.returncode == 2
    assert b"docker is not installed" in result.stderr


def test_an_unreachable_daemon_is_exit_2(docker):
    result, calls = docker(SEARCH, FAKE_IMAGE_EXIT="1", FAKE_VERSION_EXIT="1")
    assert result.returncode == 2
    assert b"Docker daemon" in result.stderr
    assert not [call for call in calls if call["argv"][:1] == ["run"]]


def test_a_missing_image_is_exit_2_and_says_how_to_build_it(docker):
    result, calls = docker(SEARCH, FAKE_IMAGE_EXIT="1")
    assert result.returncode == 2
    assert b"image build" in result.stderr
    assert not [call for call in calls if call["argv"][:1] == ["run"]]


@pytest.mark.parametrize("code", [125, 126, 127])
def test_docker_failing_to_start_the_container_is_exit_2(docker, code):
    result, _ = docker(SEARCH, FAKE_RUN_EXIT=str(code))
    assert result.returncode == 2


def test_the_automated_run_markers_reach_the_container(docker):
    _, calls = docker(SEARCH, AI_HARNESS_AUTOMATED="1", AI_HARNESS_ACTIVE="1", GH_TOKEN="x")
    assert options(run_call(calls), "-e") == ["AI_HARNESS_AUTOMATED", "AI_HARNESS_ACTIVE"]
    _, calls = docker(SEARCH)
    assert options(run_call(calls), "-e") == []


ANSWER_ARGV = [
    "knowledge",
    "decision",
    "answer",
    "--request",
    "DR-000000000000",
    "--option",
    "a",
    "--responder",
    "alice",
]


def test_without_a_terminal_no_tty_is_asked_for(docker):
    result, calls = docker(ANSWER_ARGV)
    argv = run_call(calls)
    assert "-t" not in argv and "-i" not in argv and "-it" not in argv
    assert result.returncode == 0


def test_at_a_terminal_decision_answer_gets_a_tty(docker, tmp_path):
    # stdin and stdout on a pty, as when a person runs it.
    primary, secondary = pty.openpty()
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_RUN_STDOUT": ANSWER,
    }
    try:
        process = subprocess.run(
            [str(WRAPPER), *ANSWER_ARGV],
            env=env,
            cwd=docker.work,
            stdin=secondary,
            stdout=secondary,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    finally:
        os.close(secondary)
        os.close(primary)
    assert process.returncode == 0, process.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    argv = run_call(calls)
    assert "-i" in argv and "-t" in argv


def test_read_only_mounts_are_mounted_read_only_at_their_own_path(docker, tmp_path):
    first, second = tmp_path / "docs", tmp_path / "repo"
    first.mkdir()
    second.mkdir()
    result, calls = docker(SEARCH, SRE_HARNESS_RO_MOUNTS=f"{first}:{second}")
    assert result.returncode == 0, result.stderr
    mounts = options(run_call(calls), "-v")
    for directory in (first.resolve(), second.resolve()):
        assert f"{directory}:{directory}:ro" in mounts
    assert [value for value in mounts if not value.endswith(":ro")] == ["sre-harness-state:/state"]


@pytest.mark.parametrize("entry", ["relative/dir", "/usr", "/usr/bin", "/", "/does-not-exist"])
def test_a_read_only_mount_that_is_not_a_plain_host_directory_is_refused(docker, entry):
    result, calls = docker(SEARCH, SRE_HARNESS_RO_MOUNTS=entry)
    assert result.returncode == 2
    assert calls == []


def test_an_odd_memory_limit_is_refused(docker):
    result, calls = docker(SEARCH, SRE_HARNESS_MEMORY="lots")
    assert result.returncode == 2 and calls == []


def test_image_build_tags_the_plugin_version_and_keeps_stdout_empty(docker):
    result, calls = docker(["image", "build"])
    assert result.returncode == 0, result.stderr
    (argv,) = [call["argv"] for call in calls]
    assert argv[0] == "build"
    assert options(argv, "--tag") == [IMAGE]
    assert options(argv, "--build-arg") == [f"SRE_HARNESS_VERSION={VERSION}"]
    assert options(argv, "--file") == [str(PLUGIN / "container" / "Dockerfile")]
    assert argv[-1] == str(PLUGIN)
    assert result.stdout == b""
    assert b"progress" in result.stderr


def test_volume_backup_mounts_the_volume_read_only(docker):
    result, calls = docker(["volume", "backup"], FAKE_RUN_STDOUT="tar bytes")
    assert result.returncode == 0, result.stderr
    argv = run_call(calls)
    assert options(argv, "-v") == ["sre-harness-state:/state:ro"]
    assert options(argv, "--network") == ["none"] and "--read-only" in argv
    assert argv[argv.index(IMAGE) + 1 :] == [
        "-I",
        "/opt/sre-harness/runtime/container_state.py",
        "backup",
        "/state/sre-harness",
    ]
    assert result.stdout == b"tar bytes"


def test_volume_restore_reads_stdin_into_the_volume(docker):
    result, calls = docker(["volume", "restore"], stdin=b"")
    assert result.returncode == 0, result.stderr
    argv = run_call(calls)
    assert "-i" in argv and options(argv, "-v") == ["sre-harness-state:/state"]
    assert argv[-2:] == ["restore", "/state/sre-harness"]


def test_volume_import_sends_only_the_importable_parts_of_a_host_state_root(docker, tmp_path):
    source = tmp_path / "host-state" / "sre-harness"
    for name in ("knowledge", "registry/RG-0001", "runs/run-x", "cache/git/x.git", "other"):
        (source / name).mkdir(parents=True)
    (source / "knowledge" / "config.json").write_text("{}")
    (source / "knowledge" / "index.sqlite3").write_text("index")
    (source / "registry" / "RG-0001" / "v0001.md").write_text("rule")
    (source / "other" / "secret.txt").write_text("not this")
    result, calls = docker(["volume", "import", str(source)])
    assert result.returncode == 0, result.stderr
    (call,) = [call for call in calls if call["argv"][:1] == ["run"]]
    argv = call["argv"]
    assert "-i" in argv and options(argv, "-v") == ["sre-harness-state:/state"]
    assert argv[-2:] == ["import", "/state/sre-harness"]
    with tarfile.open(fileobj=io.BytesIO(bytes.fromhex(call["stdin"]))) as archive:
        names = set(archive.getnames())
    assert "knowledge/config.json" in names
    assert "registry/RG-0001/v0001.md" in names
    assert {"runs/run-x", "cache/git/x.git"} <= names
    assert not {name for name in names if name.startswith("other") or "index" in name}


def test_volume_import_without_anything_to_import_is_exit_2(docker, tmp_path):
    result, calls = docker(["volume", "import", str(tmp_path)])
    assert result.returncode == 2 and calls == []


def test_no_argument_prints_usage_to_stderr(docker):
    result, calls = docker([])
    assert result.returncode == 2 and result.stdout == b"" and b"usage" in result.stderr
    assert calls == []


def test_the_wrapper_is_bash_3_2_syntax_and_executable():
    assert os.access(WRAPPER, os.X_OK)
    code = [line for line in WRAPPER.read_text().splitlines() if not line.lstrip().startswith("#")]
    text = "\n".join(code)
    for construct in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "|&", "coproc"):
        assert construct not in text
    assert subprocess.run(["bash", "-n", str(WRAPPER)]).returncode == 0
