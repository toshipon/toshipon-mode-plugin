"""`spec extract`: documents to the Decision Registry and the Requirement Registry.

Order of work:

1. Explicit entries by fixed rules, no model. Decisions come from ADR documents: front-matter
   (`id`, `title`, `status`, `supersedes`, `approvers`, `date`, `systems`, `components`,
   `environments`) or, without front-matter, `Status:` / `Supersedes:` / `Date:` lines and a
   `## Status` section. Requirements come from any document's sections headed `REQ-0001 ...`:
   the first paragraph is the statement; `Given / When / Then` lines (bulleted or not) and the
   bullets after an `Acceptance criteria:` / `受入条件:` line are the criteria; `Category:`,
   `Decisions:` and `Rationale:` lines fill the rest. These are `basis: explicit`.
   A requirement keeps its `REQ-` id. A decision whose document writes a `DEC-` id keeps it;
   the others get the next free `DEC-` number, and the id the document writes (an ADR number)
   is `external_id`. A document's references are resolved by those written ids only.
2. The explicit decisions go through `registry.build` before anything is sent, so a
   supersession cycle, or one id written by two documents, stops the stage (exit 2) without a
   model call. The message names the documents.
3. The documents go to the backend as DATA, whole documents per batch. It may propose
   decisions and requirements the documents imply, and ambiguity the rules missed. Each
   proposal is kept only if every evidence ref names a section that was sent and every quote
   is a substring of that section, and, for a decision, if its status is not `approved`
   (an inferred decision cannot be approved). Ambiguity is kept only if it names a known
   requirement and its span is in that requirement's text. A quote has at least
   `MIN_QUOTE` characters besides spaces. Kept proposals are `basis: inferred`,
   `confidence: low`, ambiguity `detector: llm`, and get ids of their own namespace (`DCI-`,
   `RQI-`) that no document writes, so a reference in a document never reaches one. An
   inferred decision's `supersedes` keeps only ids of explicit decisions; the rest (its own
   id, another inferred one, an unknown id) is dropped with an issue `supersedes_dropped`.
   The others go to `rejected-extractions.jsonl` with reasons.
4. Everything is built into the registries again and held to `registry.check` and the schemas.

Document text is external input: it travels as DATA and is never an instruction.
"""

import re

from .. import paths, records
from ..errors import InputError, QualityGateError
from . import ambiguity, registry
from .ingest_file import FRONT_MATTER_HEADING, text_lines

STAGE = "extract"
OUTPUT_SCHEMA = "llm-spec-extract"
MAX_BATCH_BYTES = 400 * 1024
MIN_QUOTE = 8  # characters besides spaces in a backend's quote
INFERRED_PREFIX = {"decision": "DCI", "requirement": "RQI"}
_DEC_ID = re.compile(r"DEC-[0-9]{4,}")

STATUS_WORDS = {
    "proposed": "proposed",
    "draft": "proposed",
    "提案": "proposed",
    "提案中": "proposed",
    "accepted": "approved",
    "approved": "approved",
    "承認": "approved",
    "承認済み": "approved",
    "rejected": "rejected",
    "declined": "rejected",
    "却下": "rejected",
    "superseded": "superseded",
    "置換済み": "superseded",
    "unresolved": "unresolved",
    "未決": "unresolved",
}
NFR_CATEGORIES = {
    "availability": "availability",
    "可用性": "availability",
    "performance": "performance",
    "性能": "performance",
    "security": "security",
    "セキュリティ": "security",
    "operability": "operability",
    "運用性": "operability",
    "cost": "cost",
    "コスト": "cost",
    "compliance": "compliance",
    "コンプライアンス": "compliance",
}
SECTION_NAMES = {
    "status": ("status", "状態", "ステータス"),
    "decision": ("decision", "決定", "決定事項"),
    "rationale": ("rationale", "context", "根拠", "背景", "理由"),
}

