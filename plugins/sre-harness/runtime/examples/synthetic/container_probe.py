"""Measure the container path (scripts/sre-harness-container) on synthetic data only.

Run from `runtime/` with a built image (`scripts/sre-harness-container image build`):

    python -m examples.synthetic.container_probe --out DIR [--bind-mount]
    python -m examples.synthetic.container_probe --smoke          # the CI check, small

It writes DIR/container_probe.json and DIR/container_probe.md:

- startup    wall time of one call, through the wrapper and on the host directly, for
             `knowledge status` and `knowledge search` (300 synthetic records), RUNS times
             each; the first call (cold) apart from the rest. To tell apart where the time
             goes, the same is measured for `docker image inspect` (the wrapper's check),
             `docker run <image> true`, `docker run <image> python -I -c pass` and the host's
             `python -I -c pass`, all with the wrapper's hardening options.
- exclusive  two writers on one volume. `held`: a container holds knowledge/writer.lock (flock)
             and, while it is known to hold it, `knowledge update` runs in another container;
             it must stop with exit 1 and leave the generation alone. `concurrent`: two
             `knowledge update` containers started at once, PAIRS times; a pair where one stops
             on the lock overlapped, a pair where both wrote ran one after the other. The
             generation must go up by the number of updates that wrote.
- snapshot   one writer container updates WRITES times (every summary carries the version)
             while READERS containers run `knowledge search` in a loop. Each answer must equal
             the reference answer of the generation it reports (taken right after that update,
             before the next one), and no reader may fail.
- bind       with --bind-mount, `exclusive` and `snapshot` again with the state on a host
             directory bind-mounted at /state (docker run directly: the wrapper refuses it).

Every Docker object it makes is named `sre-harness-test-...` and removed at the end. The data
is made in a new directory under $HOME (Docker Desktop and colima share the home directory
with the VM by default; a directory outside it may look empty inside a container) and
removed at the end. Exit 2 when docker or the image is missing; with --smoke, exit 1 when a
check fails. Nothing here runs in the package or in CI except --smoke.
"""

import argparse
import json
import os
import platform
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from examples.synthetic import knowledge_corpus as kc
from sre_harness import paths, records

RUNTIME = Path(__file__).resolve().parents[2]
PLUGIN = RUNTIME.parent
WRAPPER = PLUGIN / "scripts" / "sre-harness-container"
LAUNCHER = PLUGIN / "scripts" / "sre-harness"
DOCKERFILE = PLUGIN / "container" / "Dockerfile"
PREFIX = "sre-harness-test-"
LOCK = "/state/sre-harness/knowledge/writer.lock"
LOCKED = "another knowledge writer holds"
# The wrapper's options (scripts/sre-harness-container `hardening`), for the docker runs the
# probe makes itself: the bind-mount case and the breakdown of the startup time.
HARDENING = [
    "--rm",
    "--pull",
    "never",
    "--network",
    "none",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,size=64m",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "128",
    "--memory",
    "512m",
]
HOLD_SECONDS = 4
SEARCH = ["knowledge", "search", "--principal", "workspace-a", "--project", "project-alpha"]
SNAPSHOT_SEARCH = [*SEARCH, "--max-tokens", "20000"]
FETCH_ID = "KN-904a3334f940"
HOLDER = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
    "print('released', flush=True)\n"
)


class Missing(Exception):
    """docker or the image is not there (exit 2)."""


# Pure parts (tested without docker) -------------------------------------------------------


def spread(values):
    """{n, min, median, max} in seconds, rounded to the millisecond; None for no values."""
    if not values:
        return None
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


def series(values):
    """The first call (cold) apart from the rest (warm)."""
    return {"cold": round(values[0], 3) if values else None, "warm": spread(values[1:])}


def classify_pair(first, second):
    """A pair of concurrent updates, each {code, locked}: overlapped (one stopped on the lock,
    the other wrote), serial (both wrote), or anomaly (anything else)."""
    codes = sorted([first["code"], second["code"]])
    if codes == [0, 1] and (first["locked"] or second["locked"]):
        return "overlapped"
    if codes == [0, 0]:
        return "serial"
    return "anomaly"


