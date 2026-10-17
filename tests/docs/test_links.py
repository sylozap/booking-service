"""Links of the documentation lead somewhere.

Every relative link of every Markdown file of the repository is followed: the
file it names exists and is not ignored by git, and the heading its anchor
names is in that file. A link to a file git ignores works on the machine of its
author and nowhere else.

Anchors are slugged the way GitHub slugs headings, since GitHub is where the
documents are read. Links to the outside are not followed: the answer would
depend on the network, not on the repository.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")
# [text](target) and ![alt](target); a title after the target is allowed.
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def git(*args: str) -> list[str]:
    done = subprocess.run(  # noqa: S603 -- the arguments are this file's own
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.splitlines()


def markdown_files() -> list[Path]:
    """The documents of the repository: tracked, or new and not ignored."""
    listed = git("ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md")
    return sorted(ROOT / name for name in listed if (ROOT / name).exists())


def ignored(path: Path) -> bool:
    return bool(git("check-ignore", "--no-index", "--", str(path)))


def prose_lines(text: str) -> list[str]:
    """The lines of a document outside its code blocks, inline code blanked."""
    lines, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(INLINE_CODE.sub("", line))
    return lines


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lowercase, punctuation dropped, spaces as dashes."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "").lower()
    kept = "".join(
        char for char in text if char in " -_" or unicodedata.category(char)[0] in {"L", "N", "M"}
    )
    return kept.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: Counter[str] = Counter()
    found = set()
    for line in prose_lines(path.read_text()):
        heading = HEADING.match(line)
        if heading is None:
            continue
        base = slug(heading.group(2))
        found.add(f"{base}-{seen[base]}" if seen[base] else base)
        seen[base] += 1
    return found


def links(path: Path) -> list[str]:
    return [target for line in prose_lines(path.read_text()) for target in LINK.findall(line)]


def broken_links(path: Path) -> list[str]:
    broken = []
    for target in links(path):
        if EXTERNAL.match(target):
            continue
        file_part, _, anchor = unquote(target).partition("#")
        linked = (path.parent / file_part).resolve() if file_part else path
        if not linked.exists():
            broken.append(f"{target}: no such file")
        elif ignored(linked):
            broken.append(f"{target}: ignored by git, not in the repository")
        elif anchor and linked.suffix == ".md" and anchor not in anchors(linked):
            broken.append(f"{target}: no such heading")
    return broken


@pytest.mark.parametrize("document", markdown_files(), ids=lambda path: str(path.relative_to(ROOT)))
def test_every_relative_link_of_a_document_leads_somewhere(document: Path) -> None:
    assert broken_links(document) == []


def test_heading_is_slugged_the_way_github_slugs_it() -> None:
    assert slug("Трассировка") == "трассировка"
    assert slug("T9.7 · README проекта") == "t97--readme-проекта"
    assert slug("`booking` и [ADR-0001](adr/0001.md)") == "booking-и-adr-0001"


def test_repeated_heading_gets_the_number_of_its_repetition(tmp_path: Path) -> None:
    document = tmp_path / "doc.md"
    document.write_text("# Тесты\n\n## Тесты\n\n```\n# Тесты\n```\n")

    assert anchors(document) == {"тесты", "тесты-1"}