_REF = re.compile(r"(?<![A-Za-z0-9])(?:ADR|DEC)-[0-9]{1,6}(?![0-9])")
_ADR = re.compile(r"(?<![A-Za-z0-9])ADR-[0-9]{1,6}(?![0-9])")
_LINE = re.compile(
    r"[ \t]*(status|状態|ステータス|supersedes|置換|date|日付)[ \t]*[:：][ \t]*(\S.*?)[ \t]*",
    re.IGNORECASE,
)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_APPROVER = re.compile(r"(.+?)\s*[(（](.+?)[)）]")
_REQ = re.compile(r"(REQ-[0-9]{4,})(?![0-9])[\s:：-]*(.*)")
_FIELD = re.compile(
    r"(category|カテゴリ|decisions?|決定|rationale|根拠|理由)[ \t]*[:：][ \t]*(.*)", re.IGNORECASE
)
_CRITERIA = re.compile(r"(acceptance criteria|受入条件|受け入れ条件)[ \t]*[:：]?", re.IGNORECASE)
_BULLET = re.compile(r"[-*][ \t]+(.*)")
_GWT = re.compile(r"(given|when|then|and|前提|もし|ならば)[ \t]*[:：]?[ \t]+(.*)", re.IGNORECASE)
_GWT_PART = {"given": "given", "前提": "given", "when": "when", "もし": "when", "then": "then"}
_GWT_PART["ならば"] = "then"
_STUB_DECISION = re.compile(r"(?:決定|decision)[ \t]*[:：][ \t]*(\S.*)", re.IGNORECASE)
_STUB_REQUIREMENT = re.compile(r"(?:要求|requirement)[ \t]*[:：][ \t]*(\S.*)", re.IGNORECASE)
STUB_VAGUE = "TBD"

INSTRUCTIONS = """\
Task: read the specification documents in DATA and propose what they imply but do not state in
the fixed forms the harness already read. `known_decisions` and `known_requirements` were
extracted by fixed rules; do not repeat them.

- decisions: decisions a document records in prose (meeting minutes, threads). For each give
  key (D1, D2, ...), title, content, rationale, status (proposed, rejected or unresolved; the
  harness refuses approved for anything it did not read from an explicit record), supersedes
  (ids from known_decisions it replaces, if the text says so; nothing else), scope, and
  evidence.
- requirements: obligations a document states outside a `REQ-` section. Give key (R1, ...),
  statement, rationale, nfr_category or null, decision_refs (ids from known_decisions),
  acceptance_criteria (given, when, then; empty when the text gives none), and evidence.
- ambiguity: places in a requirement (a known id, or a key from your requirements) that are
  vague, unbounded, compound, lack a metric or lack criteria. span is copied from the
  requirement's text.

evidence is a list of {ref, quote}: ref is a section ref from DATA (DOC-0001#S2), quote is text
copied exactly from that section, at least 8 characters besides spaces. Proposals with a ref
that was not sent, or a quote that is shorter or not in that section, are dropped. Do not
judge approval, priority or correctness."""


# Explicit decisions -----------------------------------------------------------------------


def _ref(document, section):
    return f"{document['id']}#{section['id']}"


def _named(document, kind):
    names = SECTION_NAMES[kind]
    for section in document["sections"]:
        if section["heading"].strip().lower() in names:
            return section
    return None


def _list(value):
    if isinstance(value, list):
        return [item for item in value if item]
    return [value] if value else []


def _body_lines(document):
    """{key: (value, section, the line as written)} for Status/Supersedes/Date lines."""
    found = {}
    for section in document["sections"]:
        if section["heading"] == FRONT_MATTER_HEADING:
            continue
        for line in text_lines(section["text"]):
            match = _LINE.fullmatch(line)
            if match is None:
                continue
            key = match.group(1).lower()
            key = {"状態": "status", "ステータス": "status", "置換": "supersedes"}.get(key, key)
            key = "date" if key == "日付" else key
            found.setdefault(key, (match.group(2), section, line.strip()))
    return found


def _front_matter_line(document, key):
    section = document["sections"][0] if document["sections"] else None
    if section is None or section["heading"] != FRONT_MATTER_HEADING:
        return None, None
    for line in text_lines(section["text"]):
        if line.lower().startswith(f"{key}:"):
            return section, line.strip()
    return section, None


def _approvers(values):
    out = []
    for value in values:
        match = _APPROVER.fullmatch(value.strip())
        if match:
            out.append({"name": match.group(1).strip()[:100], "role": match.group(2).strip()[:100]})
    return out


