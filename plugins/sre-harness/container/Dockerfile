# The image of the container path (scripts/sre-harness-container). Build it with
# `scripts/sre-harness-container image build`; the build context is the plugin directory and
# .dockerignore there lets only the runtime files in.
#
# The base is pinned by tag and by the digest of its multi-platform index (linux/amd64 and
# linux/arm64 among them), as `docker manifest inspect python:3.13.1-slim-bookworm` shows it.
# Python 3.13.1 with SQLite 3.40.1, built with FTS5. git comes from the Debian bookworm
# archive; its version is not pinned (an exact Debian version leaves the archive when a
# security update replaces it, and the build would then fail), so a rebuild can bring a newer
# git 2.39 patch release.
FROM python:3.13.1-slim-bookworm@sha256:031ebf3cde9f3719d2db385233bcb18df5162038e9cda20e64e08f49f4b47a2f

ARG SRE_HARNESS_VERSION=unknown
LABEL org.opencontainers.image.title="sre-harness" \
      org.opencontainers.image.version="${SRE_HARNESS_VERSION}"

# git only: `knowledge fetch` reads `git:` evidence with `git cat-file` from the bare
# repositories under the state root. No gh, claude, opa, sqlite-vec or embedding model.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git --version

# jsonschema at the version of requirements.txt; its own dependencies at the versions of
# constraints.txt.
COPY runtime/requirements.txt container/constraints.txt /opt/sre-harness/
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
      -r /opt/sre-harness/requirements.txt -c /opt/sre-harness/constraints.txt

COPY runtime/sre_harness_main.py runtime/container_state.py /opt/sre-harness/runtime/
COPY runtime/schemas /opt/sre-harness/runtime/schemas
COPY runtime/sre_harness /opt/sre-harness/runtime/sre_harness

# The build stops here when this SQLite has no FTS5: the knowledge index needs it.
RUN python -I -c "import sys; sys.path.insert(0, '/opt/sre-harness/runtime'); \
from sre_harness.knowledge import store; \
sys.exit(0 if store.fts5_available() else 'no FTS5 in SQLite ' + store.sqlite_version())" \
 && python -I -m compileall -q /opt/sre-harness/runtime

# A fixed user, not root. /state is created 0700 and owned by it, so a new named volume
# mounted there takes this owner and mode when it is first mounted.
RUN groupadd --gid 10001 harness \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin harness \
 && install -d -o 10001 -g 10001 -m 0700 /state \
 && install -d -m 0755 /work

ENV AI_HARNESS_HOME=/state
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["python", "-I", "/opt/sre-harness/runtime/sre_harness_main.py"]