def answer_of(search):
    """What a reader is compared on: the generation and the records with their summaries."""
    shown = [*search["records"], *search["proposed"]]
    return {
        "generation": search["snapshot"]["generation"],
        "records": [[record["id"], record["summary"]] for record in shown],
        "truncated": search["truncated"]["count"],
    }


def mismatches(answers, references):
    """Reader answers that differ from the reference of the generation they report."""
    wrong = []
    for answer in answers:
        expected = references.get(answer["generation"])
        if expected is None or expected != answer:
            wrong.append(answer["generation"])
    return wrong


def variant(corpus, tag):
    """The corpus with `tag` at the end of every summary that has room: every record changes."""
    changed = json.loads(json.dumps(corpus))
    for item in changed["items"]:
        text = f"{item['summary']} [{tag}]"
        if len(text) <= 240:
            item["summary"] = text
    return changed


def base_digest(text):
    for line in text.splitlines():
        if line.startswith("FROM "):
            return line.split()[1]
    return None


# Docker -----------------------------------------------------------------------------------


def docker(*argv, check=True, **options):
    try:
        result = subprocess.run(["docker", *argv], capture_output=True, text=True, **options)
    except FileNotFoundError:
        raise Missing("docker is not installed or not on PATH.") from None
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(argv[:3])} failed: {result.stderr.strip()}")
    return result


def environment(image):
    version = json.loads(docker("version", "--format", "{{json .}}").stdout)
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    context = docker("context", "show", check=False).stdout.strip()
    operating = info.get("OperatingSystem", "")
    if "Docker Desktop" in operating:
        runtime = "Docker Desktop"
    elif "colima" in context or ".colima" in os.environ.get("DOCKER_HOST", ""):
        runtime = "colima"
    else:
        runtime = "other (not Docker Desktop, not colima)"
    inspected = json.loads(docker("image", "inspect", image).stdout)[0]
    mac = platform.mac_ver()[0]
    return {
        "host_os": f"macOS {mac}" if mac else platform.platform(),
        "host_arch": platform.machine(),
        "docker_client": version.get("Client", {}).get("Version"),
        "docker_server": version.get("Server", {}).get("Version"),
        "docker_runtime": runtime,
        "docker_context": context,
        "vm_os": operating,
        "vm_kernel": info.get("KernelVersion"),
        "vm_arch": info.get("Architecture"),
        "vm_cpus": info.get("NCPU"),
        "vm_memory_bytes": info.get("MemTotal"),
        "storage_driver": info.get("Driver"),
        "image": image,
        "image_id": inspected.get("Id"),
        "image_size_bytes": inspected.get("Size"),
        "base_image": base_digest(DOCKERFILE.read_text(encoding="utf-8")),
        "host_python": platform.python_version(),
    }


class Volume:
    """The state on a named volume, through the wrapper."""

    kind = "volume"

    def __init__(self, image, work):
        self.image, self.work = image, work
        self.name = f"{PREFIX}probe-{secrets.token_hex(4)}"
        self.env = {**os.environ, "SRE_HARNESS_IMAGE": image, "SRE_HARNESS_VOLUME": self.name}
        for name in ("SRE_HARNESS_RO_MOUNTS", "AI_HARNESS_AUTOMATED", "AI_HARNESS_ACTIVE"):
            self.env.pop(name, None)

    def create(self, host_root):
        self.wrapper(["volume", "create"], check=True)
        self.wrapper(["volume", "import", str(host_root)], check=True)

    def wrapper(self, argv, check=False, **options):
        result = subprocess.run(
            [str(WRAPPER), *argv],
            env=self.env,
            cwd=self.work,
            capture_output=True,
            text=True,
            **options,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:3])}: exit {result.returncode}: {result.stderr}")
        return result

    def harness(self, argv):
        return self.wrapper(argv)

    def state_mount(self):
        return ["-v", f"{self.name}:/state"]

    def user(self):
        return []  # the image's user, the owner of the volume

    def remove(self):
        docker("volume", "rm", "-f", self.name, check=False)