def explicit_decision(document):
    """A decision draft from an ADR document, or None when it names nothing to decide."""
    fields = document.get("front_matter", {})
    lines = _body_lines(document)
    issues, evidence = [], []

    raw_status, status_section, status_line = None, None, None
    if isinstance(fields.get("status"), str) and fields["status"]:
        raw_status = fields["status"]
        status_section, status_line = _front_matter_line(document, "status")
    elif "status" in lines:
        raw_status, status_section, status_line = lines["status"]
    elif (section := _named(document, "status")) is not None and section["text"].strip():
        raw_status = text_lines(section["text"].strip())[0]
        status_section, status_line = section, raw_status.strip()
    word = raw_status.split()[0].lower() if raw_status else None
    status = STATUS_WORDS.get(word or "")
    if status is None:
        status = "unresolved"
        detail = f"status {raw_status!r} is not a known status" if raw_status else "no status"
        issues.append({"kind": "unknown_status", "detail": detail[:300]})
    if status_section is not None and status_line:
        evidence.append({"id": "E1", "ref": _ref(document, status_section), "quote": status_line})

    stated_id = fields.get("id") if isinstance(fields.get("id"), str) else None
    title_ids = _ADR.findall(document["title"])
    external = stated_id or (title_ids[0] if title_ids else None)
    title = document["title"]
    if external and title.startswith(external):
        title = title[len(external) :].lstrip(" :：-") or title

    supersedes = _list(fields.get("supersedes"))
    if not supersedes and "supersedes" in lines:
        supersedes = _REF.findall(lines["supersedes"][0])
    content_section = _named(document, "decision") or next(
        (s for s in document["sections"] if s["heading"] != FRONT_MATTER_HEADING), None
    )
    content = content_section["text"] if content_section else ""
    first = next((line.strip() for line in text_lines(content) if line.strip()), None)
    if content_section is not None and first:
        evidence.append(
            {"id": f"E{len(evidence) + 1}", "ref": _ref(document, content_section), "quote": first}
        )
    rationale_section = _named(document, "rationale")
    date = fields.get("date") if isinstance(fields.get("date"), str) else None
    if date is None and "date" in lines:
        date = lines["date"][0]
    decided_at = date[:10] if date and _DATE.match(date) else None
    draft = {
        "title": title[:300],
        "content": content,
        "rationale": rationale_section["text"] if rationale_section else "",
        "status": status,
        "approvers": _approvers(_list(fields.get("approvers"))),
        "decided_at": decided_at,
        "evidence": evidence,
        "scope": {
            name: [str(item)[:100] for item in _list(fields.get(name))]
            for name in ("systems", "components", "environments")
        },
        "supersedes": supersedes,
        "basis": "explicit",
        "confidence": "high",
        "issues": issues,
    }
    if external:
        draft["external_id"] = external[:64]
    return draft


# Explicit requirements --------------------------------------------------------------------


def _criterion(parts):
    then = parts.get("then", "").strip()
    if not then:
        return None
    item = {
        "given": parts.get("given", "").strip(),
        "when": parts.get("when", "").strip(),
        "then": then,
    }
    measured = None
    for key in ("then", "when", "given"):
        measured = measured or ambiguity.measure(item[key])
    item["measurable"] = measured is not None
    if measured:
        item["target"], item["unit"] = measured
    return item


def parse_requirement(text):
    """{statement, acceptance_criteria, rationale, nfr_category, refs} from a REQ section."""
    statement, criteria, rationale, category, refs = [], [], "", None, []
    mode, current, last = "statement", {}, None

    def close():
        nonlocal current
        made = _criterion(current) if current else None
        if made:
            criteria.append(made)
        current = {}

    for raw in text_lines(text):
        line = raw.strip()
        if not line:
            if mode == "statement" and statement:
                mode = "after"
            continue
        field = _FIELD.fullmatch(line)
        if field:
            key, value = field.group(1).lower(), field.group(2).strip()
            if key in ("category", "カテゴリ"):
                category = NFR_CATEGORIES.get(value.lower(), category)
            elif key in ("rationale", "根拠", "理由"):
                rationale = value
            else:
                refs += _REF.findall(value)
            mode = "after"
            continue
        if _CRITERIA.fullmatch(line):
            close()
            mode = "criteria"
            continue
        bullet = _BULLET.fullmatch(line)
        item = bullet.group(1).strip() if bullet else line
        gwt = _GWT.fullmatch(item)
        if gwt:
            word = gwt.group(1).lower()
            part = _GWT_PART.get(word, last)
            if part == "given" and current:
                close()
            if part is not None:
                joined = f"{current[part]} {gwt.group(2)}" if part in current else gwt.group(2)
                current[part] = joined.strip()
                last = part
            mode = "criteria"
            continue
        if bullet and mode == "criteria":
            close()
            current = {"then": item}
            close()
        elif mode == "statement":
            statement.append(line)
    close()
    for index, criterion in enumerate(criteria, start=1):
        criterion["id"] = f"AC{index}"
    return {
        "statement": "\n".join(statement),
        "acceptance_criteria": criteria,
        "rationale": rationale,
        "nfr_category": category,
        "refs": refs,
    }


