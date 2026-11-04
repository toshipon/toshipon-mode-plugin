"""Read-only source collection and versioned, verifiable context snapshots."""

import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

from .evaluator import finite_numbers, schema

SOURCE_FIELDS = ("id", "kind", "locator", "title", "retrieved", "note")
QUESTION_FIELDS = (
    "id",
    "question",
    "blocks",
    "owner",
    "status",
    "asked_at",
    "answer",
    "evidence",
    "incorporated_in",
    "updated",
)

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
AWS_OPERATIONS = {"rds": "describe-db-instances", "ec2": "describe-instances"}


class IntakeError(ValueError):
    """The source contract cannot be collected or verified safely."""


def _json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise IntakeError("JSON contains a duplicate property.")
            result[key] = value
        return result

    def constant(_value):
        raise IntakeError("JSON contains a non-finite numeric value.")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise IntakeError("Invalid UTF-8 JSON input.") from error
    if not finite_numbers(result):
        raise IntakeError("JSON contains a non-finite numeric value.")
    return result


def _validate(name, value):
    if not finite_numbers(value):
        raise IntakeError("Input contains non-finite numeric values.")
    validator = Draft202012Validator(schema(name), format_checker=FormatChecker())
    if not validator.is_valid(value):
        # Do not reproduce source contents or credentials in diagnostics.
        raise IntakeError(f"Invalid {name} schema.")
    ids = [source["id"] for source in value["sources"]]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"S[0-9]+", sid) for sid in ids):
        raise IntakeError("Source IDs must be unique S-number identifiers.")
    for source in value["sources"]:
        if "retrieved_at" in source:
            try:
                stamp = datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00"))
            except ValueError:
                raise IntakeError("Invalid source retrieval timestamp.") from None
            if stamp.tzinfo is None:
                raise IntakeError("Source retrieval timestamps need a timezone.")
        for key in ("title", "locator", "owner", "version"):
            if key in source and (not source[key].strip() or re.search(r"[\r\n\t]", source[key])):
                raise IntakeError("Source metadata must be nonempty single-line text.")


