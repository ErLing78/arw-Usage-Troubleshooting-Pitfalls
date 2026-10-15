"""Guard against publishing machine-specific details.

This repository is built entirely from observations made on real machines, and
its documentation quotes real command output. That workflow is exactly the one
that leaks things: a pasted recycle bin listing carries real entry IDs and
timestamps, a pasted path carries the account name and host name.

A first pass of this check found the account name, the host name, the tool
directory from another project, and six genuine recycle bin entries in the
docs. The scan below is what makes sure it cannot happen again.

Run it like any other test::

    python tests/test_privacy.py

Exit code 0 means clean. Findings are printed with file and line number.

The scan is deliberately conservative in one direction only: it prefers false
positives over false negatives. If a legitimate line trips it, extend
ALLOWED_LINES with a comment explaining why, rather than loosening a pattern.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Files worth scanning: source, docs, config, and extension-less files that
# still hold text (LICENSE, COPYING, ...).
TEXT_EXT = {".py", ".md", ".ps1", ".yml", ".yaml", ".toml", ".txt", ".json", ".cfg", ".ini"}
TEXT_NAMES = {"LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS"}

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist"}

# ---------------------------------------------------------------------------
# Patterns that must not appear. Grouped so the report says what went wrong.
# ---------------------------------------------------------------------------
FORBIDDEN = [
    # Credentials. Nothing in a documentation repository needs these.
    ("credential", "password assignment", re.compile(r"pass(?:word|wd)\s*[:=]\s*\S", re.I)),
    ("credential", "secret assignment", re.compile(r"secret\s*[:=]\s*\S", re.I)),
    ("credential", "api key", re.compile(r"(?:api[_-]?key|apikey)\s*[:=]\s*\S", re.I)),
    ("credential", "private key block", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("credential", "github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("credential", "openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("credential", "aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("credential", "slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("credential", "bearer token", re.compile(r"Authorization\s*:\s*Bearer\s+\S+", re.I)),

    # Personal / machine identity.
    ("identity", "windows account SID", re.compile(r"\bS-1-5-\d{2}(?:-\d+){2,}\b")),
    ("identity", "email address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("identity", "8.3 short user name", re.compile(r"\b[A-Z]{3,}~\d\.[A-Z]{0,3}\b")),
    ("identity", "host name pattern", re.compile(r"\bLAPTOP-[A-Z0-9]{6,}\b", re.I)),

    # Machine-specific paths. System locations are fine; user profiles are not.
    ("path", "user profile path", re.compile(r"C:\\Users\\(?!<|you\b)[A-Za-z0-9._~-]{2,}")),

    # Quoted real output. Illustrative listings use <同一时刻> or <same moment>
    # and synthetic IDs, so a real timestamp means something was pasted raw.
    ("output", "real timestamp", re.compile(r"\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\b")),
]

# ---------------------------------------------------------------------------
# Release blockers. Unlike the checks above, these are never exemptible: a file
# that still says "YOUR NAME" is not a privacy leak, but it is not ready to
# publish either. Checked before the exemption list, so a placeholder cannot
# hide behind a line that also matches an allowed pattern.
#
# The patterns are assembled from fragments on purpose. Writing them literally
# would mean this file trips its own check, and a scanner that has to exempt
# itself is one you cannot trust.
# ---------------------------------------------------------------------------
_UNFILLED_NAME = "<" + "YOUR" + " " + "NAME" + ">"
_UNFILLED_GITHUB_USER = "github" + "." + "com/" + "USER" + "NAME"

PUBLISH_BLOCKERS = [
    ("publish", "unfilled author name", re.compile(re.escape(_UNFILLED_NAME))),
    ("publish", "unfilled GitHub user in URL", re.compile(re.escape(_UNFILLED_GITHUB_USER))),
]

# ---------------------------------------------------------------------------
# Exemptions. Each one needs a reason.
# ---------------------------------------------------------------------------
ALLOWED_LINES = [
    # Standard Windows locations - these ARE the documentation.
    re.compile(r"C:\\Windows\\(?:Microsoft\.NET|System32|Web)"),
    re.compile(r"C:\\\$Recycle\.Bin"),
    re.compile(r"%APPDATA%"),
    re.compile(r"\$env:APPDATA"),

    # The project's agreed synthetic identifiers.
    re.compile(r"LONGPATH~1\.USR"),
    re.compile(r"LongUserName\.USERHOST"),
    re.compile(r"tmpXXXX"),
    re.compile(r"<user>"),
    re.compile(r"\b(?:A1B2C3|D4E5F6|G7H8I9|J1K2L3|M4N5O6|P7Q8R9)\b"),

    # Legitimate references to the environment variable named USERNAME.
    re.compile(r"\$env:USERNAME\b"),
    re.compile(r"environ(?:\.get\(|\[)['\"]USERNAME['\"]"),

    # Illustrative placeholder for "a moment in time".
    re.compile(r"<同一时刻>"),
    re.compile(r"<same moment>"),

    # Public service hosts that happen to look like an email pattern.
    re.compile(r"git@github\.com"),
]


def allowed(line):
    """True when the line is exempt, checking every exemption at once.

    The whole line is tested rather than just the matched span, because the
    exemptions exist to certify a line as reviewed, not to pattern-match a token.
    """
    return any(pattern.search(line) for pattern in ALLOWED_LINES)


def collect():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in TEXT_EXT or name in TEXT_NAMES:
                files.append(os.path.join(dirpath, name))
    return sorted(files)


def check_junk_files():
    """Build artefacts and editor droppings must not be committed."""
    bad = []
    junk_names = re.compile(r"^(?:__pycache__|\.pytest_cache|build|dist|.*\.egg-info)$")
    junk_ext = re.compile(r"\.(?:pyc|pyo|log|tmp|bak|orig|rej|swp)$", re.I)
    junk_plain = {"Thumbs.db", ".DS_Store"}

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in list(dirnames):
            if junk_names.match(name):
                bad.append(os.path.relpath(os.path.join(dirpath, name), ROOT) + os.sep)
        for name in filenames:
            if junk_ext.search(name) or name in junk_plain:
                bad.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    return bad


def main():
    files = collect()
    print("scanning %d text files under %s" % (len(files), ROOT))
    print()

    findings = []
    for path in files:
        rel = os.path.relpath(path, ROOT)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError as exc:
            findings.append(("io", rel, 0, "unreadable: %s" % exc, ""))
            continue

        for lineno, line in enumerate(lines, 1):
            # Release blockers are checked first and ignore the exemption list.
            for category, label, pattern in PUBLISH_BLOCKERS:
                match = pattern.search(line)
                if match:
                    findings.append((category, rel, lineno, label, match.group(0)))

            if allowed(line):
                continue
            for category, label, pattern in FORBIDDEN:
                match = pattern.search(line)
                if match:
                    findings.append((category, rel, lineno, label, match.group(0)))

    junk = check_junk_files()

    leaks = [f for f in findings if f[0] != "publish"]
    blockers = [f for f in findings if f[0] == "publish"]

    if leaks:
        print("FAIL  %d leak(s)" % len(leaks))
        for category, rel, lineno, label, text in leaks:
            print("  [%s] %s:%d  %s -> %r" % (category, rel, lineno, label, text))
    else:
        print("PASS  no credential, identity, path or quoted-output leaks")

    if blockers:
        print()
        print("FAIL  %d release blocker(s)" % len(blockers))
        for _category, rel, lineno, label, text in blockers:
            print("  [%s] %s:%d  %s -> %r" % ("publish", rel, lineno, label, text))
    else:
        print("PASS  no unfilled placeholders")

    if junk:
        print()
        print("FAIL  %d leftover file(s) / directory(ies)" % len(junk))
        for item in junk:
            print("  %s" % item)
    else:
        print("PASS  no build artefacts or editor droppings")

    print()
    if findings or junk:
        print("PRIVACY CHECK FAILED")
        return 1
    print("PRIVACY CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
