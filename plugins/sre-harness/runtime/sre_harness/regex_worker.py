"""Match regular expressions for `guard.regex_search`, in a process of their own.

guard starts this file as `sys.executable -I regex_worker.py` with a time limit and kills it
when the limit passes, so a pattern that backtracks without end stops only this process. Input
on stdin: {"patterns": [...], "texts": [...]}. Output on stdout: {"matches": [[text index, ...]
per pattern]}, or {"invalid": <pattern index>} for a pattern that does not compile.

It imports only the standard library and reads nothing but stdin.
"""

import json
import re
import sys


def search(request):
    compiled = []
    for index, pattern in enumerate(request["patterns"]):
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            return {"invalid": index}
    texts = request["texts"]
    return {"matches": [[i for i, text in enumerate(texts) if p.search(text)] for p in compiled]}


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    json.dump(search(json.loads(stdin.read())), stdout, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