def explicit_requirements(document):
    """(drafts, rejected) from every `REQ-...` section of a document."""
    drafts = []
    for section in document["sections"]:
        match = _REQ.fullmatch(section["heading"].strip())
        if match is None:
            continue
        parsed = parse_requirement(section["text"])
        draft = {
            "id": match.group(1),
            "external_id": match.group(1),
            "title": match.group(2).strip()[:300],
            "statement": parsed["statement"],
            "acceptance_criteria": parsed["acceptance_criteria"],
            "rationale": parsed["rationale"],
            "nfr_category": parsed["nfr_category"],
            "refs": parsed["refs"],
            "basis": "explicit",
            "confidence": "high",
            "evidence": [],
            "key": _ref(document, section),
        }
        if parsed["statement"]:
            draft["evidence"] = [
                {"id": "E1", "ref": _ref(document, section), "quote": parsed["statement"]}
            ]
        drafts.append(draft)
    return drafts


# DATA and the stub ------------------------------------------------------------------------


def _document_data(document):
    sections = [
        {"ref": _ref(document, s), "heading": s["heading"], "text": s["text"]}
        for s in document["sections"]
    ]
    return {
        "id": document["id"],
        "kind": document["kind"],
        "title": document["title"],
        "sections": sections,
    }


def batches(documents, known):
    """Whole documents per batch, each batch under MAX_BATCH_BYTES. Nothing is cut."""
    base = len(records.canonical(known).encode("utf-8"))
    out, current, size = [], None, 0
    for document in documents:
        item = _document_data(document)
        cost = len(records.canonical(item).encode("utf-8"))
        if base + cost > MAX_BATCH_BYTES:
            raise QualityGateError(
                f"{document['id']} is {cost} bytes, too large for one {MAX_BATCH_BYTES}-byte "
                "batch with the known entries; stopped instead of truncating it."
            )
        if current is None or base + size + cost > MAX_BATCH_BYTES:
            current, size = {**known, "documents": []}, 0
            out.append(current)
        current["documents"].append(item)
        size += cost
    return out


def stub_extract(data):
    """Fixed rules, no model: in minutes, a `決定:` / `Decision:` line is a proposed decision and
    a `要求:` / `Requirement:` line a requirement; a known requirement containing "TBD" gets a
    vague_term finding."""
    decisions, requirements, found = [], [], []
    for document in data["documents"]:
        if document["kind"] != "minutes":
            continue
        for section in document["sections"]:
            for line in text_lines(section["text"]):
                evidence = None
                decision = _STUB_DECISION.fullmatch(line.strip())
                wanted = _STUB_REQUIREMENT.fullmatch(line.strip())
                if decision:
                    text = decision.group(1)
                    evidence = [{"ref": section["ref"], "quote": text}]
                    decisions.append(
                        {
                            "key": f"D{len(decisions) + 1}",
                            "title": text[:60],
                            "content": text,
                            "rationale": "",
                            "status": "proposed",
                            "supersedes": [],
                            "scope": {"systems": [], "components": [], "environments": []},
                            "evidence": evidence,
                        }
                    )
                elif wanted:
                    text = wanted.group(1)
                    requirements.append(
                        {
                            "key": f"R{len(requirements) + 1}",
                            "statement": text,
                            "rationale": "",
                            "nfr_category": None,
                            "decision_refs": [],
                            "acceptance_criteria": [],
                            "evidence": [{"ref": section["ref"], "quote": text}],
                        }
                    )
    for requirement in data["known_requirements"]:
        if STUB_VAGUE in requirement["statement"]:
            found.append(
                {"requirement": requirement["id"], "kind": "vague_term", "span": STUB_VAGUE}
            )
    return {"decisions": decisions, "requirements": requirements, "ambiguity": found}


