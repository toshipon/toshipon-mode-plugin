"""Path globs as the declarative checks read them: `**` spans directories, `*` and `?` stay
within one path segment.

`path_matches` reads a glob in time proportional to its length times the path's: it follows
every way the glob can match at once (`_matches`) and never backtracks, so no glob makes it
slow. `glob_regex` is the same reading as a regular expression and is the reference the matcher
is tested against; nothing matches paths with it.

`**/` repeated means what one `**/` means, and is read as one. The knowledge index also refuses
a glob with more than MAX_DOUBLE_STARS `**`, or longer than MAX_GLOB_LENGTH (`problem`), where
it comes in. The review side reads its globs as before.
"""

import re

MAX_GLOB_LENGTH = 200
MAX_DOUBLE_STARS = 3
INVALID = "invalid_glob"


def collapsed(glob):
    """`glob` with each run of `**/` (and of `**`) read as one: the same paths match."""
    previous = None
    while previous != glob:
        previous = glob
        glob = glob.replace("**/**/", "**/").replace("****", "**")
    return glob


def problem(glob):
    """INVALID when `glob` is longer than MAX_GLOB_LENGTH or holds more than MAX_DOUBLE_STARS
    `**` once collapsed; None otherwise."""
    if len(glob) > MAX_GLOB_LENGTH or collapsed(glob).count("**") > MAX_DOUBLE_STARS:
        return INVALID
    return None


def _tokens(glob):
    """The glob as (kind, character): `deep/` for `**/`, `deep` for `**`, `star`, `one`, and
    `literal` with its character."""
    glob = collapsed(glob)
    out, index = [], 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append(("deep/", None))
            index += 3
        elif glob.startswith("**", index):
            out.append(("deep", None))
            index += 2
        elif glob[index] == "*":
            out.append(("star", None))
            index += 1
        elif glob[index] == "?":
            out.append(("one", None))
            index += 1
        else:
            out.append(("literal", glob[index]))
            index += 1
    return out


def glob_regex(glob):
    """`**` spans directories, `*` and `?` stay within one path segment."""
    parts = {"deep/": "(?:.*/)?", "deep": ".*", "star": "[^/]*", "one": "[^/]"}
    return re.compile(
        "".join(parts[kind] if kind in parts else re.escape(char) for kind, char in _tokens(glob))
    )


def _closure(tokens, states):
    """`states` with every position reachable without reading a character. A state is
    (token index, inside): `inside` marks being within a `**/` that has read something and so
    must still end at a `/`."""
    seen, todo = set(states), list(states)
    while todo:
        index, inside = todo.pop()
        if inside or index == len(tokens):
            continue
        if tokens[index][0] in {"deep/", "deep", "star"}:  # each may match nothing
            skipped = (index + 1, False)
            if skipped not in seen:
                seen.add(skipped)
                todo.append(skipped)
    return seen


def _matches(tokens, path):
    states = _closure(tokens, {(0, False)})
    for char in path:
        moved = set()
        for index, inside in states:
            if index == len(tokens):
                continue
            kind, literal = tokens[index]
            if kind == "deep/":
                moved.add((index, True))  # any character, more to come before the `/`
                if char == "/":
                    moved.add((index + 1, False))
            elif inside:
                continue
            elif kind == "deep":
                moved.add((index, False))
            elif kind == "star":
                if char != "/":
                    moved.add((index, False))
            elif kind == "one":
                if char != "/":
                    moved.add((index + 1, False))
            elif literal == char:
                moved.add((index + 1, False))
        states = _closure(tokens, moved)
        if not states:
            return False
    return (len(tokens), False) in states


def path_matches(path, globs):
    return any(_matches(_tokens(glob), path) for glob in globs)
