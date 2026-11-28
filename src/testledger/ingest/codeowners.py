"""CODEOWNERS parsing and owner resolution.

Reads a repository's ``CODEOWNERS`` file from ``.github/``, the repo root, or
``docs/`` (GitHub's lookup order) and resolves the owners of a path using
GitHub's rules: gitignore-style patterns where the **last matching pattern
wins**. As on GitHub, ``#`` starts a comment, ``!`` negation and ``[...]``
character classes are not supported (those lines never match), and ``\\`` is
not an escape character.

Matching semantics (gitignore): a pattern containing a ``/`` (other than a
trailing one) is anchored to the repo root; otherwise it matches the file
name in any directory. A trailing ``/`` marks a directory and owns everything
beneath it; a pattern that matches a directory also owns its contents.
``*`` and ``?`` never cross ``/``, ``**`` does. Paths are normalised to ``/``
separators first, so Windows-style paths resolve the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def _translate(pattern: str) -> re.Pattern[str] | None:
    """Compile one CODEOWNERS pattern to a regex, or None when it can never match."""
    if pattern.startswith("!") or "[" in pattern:
        return None  # negation and character classes are unsupported on GitHub
    anchored = "/" in pattern.rstrip("/")
    pattern = pattern.lstrip("/")
    if pattern.endswith("/"):
        pattern += "**"

    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                # '**' crosses directories; a following '/' also matches zero dirs
                if pattern[i : i + 3] == "**/":
                    out.append("(.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    if anchored:
        regex = f"^{body}(/.*)?$"
    else:
        regex = f"(^|.*/){body}(/.*)?$"
    return re.compile(regex)


class CodeOwners:
    """An ordered rule list; later rules override earlier ones."""

    def __init__(self, rules: list[tuple[re.Pattern[str], list[str]]]):
        self.rules = rules

    @classmethod
    def parse(cls, text: str) -> CodeOwners:
        rules: list[tuple[re.Pattern[str], list[str]]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            compiled = _translate(parts[0])
            if compiled is not None:
                rules.append((compiled, parts[1:]))
        return cls(rules)

    @classmethod
    def load(cls, repo: Path, path: str | None = None) -> CodeOwners | None:
        """Load from an explicit ``path`` (relative to ``repo``), or auto-detect.

        Returns None when no CODEOWNERS file exists.
        """
        if path is not None:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = repo / candidate
            if not candidate.is_file():
                return None
            return cls.parse(candidate.read_text(encoding="utf-8"))
        for location in LOCATIONS:
            candidate = repo / location
            if candidate.is_file():
                return cls.parse(candidate.read_text(encoding="utf-8"))
        return None

    def owners_for(self, path: str) -> list[str]:
        """Owners of ``path``; the last matching rule wins. Empty when none match."""
        normalised = path.replace("\\", "/").lstrip("/")
        owners: list[str] = []
        for pattern, rule_owners in self.rules:
            if pattern.match(normalised):
                owners = rule_owners
        return owners