class Bind(Volume):
    """The state on a host directory bind-mounted at /state, with docker run directly."""

    kind = "bind"

    def __init__(self, image, work):
        super().__init__(image, work)
        self.state = work.parent / "bind-state"

    def create(self, host_root):
        self.state.mkdir(mode=0o700)
        # The host's index stays out: this state builds its own, as a new volume does.
        ignored = shutil.ignore_patterns("index.sqlite3*", "writer.lock")
        shutil.copytree(host_root, self.state / "sre-harness", ignore=ignored)

    def user(self):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]  # the owner of the host directory

    def harness(self, argv):
        return subprocess.run(
            [
                "docker",
                "run",
                *HARDENING,
                *self.user(),
                *self.state_mount(),
                "-v",
                f"{self.work}:/work:ro",
                "-w",
                "/work",
                self.image,
                *argv,
            ],
            capture_output=True,
            text=True,
        )

    def state_mount(self):
        return ["-v", f"{self.state}:/state"]

    def remove(self):
        shutil.rmtree(self.state, ignore_errors=True)


def generation(state):
    result = state.harness(["knowledge", "status"])
    if result.returncode != 0:
        raise RuntimeError(f"knowledge status: exit {result.returncode}: {result.stderr}")
    return json.loads(result.stdout)["generation"]


def update(state, name):
    started = time.monotonic()
    result = state.harness(["knowledge", "update", "--source", "file", "--input", name])
    return {
        "code": result.returncode,
        "locked": LOCKED in result.stderr,
        "started": started,
        "ended": time.monotonic(),
        "stderr": result.stderr.strip()[-300:],
    }


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path.name


# Measurements -----------------------------------------------------------------------------


def timed(argv, runs, **options):
    values = []
    for _ in range(runs):
        started = time.perf_counter()
        result = subprocess.run(argv, capture_output=True, **options)
        values.append(time.perf_counter() - started)
        if result.returncode != 0:
            raise RuntimeError(f"{argv[:4]} exited {result.returncode}: {result.stderr[-300:]}")
    return series(values)


def startup(state, image, host_env, runs):
    host = [sys.executable, "-I", str(RUNTIME / "sre_harness_main.py")]
    wrapper = [str(WRAPPER)]
    common = {"env": state.env, "cwd": state.work}
    probe = ["docker", "run", *HARDENING]
    return {
        "wrapper knowledge status": timed([*wrapper, "knowledge", "status"], runs, **common),
        "wrapper knowledge search": timed([*wrapper, *SEARCH], runs, **common),
        "host knowledge status": timed([*host, "knowledge", "status"], runs, env=host_env),
        "host knowledge search": timed([*host, *SEARCH], runs, env=host_env),
        "docker image inspect": timed(["docker", "image", "inspect", image], runs),
        "docker run true": timed([*probe, "--entrypoint", "true", image], runs),
        "docker run python -I -c pass": timed(
            [*probe, "--entrypoint", "python", image, "-I", "-c", "pass"], runs
        ),
        "host python -I -c pass": timed([sys.executable, "-I", "-c", "pass"], runs),
    }


def hold_lock(state, seconds):
    """Start a container that holds the writer lock; returns it once it holds the lock."""
    argv = [
        "docker",
        "run",
        *HARDENING,
        *state.user(),
        *state.state_mount(),
        "--entrypoint",
        "python",
        state.image,
        "-I",
        "-c",
        HOLDER,
        LOCK,
        str(seconds),
    ]
    holder = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = holder.stdout.readline().strip() if holder.stdout else ""
    if line != "held":
        holder.kill()
        raise RuntimeError(f"the lock holder did not take the lock: {holder.stderr.read()}")
    return holder