def _bounded_read(path, maximum=MAX_SOURCE_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise IntakeError("Source must be a regular, non-symlink file.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError:
        raise IntakeError("Unable to read source file.") from None
    if len(raw) > maximum:
        raise IntakeError("Source exceeds the configured size limit.")
    return raw


def _decode(raw):
    if len(raw) > MAX_SOURCE_BYTES:
        raise IntakeError("Source exceeds the configured size limit.")
    try:
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise IntakeError("Source must be UTF-8 text.") from None


class _StorageText(HTMLParser):
    """Keep source wording and paragraph boundaries without running HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IntakeError("Confluence redirects are not permitted.")


def _read_confluence(source):
    site = source["site"]
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.atlassian\.net", site):
        raise IntakeError("Confluence site must be an atlassian.net tenant hostname.")
    page_id = source["page_id"]
    if not re.fullmatch(r"[0-9]+", page_id):
        raise IntakeError("Confluence page ID must contain digits only.")
    try:
        locator = urlparse(source["locator"])
        valid = (
            locator.scheme == "https"
            and locator.hostname == site
            and not locator.username
            and not locator.password
            and locator.port in {None, 443}
            and re.search(rf"/pages/{re.escape(page_id)}(?:/|$)", locator.path) is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise IntakeError("Confluence locator must use the configured tenant HTTPS host.")
    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not email or not token or "\n" in email or "\n" in token:
        raise IntakeError("Confluence credentials are not configured.")
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    request = urllib.request.Request(
        f"https://{site}/wiki/api/v2/pages/{page_id}?body-format=storage",
        headers={"Accept": "application/json", "Authorization": f"Basic {encoded}"},
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=30) as response:
            raw = response.read(MAX_SOURCE_BYTES + 1)
        body = _json(_decode(raw))
        if str(body["id"]) != page_id:
            raise IntakeError("Confluence response page ID does not match the request.")
        version = body["version"]["number"]
        storage = body["body"]["storage"]["value"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise IntakeError("Confluence response lacks a valid version.")
        if not isinstance(storage, str):
            raise IntakeError("Confluence response lacks a storage body.")
        parser = _StorageText()
        parser.feed(storage)
        content = "".join(parser.parts).strip() + "\n"
        if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise IntakeError("Confluence text exceeds the configured size limit.")
        return content, str(version)
    except IntakeError:
        raise
    except (OSError, urllib.error.URLError, KeyError, TypeError, ValueError):
        raise IntakeError("Confluence read failed or returned an invalid page.") from None


def _canonical_resource(value):
    """AWS describe APIs do not guarantee resource, tag or nested list ordering."""
    if isinstance(value, dict):
        return {key: _canonical_resource(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted(
            (_canonical_resource(item) for item in value),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    return value


def _read_aws(source, region):
    operation = AWS_OPERATIONS.get(source["service"])
    if operation is None or not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+", region):
        raise IntakeError("Unsupported AWS read operation or region.")
    command = [
        "aws",
        source["service"],
        operation,
        "--region",
        region,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    try:
        environment = {
            **os.environ,
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "AWS_CLI_AUTO_PROMPT": "off",
        }
        result = subprocess.run(
            command, capture_output=True, timeout=60, check=False, env=environment
        )
    except (OSError, subprocess.TimeoutExpired):
        raise IntakeError("AWS read command failed or timed out.") from None
    if result.returncode:
        raise IntakeError("AWS read command failed.")
    raw = result.stdout.encode("utf-8") if isinstance(result.stdout, str) else result.stdout
    body = _json(_decode(raw))
    if not isinstance(body, dict):
        raise IntakeError("AWS read command did not return a JSON object.")
    content = (
        json.dumps(_canonical_resource(body), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise IntakeError("AWS snapshot exceeds the configured size limit.")
    return content


def _output_path(out):
    raw = Path(out).absolute()
    if raw.is_symlink():
        raise IntakeError("Output must not be a symlink.")
    if raw.exists() and (not raw.is_dir() or any(raw.iterdir())):
        raise IntakeError("Output must be a new or empty directory.")
    return raw


def _write_text(path, value):
    with Path(path).open("x", encoding="utf-8", newline="") as stream:
        stream.write(value)


def _write_json(path, value):
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def collect(manifest_path, out):
    """Collect explicit sources read-only; create a new Doc workspace snapshot."""
    manifest_path = Path(manifest_path).absolute()
    manifest = _json(_bounded_read(manifest_path, MAX_MANIFEST_BYTES))
    _validate("source-manifest", manifest)
    out = _output_path(out)
    collected = []
    contents = {}
    # Finish all external reads before creating output so a failed adapter leaves no partial view.
    for source in manifest["sources"]:
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        adapter = source["adapter"]
        if adapter == "file":
            source_path = manifest_path.parent / source["path"]
            raw = _bounded_read(source_path)
            content = _decode(raw)
            if source_path.suffix.lower() == ".json":
                _json(content)
            version = source["version"]
        elif adapter == "confluence":
            content, version = _read_confluence(source)
        else:
            content = _read_aws(source, manifest["region"])
            version = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not content.strip():
            raise IntakeError("Source must not be empty.")
        snapshot_path = f"sources/{source['id']}.txt"
        record = {key: source[key] for key in ("id", "kind", "title", "locator", "owner")}
        record.update(
            version=version,
            retrieved_at=retrieved_at,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            snapshot_path=snapshot_path,
        )
        collected.append(record)
        contents[snapshot_path] = content
    context = {
        "schema_version": 1,
        **{key: manifest[key] for key in ("project", "region", "budget_monthly_usd")},
        "sources": collected,
    }
    _validate("context", context)
    # Check again after potentially slow network reads; never overwrite a now-populated directory.
    out = _output_path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "sources").mkdir()
    for name, content in contents.items():
        _write_text(out / name, content)
    with (out / "sources.tsv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(SOURCE_FIELDS)
        for source in collected:
            kind = source["kind"] if source["kind"] in {"meeting", "confluence"} else "other"
            writer.writerow(
                [
                    source["id"],
                    kind,
                    source["locator"],
                    source["title"],
                    source["retrieved_at"][:10],
                    f"version={source['version']}",
                ]
            )
    _write_text(out / "facts.md", "# 前提状況\n\n出典を取り込みました。抽出内容は未承認です。\n")
    _write_text(out / "questions.tsv", "\t".join(QUESTION_FIELDS) + "\n")
    _write_text(out / "decisions.tsv", "timestamp\tphase\tdecision\trationale\tevidence\n")
    _write_text(out / ".gitignore", "sources/\nclaims.json\n")
    _write_json(out / "context.json", context)
    return context


def load_context(workspace):
    """Load metadata and verify every snapshot before it becomes model context."""
    workspace = Path(workspace).resolve()
    context = _json(_bounded_read(workspace / "context.json", MAX_MANIFEST_BYTES))
    _validate("context", context)
    sources_dir = workspace / "sources"
    if sources_dir.is_symlink() or not sources_dir.is_dir():
        raise IntakeError("Context sources directory must be a regular directory.")
    for source in context["sources"]:
        if source["snapshot_path"] != f"sources/{source['id']}.txt":
            raise IntakeError("Context snapshot path does not match its source ID.")
        raw = _bounded_read(workspace / source["snapshot_path"])
        _decode(raw)
        if hashlib.sha256(raw).hexdigest() != source["content_sha256"]:
            raise IntakeError("Context snapshot content changed after collection.")
    return context


def snapshot_hashes(workspace, context=None):
    """Hash the same metadata and source bytes that were actually verified."""
    workspace = Path(workspace)
    raw = _bounded_read(workspace / "context.json", MAX_MANIFEST_BYTES)
    current = _json(raw)
    _validate("context", current)
    if context is not None and current != context:
        raise IntakeError("Source context changed while being read.")
    sources_dir = workspace / "sources"
    if sources_dir.is_symlink() or not sources_dir.is_dir():
        raise IntakeError("Context sources directory must be a regular directory.")
    hashes = {"context.json": hashlib.sha256(raw).hexdigest()}
    for source in current["sources"]:
        if source["snapshot_path"] != f"sources/{source['id']}.txt":
            raise IntakeError("Context snapshot path does not match its source ID.")
        body = _bounded_read(workspace / source["snapshot_path"])
        value = hashlib.sha256(body).hexdigest()
        if value != source["content_sha256"]:
            raise IntakeError("Context snapshot content changed after collection.")
        hashes[source["snapshot_path"]] = value
    if _bounded_read(workspace / "context.json", MAX_MANIFEST_BYTES) != raw:
        raise IntakeError("Source context changed while being read.")
    return hashes
