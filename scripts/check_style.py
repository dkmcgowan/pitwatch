#!/usr/bin/env python3
"""Check tracked files against the house prose rules.

Two rules, both easy to break by accident:

1. American spellings, in prose and in user facing strings as well as in code.
2. No typographic punctuation. Em dashes, smart quotes and ellipsis characters
   do not survive every terminal, every email client or every copy and paste,
   and they are not needed. Commas, colons and semicolons are.

Run with no arguments to check every tracked file, or pass paths to check
those instead.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BANNED_CHARACTERS = {
    "—": "em dash",
    "–": "en dash",
    "‘": "left single quote",
    "’": "right single quote",
    "“": "left double quote",
    "”": "right double quote",
    "…": "ellipsis",
    " ": "non breaking space",
}

BRITISH_SPELLINGS = re.compile(
    r"\b("
    r"programme|licence|behaviour|colour|organis(?:e|ed|es|ing|ation)|"
    r"realis(?:e|ed|es|ing)|recognis(?:e|ed|es|ing)|neighbour|dialling|"
    r"honour|analys(?:e|ed|es|ing)|summaris(?:e|ed|es|ing)|"
    r"utilis(?:e|ed|es|ing)|whilst|amongst|initialis(?:e|ed|es|ing)|"
    r"serialis(?:e|ed|es|ing)|optimis(?:e|ed|es|ing)|normalis(?:e|ed|es|ing)|"
    r"centre|metre|defence|grey"
    r")\b",
    re.IGNORECASE,
)

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}

# This file has to contain every character and every word it is looking for, so
# it is the one file that cannot be checked by it.
SELF = Path(__file__).resolve()


def tracked_files() -> list[Path]:
    listing = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [Path(name) for name in listing.splitlines() if name]


def check(path: Path) -> list[str]:
    if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
        return []
    if path.resolve() == SELF:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{path}: not valid UTF-8"]

    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        for character, name in BANNED_CHARACTERS.items():
            if character in line:
                problems.append(f"{path}:{number}: {name}: {line.strip()}")
        match = BRITISH_SPELLINGS.search(line)
        if match:
            problems.append(f"{path}:{number}: British spelling {match.group(0)!r}: {line.strip()}")
    return problems


def main(argv: list[str]) -> int:
    paths = [Path(name) for name in argv[1:]] or tracked_files()
    problems = [problem for path in paths for problem in check(path)]
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s). See scripts/check_style.py for the rules.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
