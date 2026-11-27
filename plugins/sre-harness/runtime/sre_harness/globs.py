"""Path globs as the declarative checks read them: `**` spans directories, `*` and `?` stay
within one path segment."""

import re


def glob_regex(glob):
    """`**` spans directories, `*` and `?` stay within one path segment."""
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