# Checks -----------------------------------------------------------------------------------


def _evidence_problems(evidence, sections, minimum=0):
    reasons = []
    for index, item in enumerate(evidence):
        text = sections.get(item["ref"])
        if text is None:
            reasons.append(f"unknown_section: evidence[{index}] names {item['ref']}, not sent")
        elif minimum and len("".join(item["quote"].split())) < minimum:
            reasons.append(
                f"quote_too_short: evidence[{index}] has fewer than {minimum} characters "
                "besides spaces"
            )
        elif not item["quote"].strip() or item["quote"] not in text:
            reasons.append(
                f"quote_not_found: evidence[{index}] is not a substring of {item['ref']}"
            )
    return reasons


def _numbered_evidence(evidence):
    return [
        {"id": f"E{i}", "ref": e["ref"], "quote": e["quote"]} for i, e in enumerate(evidence, 1)
    ]


class _Extraction:
    """Accumulates drafts, rejections and ids across explicit rules and backend answers."""

    def __init__(self, run_id, now, sections, reserved=()):
        self.run_id, self.now, self.sections = run_id, now, sections
        self.decisions, self.requirements, self.rejected = [], [], []
        self.llm_ambiguity = []  # (requirement id, entry)
        self.reserved = set(reserved)  # DEC- ids that documents write
        self.sources = {}  # decision id -> the path of its document

    def reject(self, key, reasons, call_id=None, draft=None):
        row = {"key": key, "reasons": reasons}
        if call_id:
            row["call_id"] = call_id
        if draft is not None:
            row["draft"] = draft
        self.rejected.append(row)

    def _decision_id(self, draft):
        if draft["basis"] == "inferred":
            return self._inferred_id("decision", self.decisions)
        external = draft.get("external_id") or ""
        if _DEC_ID.fullmatch(external):
            return external
        taken = self.reserved | {d["id"] for d in self.decisions}
        number = 1
        while records.record_id("DEC", number) in taken:
            number += 1
        return records.record_id("DEC", number)

    @staticmethod
    def _inferred_id(kind, made):
        count = sum(1 for item in made if item["basis"] == "inferred")
        return records.record_id(INFERRED_PREFIX[kind], count + 1)

    @staticmethod
    def _minimum(draft):
        return MIN_QUOTE if draft["basis"] == "inferred" else 0

    def add_decision(self, draft, key, call_id=None, source=None):
        external = draft.get("external_id")
        first = next(
            (d for d in self.decisions if external and d.get("external_id") == external), None
        )
        if first is not None:
            raise InputError(
                f"two decisions write the id {external}: {self.sources.get(first['id'])} and "
                f"{source}; fix the documents. Nothing was written."
            )
        reasons = _evidence_problems(draft["evidence"], self.sections, self._minimum(draft))
        if reasons:
            self.reject(key, reasons, call_id, draft)
            return None
        made = {
            "schema_version": records.SCHEMA_VERSION,
            "id": self._decision_id(draft),
            "run_id": self.run_id,
            "created_at": self.now,
            **draft,
        }
        self.decisions.append(made)
        if source:
            self.sources[made["id"]] = source
        return made

    def add_requirement(self, draft, call_id=None):
        key = draft.pop("key")
        reasons = []
        if not draft["statement"]:
            reasons.append("no_statement: the section has no statement before its criteria")
        reasons += _evidence_problems(draft["evidence"], self.sections, self._minimum(draft))
        if any(r["id"] == draft.get("id") for r in self.requirements):
            reasons.insert(0, f"duplicate_id: {draft['id']} is defined by an earlier section")
        if reasons:
            self.reject(key, reasons, call_id, draft)
            return None
        draft.setdefault("id", self._inferred_id("requirement", self.requirements))
        made = {
            "schema_version": records.SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.now,
            **draft,
        }
        self.requirements.append(made)
        return made

    def _explicit_supersedes(self, refs):
        """(registry ids of explicit decisions, issues for the refs dropped). The backend was
        shown both the registry id and the written id of each explicit decision."""
        known = {}
        for item in self.decisions:
            if item["basis"] == "explicit":
                known[item["id"]] = item["id"]
                if item.get("external_id"):
                    known.setdefault(item["external_id"], item["id"])
        kept, issues = [], []
        for ref in refs:
            target = known.get(ref)
            if target is None:
                detail = f"{ref}: not an explicit decision; an inferred one supersedes only those"
                issues.append({"kind": "supersedes_dropped", "detail": detail[:300]})
            elif target not in kept:
                kept.append(target)
        return kept, issues

    def answer(self, output, call_id, backend):
        """Hold one backend answer to the evidence, status and reference checks."""
        generated = {"backend": backend, "call_id": call_id}
        for draft in output["decisions"]:
            key = draft["key"]
            reasons = []
            if draft["status"] == "approved":
                reasons.append("inferred_cannot_be_approved: only an explicit record is approved")
            reasons += _evidence_problems(draft["evidence"], self.sections, MIN_QUOTE)
            if reasons:
                self.reject(key, reasons, call_id, draft)
                continue
            supersedes, issues = self._explicit_supersedes(draft["supersedes"])
            self.add_decision(
                {
                    "title": draft["title"],
                    "content": draft["content"],
                    "rationale": draft["rationale"],
                    "status": draft["status"],
                    "approvers": [],
                    "decided_at": None,
                    "evidence": _numbered_evidence(draft["evidence"]),
                    "scope": draft["scope"],
                    "supersedes": supersedes,
                    "basis": "inferred",
                    "confidence": "low",
                    "issues": issues,
                    "generated_by": generated,
                },
                key,
                call_id,
            )
        keys = {}
        for draft in output["requirements"]:
            criteria = [_criterion(item) for item in draft["acceptance_criteria"]]
            criteria = [item for item in criteria if item]
            for index, criterion in enumerate(criteria, start=1):
                criterion["id"] = f"AC{index}"
            made = self.add_requirement(
                {
                    "key": draft["key"],
                    "statement": draft["statement"],
                    "acceptance_criteria": criteria,
                    "rationale": draft["rationale"],
                    "nfr_category": draft["nfr_category"],
                    "refs": draft["decision_refs"],
                    "basis": "inferred",
                    "confidence": "low",
                    "evidence": _numbered_evidence(draft["evidence"]),
                    "generated_by": generated,
                },
                call_id,
            )
            if made is not None:
                keys[draft["key"]] = made["id"]
        by_id = {item["id"]: item for item in self.requirements}
        for index, entry in enumerate(output["ambiguity"]):
            name = keys.get(entry["requirement"], entry["requirement"])
            target = by_id.get(name)
            key = f"ambiguity[{index}]"
            if target is None:
                reason = f"unknown_requirement: {entry['requirement']} is not a kept requirement"
                self.reject(key, [reason], call_id, entry)
                continue
            field = next((f for f, text in ambiguity.fields(target) if entry["span"] in text), None)
            if field is None:
                reason = f"span_not_found: the span is not in {name}'s text"
                self.reject(key, [reason], call_id, entry)
                continue
            found = {"kind": entry["kind"], "span": entry["span"], "field": field}
            self.llm_ambiguity.append((name, {**found, "detector": "llm"}))


