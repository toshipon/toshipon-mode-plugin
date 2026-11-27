"""Path globs as the declarative checks read them: `**` spans directories, `*` and `?` stay
within one path segment.

`**/` repeated means what one `**/` means, and is read as one, so that it adds no backtracking.
Several `**` apart still backtrack with the number of path segments: the knowledge index refuses
a glob with more than MAX_DOUBLE_STARS of them, or longer than MAX_GLOB_LENGTH (`problem`), where
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


def glob_regex(glob):
    """`**` spans directories, `*` and `?` stay within one path segment."""
    glob = collapsed(glob)
    out, index = [], 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(out))


def path_matches(path, globs):
    return any(glob_regex(glob).fullmatch(path) for glob in globs)
