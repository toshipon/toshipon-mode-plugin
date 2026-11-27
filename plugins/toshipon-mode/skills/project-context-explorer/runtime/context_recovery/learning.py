"""Learning mode's records. The session writes the questions; this checks that each one is
tied to entities and to evidence that reads back, and keeps the answers so that weak areas
are asked again first. Everything stays in the local state directory."""

from .base import Refusal, append_private, now, read_jsonl
from .claims import verify
from .untrusted import one_line

KINDS = ("recognition", "dependency", "rationale", "failure_scenario")
RESULTS = ("correct", "partial", "wrong")
WEAK_BELOW = 0.67


def check_question(question, index):
    if not isinstance(question, dict):
        return ["question must be an object"]
    name = question.get("id")
    if not isinstance(name, str) or not name.strip() or len(name) > 40:
        return [f"question id must be 1-40 characters: {name!r}"]
    problems = []
    if question.get("kind") not in KINDS:
        problems.append(f"{name}: kind must be one of {', '.join(KINDS)}")
    for key in ("prompt", "answer"):
        if not isinstance(question.get(key), str) or not question[key].strip():
            problems.append(f"{name}: {key} is empty")
    entities = question.get("entity_ids")
    if not isinstance(entities, list) or not entities:
        problems.append(f"{name}: entity_ids must name at least one entity")
    else:
        problems += [
            f"{name}: entity is not in the index: {e}"
            for e in entities
            if e not in index["entities"]
        ]
    if not isinstance(question.get("claims"), list) or not question["claims"]:
        problems.append(f"{name}: a question needs at least one claim with its evidence")
    return problems


def add(path, questions, index, resolve):
    """Store questions whose answers rest on evidence that reads back. All or none."""
    if not isinstance(questions, list):
        raise Refusal("questions must be a list")
    problems = [problem for question in questions for problem in check_question(question, index)]
    if problems:
        raise Refusal("questions do not follow the contract", problems)
    for question in questions:
        report = verify(question["claims"], resolve)
        problems += [
            f"{question['id']}: {p}" for result in report["results"] for p in result["problems"]
        ]
        if not any(claim["status"] != "unknown" for claim in question["claims"]):
            problems.append(
                f"{question['id']}: every claim is unknown; there is no evidence to learn from"
            )
    if problems:
        raise Refusal("a question's evidence does not read back", problems)
    known = {
        record["question"]["id"] for record in read_jsonl(path) if record["event"] == "question"
    }
    added = []
    for question in questions:
        if question["id"] in known:
            continue
        append_private(path, {"event": "question", "at": now(), "question": question})
        added.append(question["id"])
    return {"added": added, "skipped": sorted(q["id"] for q in questions if q["id"] not in added)}


def _fold(path):
    questions, answers = {}, {}
    for record in read_jsonl(path):
        if record["event"] == "question":
            questions[record["question"]["id"]] = record["question"]
        else:
            answers.setdefault(record["question_id"], []).append(record)
    return questions, answers


def record_answer(path, question_id, result, note=""):
    questions, _ = _fold(path)
    if question_id not in questions:
        raise Refusal(f"no such question: {question_id}")
    if result not in RESULTS:
        raise Refusal(f"result must be one of {', '.join(RESULTS)}")
    record = {
        "event": "answer",
        "at": now(),
        "question_id": question_id,
        "result": result,
        "note": one_line(note),
    }
    append_private(path, record)
    return record


def weak_areas(path):
    """Per entity and per kind: attempts, share answered correctly, and whether it is weak.
    Weak means the last answer was not correct, or fewer than two in three were."""
    questions, answers = _fold(path)

    def summary(key_of):
        groups = {}
        for name, question in questions.items():
            for key in key_of(question):
                group = groups.setdefault(key, {"attempts": 0, "correct": 0, "last_missed": False})
                for answer in answers.get(name, []):
                    group["attempts"] += 1
                    group["correct"] += answer["result"] == "correct"
                if answers.get(name) and answers[name][-1]["result"] != "correct":
                    group["last_missed"] = True
        return {
            key: {
                "attempts": g["attempts"],
                "accuracy": round(g["correct"] / g["attempts"], 2) if g["attempts"] else None,
                "weak": bool(g["attempts"])
                and (g["last_missed"] or g["correct"] / g["attempts"] < WEAK_BELOW),
            }
            for key, g in sorted(groups.items())
        }

    return {
        "entities": summary(lambda question: question["entity_ids"]),
        "kinds": summary(lambda question: [question["kind"]]),
        "questions": len(questions),
        "answered": sum(1 for name in questions if answers.get(name)),
    }


def next_question(path):
    """One question: never-asked first, then the one whose last answer was missed, oldest first.
    The answer and its evidence are held back until the user has answered."""
    questions, answers = _fold(path)
    unasked = [name for name in questions if not answers.get(name)]
    missed = sorted(
        (
            name
            for name in questions
            if answers.get(name) and answers[name][-1]["result"] != "correct"
        ),
        key=lambda name: answers[name][-1]["at"],
    )
    for name in [*unasked, *missed]:
        question = questions[name]
        return {key: question[key] for key in ("id", "kind", "entity_ids", "prompt")}
    return None


def reveal(path, question_id):
    questions, _ = _fold(path)
    if question_id not in questions:
        raise Refusal(f"no such question: {question_id}")
    return questions[question_id]