def _read_documents(run_dir):
    documents = records.read_jsonl(paths.child(run_dir, "documents.jsonl"))
    for document in documents:
        problems = records.violations("spec-document", document)
        if problems:
            raise InputError(
                f"documents.jsonl has an invalid record {document.get('id')!r}: {problems[0]}"
            )
    ingest = paths.child(run_dir, "ingest.json")
    missing = records.read_json(ingest).get("input_missing", []) if ingest.is_file() else []
    return documents, missing


def _decision_drafts(extraction):
    return [{k: v for k, v in d.items() if k != "superseded_by"} for d in extraction.decisions]


def _finish_requirements(extraction, decisions):
    """A document's `Decisions:` line reaches an explicit decision by its written id; the
    backend's `decision_refs` may also use the registry ids it was shown."""
    written, assigned = {}, {}
    for item in decisions:
        if item["basis"] != "explicit":
            continue
        assigned[item["id"]] = item["id"]
        if item.get("external_id"):
            written[item["external_id"]] = item["id"]
    missing, out = [], []
    extra = {}
    for name, entry in extraction.llm_ambiguity:
        extra.setdefault(name, []).append(entry)
    for item in extraction.requirements:
        refs = []
        index = written if item["basis"] == "explicit" else {**assigned, **written}
        for ref in item.pop("refs"):
            target = index.get(ref)
            if target is None:
                missing.append({"kind": "decision_ref", "from": item["id"], "ref": ref})
            elif target not in refs:
                refs.append(target)
        item["decision_refs"] = refs
        found = ambiguity.detect(item)
        seen = {(entry["kind"], entry.get("span")) for entry in found}
        for entry in extra.get(item["id"], []):
            if (entry["kind"], entry["span"]) not in seen:
                found.append(entry)
                seen.add((entry["kind"], entry["span"]))
        item["ambiguity"] = found
        out.append(item)
    return out, missing