def exclusive(state, corpus, pairs, held):
    """`held` refusals while another container holds the lock, then `pairs` concurrent pairs."""
    work = state.work
    held_rows = []
    for number in range(held):
        name = write_json(work / f"held-{number}.json", variant(corpus, f"h{number}"))
        before = generation(state)
        holder = hold_lock(state, HOLD_SECONDS)
        try:
            refused = update(state, name)
            still_held = holder.poll() is None
        finally:
            holder.wait(timeout=HOLD_SECONDS + 30)
        released = time.monotonic()
        after = generation(state)
        held_rows.append(
            {
                "code": refused["code"],
                "locked": refused["locked"],
                "update_seconds": round(refused["ended"] - refused["started"], 3),
                "holder_still_held_at_end": still_held and refused["ended"] < released,
                "generation_unchanged": before == after,
            }
        )
        # Then it writes: the lock was the only thing in the way.
        held_rows[-1]["after_release_code"] = update(state, name)["code"]
    counts = {"overlapped": 0, "serial": 0, "anomaly": 0}
    rows, wrote = [], 0
    start = generation(state)
    for number in range(pairs):
        names = [
            write_json(work / f"pair-{number}-{side}.json", variant(corpus, f"p{number}{side}"))
            for side in "ab"
        ]
        results = [None, None]

        def run(index, name):
            results[index] = update(state, name)

        threads = [threading.Thread(target=run, args=pair) for pair in enumerate(names)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        first, second = results
        assert first is not None and second is not None
        kind = classify_pair(first, second)
        counts[kind] += 1
        wrote += [first["code"], second["code"]].count(0)
        overlap = min(first["ended"], second["ended"]) - max(first["started"], second["started"])
        rows.append(
            {
                "kind": kind,
                "codes": [first["code"], second["code"]],
                "process_overlap_seconds": round(overlap, 3),
                "stderr": [first["stderr"], second["stderr"]] if kind == "anomaly" else None,
            }
        )
    end = generation(state)
    return {
        "held": {
            "n": held,
            "refused_with_exit_1": sum(row["code"] == 1 and row["locked"] for row in held_rows),
            "generation_unchanged": sum(row["generation_unchanged"] for row in held_rows),
            "lock_still_held_when_refused": sum(
                row["holder_still_held_at_end"] for row in held_rows
            ),
            "wrote_after_release": sum(row["after_release_code"] == 0 for row in held_rows),
            "update_seconds": spread([row["update_seconds"] for row in held_rows]),
        },
        "concurrent": {
            "pairs": pairs,
            **counts,
            "updates_that_wrote": wrote,
            "generation_advance": end - start,
            "generation_matches": end - start == wrote,
            "process_overlap_seconds": spread([row["process_overlap_seconds"] for row in rows]),
            "anomalies": [row for row in rows if row["kind"] == "anomaly"],
        },
    }


def snapshot(state, corpus, writes, readers):
    work = state.work
    references, answers, failures = {}, [], []
    done = threading.Event()

    def read(number):
        while not done.is_set():
            result = state.harness(SNAPSHOT_SEARCH)
            if result.returncode != 0:
                failures.append(
                    {
                        "reader": number,
                        "code": result.returncode,
                        "locked": "database is locked" in result.stderr,
                        "stderr": result.stderr.strip()[-300:],
                    }
                )
                continue
            answers.append(answer_of(json.loads(result.stdout)))

    threads = [threading.Thread(target=read, args=(number,)) for number in range(readers)]
    for thread in threads:
        thread.start()
    write_failures = 0
    try:
        for number in range(writes):
            name = write_json(work / f"snapshot-{number}.json", variant(corpus, f"s{number}"))
            if update(state, name)["code"] != 0:
                write_failures += 1
                continue
            # The reference of this generation: read before the next update starts.
            result = state.harness(SNAPSHOT_SEARCH)
            if result.returncode == 0:
                found = answer_of(json.loads(result.stdout))
                references[found["generation"]] = found
    finally:
        done.set()
        for thread in threads:
            thread.join()
    # The readers' first answers may predate the first write of this test: their reference is
    # the state before it, taken now that nothing writes.
    wrong = mismatches(
        [answer for answer in answers if answer["generation"] in references], references
    )
    return {
        "writes": writes,
        "write_failures": write_failures,
        "readers": readers,
        "answers": len(answers),
        "answers_compared": len(answers) - sum(a["generation"] not in references for a in answers),
        "generations_seen": len({answer["generation"] for answer in answers}),
        "mismatches": len(wrong),
        "reader_failures": len(failures),
        "reader_failures_locked": sum(failure["locked"] for failure in failures),
        "failure_samples": failures[:3],
    }


def prepare(work):
    """Synthetic data: the 300-record scenario, its config and source texts in a host state
    root, and the host index (for the host-direct timings). Returns (host env, corpus)."""
    data = work / "data"
    data.mkdir()
    written = kc.write_scenario(data, 300)
    host_env = {
        "AI_HARNESS_HOME": str(work / "host-state"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    root = paths.state_root(host_env)
    kc.write_sources(root, kc.scenario(300))
    config = json.loads(Path(written["config"]).read_text(encoding="utf-8"))
    paths.write_private(root / "knowledge" / "config.json", records.canonical(config))
    corpus = json.loads(Path(written["corpus"]).read_text(encoding="utf-8"))
    host = subprocess.run(
        [sys.executable, "-I", str(RUNTIME / "sre_harness_main.py"), "knowledge", "update"]
        + ["--source", "file", "--input", str(data / "corpus.json")],
        env=host_env,
        capture_output=True,
        text=True,
    )
    if host.returncode != 0:
        raise RuntimeError(f"host update failed: {host.stderr}")
    return host_env, root, corpus


def smoke_checks(state):
    """status (fts5), update, search, fetch through the wrapper."""
    status = state.harness(["knowledge", "status"])
    updated = state.harness(["knowledge", "update", "--source", "file", "--input", "corpus.json"])
    found = state.harness(SEARCH)
    fetched = state.harness(["knowledge", "fetch", "--principal", "workspace-a", "--id", FETCH_ID])
    return {
        "status_fts5": status.returncode == 0 and json.loads(status.stdout)["fts5"] is True,
        "update": updated.returncode == 0,
        "search": found.returncode == 0 and bool(json.loads(found.stdout)["records"]),
        "fetch": fetched.returncode == 0 and json.loads(fetched.stdout)["status"] == "ok",
        "backup_restore": backup_restore(state),
    }


def backup_restore(state):
    """`volume backup` of the state, `volume restore` into a new volume: the same generation."""
    copy = Volume(state.image, state.work)
    try:
        saved = subprocess.run(
            [str(WRAPPER), "volume", "backup"], env=state.env, cwd=state.work, capture_output=True
        )
        restored = subprocess.run(
            [str(WRAPPER), "volume", "restore"],
            env=copy.env,
            cwd=state.work,
            input=saved.stdout,
            capture_output=True,
        )
        if saved.returncode != 0 or restored.returncode != 0:
            return False
        return generation(copy) == generation(state)
    finally:
        copy.remove()


def failed(result):
    """The smoke checks that failed, as short strings."""
    problems = [name for name, ok in result["basic"].items() if not ok]
    held = result["exclusive"]["held"]
    if held["refused_with_exit_1"] != held["n"] or held["generation_unchanged"] != held["n"]:
        problems.append("held lock: an update was not refused")
    concurrent = result["exclusive"]["concurrent"]
    if concurrent["anomaly"] or not concurrent["generation_matches"]:
        problems.append("concurrent updates")
    shot = result["snapshot"]
    if shot["mismatches"] or shot["reader_failures"] or shot["write_failures"]:
        problems.append("snapshot")
    return problems


# Report -----------------------------------------------------------------------------------


def markdown(result):
    env = result["environment"]
    lines = [
        "# container probe",
        "",
        f"- host: {env['host_os']} {env['host_arch']}, Python {env['host_python']}",
        f"- Docker: client {env['docker_client']}, server {env['docker_server']}, "
        f"{env['docker_runtime']} (context {env['docker_context']}), VM {env['vm_os']} "
        f"{env['vm_kernel']} {env['vm_arch']}, {env['vm_cpus']} CPU, storage {env['storage_driver']}",
        f"- image: {env['image']} {env['image_id']}, {env['image_size_bytes']} bytes",
        f"- base: {env['base_image']}",
        "",
    ]
    if "backup_restore_same_generation" in result:
        lines += [
            f"- volume backup, then restore into a new volume, same generation: "
            f"{result['backup_restore_same_generation']}",
            "",
        ]
    if result.get("startup"):
        lines += [
            "## startup (seconds per call)",
            "",
            "| what | cold | n | min | median | max |",
            "|---|---|---|---|---|---|",
        ]
        for name, row in result["startup"].items():
            warm = row["warm"] or {}
            lines.append(
                f"| {name} | {row['cold']} | {warm.get('n')} | {warm.get('min')} | "
                f"{warm.get('median')} | {warm.get('max')} |"
            )
        lines.append("")
    for kind in ("volume", "bind"):
        if kind not in result["exclusive_by_state"]:
            continue
        held = result["exclusive_by_state"][kind]["held"]
        pair = result["exclusive_by_state"][kind]["concurrent"]
        shot = result["snapshot_by_state"][kind]
        lines += [
            f"## {kind}: exclusive writer and read snapshots",
            "",
            "| test | result |",
            "|---|---|",
            f"| held lock: updates refused with exit 1 | {held['refused_with_exit_1']} / {held['n']} |",
            f"| held lock: generation unchanged | {held['generation_unchanged']} / {held['n']} |",
            f"| held lock: lock still held when refused | "
            f"{held['lock_still_held_when_refused']} / {held['n']} |",
            f"| held lock: wrote after release | {held['wrote_after_release']} / {held['n']} |",
            f"| concurrent pairs: overlapped / serial / anomaly | {pair['overlapped']} / "
            f"{pair['serial']} / {pair['anomaly']} (of {pair['pairs']}) |",
            f"| concurrent: generation advance = updates that wrote | {pair['generation_advance']} "
            f"= {pair['updates_that_wrote']}: {pair['generation_matches']} |",
            f"| snapshot: answers compared / mismatches | {shot['answers_compared']} / "
            f"{shot['mismatches']} |",
            f"| snapshot: generations seen by readers | {shot['generations_seen']} |",
            f"| snapshot: reader failures (database is locked) | {shot['reader_failures']} "
            f"({shot['reader_failures_locked']}) |",
            f"| snapshot: writer failures | {shot['write_failures']} |",
            "",
        ]
    return "\n".join(lines) + "\n"


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="where container_probe.json and .md go")
    parser.add_argument("--image", help="default SRE_HARNESS_IMAGE or sre-harness:<version>")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--held", type=int, default=5)
    parser.add_argument("--pairs", type=int, default=20)
    parser.add_argument("--writes", type=int, default=10)
    parser.add_argument("--readers", type=int, default=3)
    parser.add_argument("--bind-mount", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="small, no timings; exit 1 on failure")
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    args = parser.parse_args(argv)
    if args.smoke:
        args.runs, args.held, args.pairs, args.writes, args.readers = 0, 1, 2, 2, 2
    elif args.out is None:
        parser.error("--out is required (except with --smoke)")
    return args


def default_image():
    version = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
    return os.environ.get("SRE_HARNESS_IMAGE") or f"sre-harness:{version}"


def main(argv=None):
    args = parse(argv)
    image = args.image or default_image()
    if shutil.which("docker") is None:
        print("docker is not installed or not on PATH.", file=sys.stderr)
        return 2
    if docker("image", "inspect", image, check=False).returncode != 0:
        print(f"there is no image {image}; build it first.", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix=f".{PREFIX}probe-", dir=Path.home()))
    states = []
    try:
        host_env, host_root, corpus = prepare(work)
        result = {
            "environment": environment(image),
            "settings": vars(args) | {"out": str(args.out)},
        }
        volume = Volume(image, work / "data")
        states.append(volume)
        volume.create(host_root)
        if args.smoke:
            result["basic"] = smoke_checks(volume)
        else:
            volume.wrapper(["knowledge", "update", "--source", "file", "--input", "corpus.json"])
            result["startup"] = startup(volume, image, host_env, args.runs)
            result["backup_restore_same_generation"] = backup_restore(volume)
        kinds = [volume]
        if args.bind_mount:
            bind = Bind(image, work / "data")
            states.append(bind)
            bind.create(host_root)
            kinds.append(bind)
        result["exclusive_by_state"], result["snapshot_by_state"] = {}, {}
        for state in kinds:
            result["exclusive_by_state"][state.kind] = exclusive(
                state, corpus, args.pairs, args.held
            )
            result["snapshot_by_state"][state.kind] = snapshot(
                state, corpus, args.writes, args.readers
            )
        if args.smoke:
            result["exclusive"] = result["exclusive_by_state"]["volume"]
            result["snapshot"] = result["snapshot_by_state"]["volume"]
            problems = failed(result)
            result["failed"] = problems
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1 if problems else 0
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "container_probe.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (args.out / "container_probe.md").write_text(markdown(result), encoding="utf-8")
        print(markdown(result))
        return 0
    except Missing as missing:
        print(str(missing), file=sys.stderr)
        return 2
    finally:
        for state in states:
            state.remove()
        if args.keep:
            print(f"kept {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