def extract(run_dir, session):
    """Run the stage. Returns the summary. Raises InputError on a supersession cycle."""
    run_id, now = run_dir.name, records.iso()
    documents, ingest_missing = _read_documents(run_dir)
    sections = {_ref(d, s): s["text"] for d in documents for s in d["sections"]}
    adrs = [(d, explicit_decision(d)) for d in documents if d["kind"] == "adr"]
    reserved = {
        draft["external_id"]
        for _, draft in adrs
        if _DEC_ID.fullmatch(draft.get("external_id") or "")
    }
    extraction = _Extraction(run_id, now, sections, reserved)
    for document, draft in adrs:
        extraction.add_decision(draft, document["id"], source=document["provenance"]["locator"])
    for document in documents:
        for draft in explicit_requirements(document):
            extraction.add_requirement(draft)
    # a cycle or a written id claimed twice stops here, before any call
    registry.build(_decision_drafts(extraction), extraction.sources)

    known = {
        "known_decisions": [
            {k: d[k] for k in ("id", "external_id", "title", "status") if k in d}
            for d in extraction.decisions
        ],
        "known_requirements": [
            {"id": r["id"], "statement": r["statement"]} for r in extraction.requirements
        ],
    }
    calls = []
    for data in batches(documents, known) if documents else []:
        output = session.ask(STAGE, INSTRUCTIONS, data, OUTPUT_SCHEMA, stub_extract)
        calls.append(session.last_call_id)
        batch_sections = {s["ref"] for doc in data["documents"] for s in doc["sections"]}
        extraction.sections = {ref: sections[ref] for ref in batch_sections}
        extraction.answer(output, session.last_call_id, session.backend)
    extraction.sections = sections

    built = registry.build(_decision_drafts(extraction), extraction.sources)
    requirements, requirement_missing = _finish_requirements(extraction, built["decisions"])
    problems = registry.check(built["decisions"])
    for item in built["decisions"]:
        problems += [f"{item['id']}: {p}" for p in records.violations("decision", item)[:3]]
    for item in requirements:
        problems += [f"{item['id']}: {p}" for p in records.violations("requirement", item)[:3]]
    if problems:
        raise QualityGateError("the registries break their invariants: " + "; ".join(problems[:5]))

    head = {"schema_version": records.SCHEMA_VERSION, "run_id": run_id, "created_at": now}
    decisions_file = {**head, **built}
    requirements_file = {**head, "requirements": requirements, "input_missing": requirement_missing}
    records.validate("decision", decisions_file, "registry")
    records.validate("requirement", requirements_file, "registry")
    records.write_json(paths.child(run_dir, "decisions.json"), decisions_file)
    records.write_json(paths.child(run_dir, "requirements.json"), requirements_file)
    records.write_jsonl(
        paths.child(run_dir, "rejected-extractions.jsonl"),
        [{**head, **row} for row in extraction.rejected],
    )
    counts = {"rule": 0, "llm": 0}
    for item in requirements:
        for entry in item["ambiguity"]:
            counts[entry["detector"]] += 1
    missing = ingest_missing + built["input_missing"] + requirement_missing
    summary = {
        "run_id": run_id,
        "backend": session.backend,
        "documents": len(documents),
        "decisions": len(built["decisions"]),
        "active": built["active"],
        "requirements": len(requirements),
        "ambiguity": counts,
        "rejected": len(extraction.rejected),
        "input_missing": len(missing),
    }
    records.write_json(
        paths.child(run_dir, "extract.json"),
        {**head, **summary, "calls": calls, "input_missing": missing},
    )
    return summary
