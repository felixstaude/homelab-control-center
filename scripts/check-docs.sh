#!/usr/bin/env bash
#
# Documentation checks.
#
# The docs carry the architecture, the security model and the ADR history, and
# they cross-reference each other heavily. A broken link or a dangling ADR
# reference silently turns a documented decision into an undocumented one, so
# these are checked mechanically rather than by review.
#
# Verifies:
#   1. Every relative Markdown link resolves to a file that exists.
#   2. Every in-page anchor (#heading) matches a real heading.
#   3. Every ADR-NNN mentioned anywhere has a definition in ARCHITECTURE.md.
#   4. ADR numbering is contiguous with no duplicates.
#   5. Roadmap phase headings are sequential with no duplicates.
#   6. No unresolved editing markers are left behind.
#
# Usage:  ./scripts/check-docs.sh      (or: make check-docs)

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec python3 - "$@" <<'PYTHON'
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

GREEN, RED, CYAN, RESET = "\033[0;32m", "\033[0;31m", "\033[0;36m", "\033[0m"

failures: list[str] = []
checks = 0


def ok(message: str) -> None:
    global checks
    checks += 1
    print(f"{GREEN}  ✓ {message}{RESET}")


def bad(message: str) -> None:
    global checks
    checks += 1
    failures.append(message)
    print(f"{RED}  ✗ {message}{RESET}")


def section(title: str) -> None:
    print(f"{CYAN}== {title} =={RESET}")


# Markdown files tracked as documentation. Excludes node_modules and vendored
# trees by construction: only these locations are scanned.
#
# The community-health files are included because they are the first thing a
# visitor reads and the ones most likely to link somewhere that has moved.
# `docs/plans/` and `docs/development/` are included because a design document
# nobody link-checks rots exactly as fast as one nobody reads.
DOC_PATHS = sorted(
    {
        *Path("docs").glob("*.md"),
        *Path("docs/plans").glob("*.md"),
        *Path("docs/development").glob("*.md"),
        *Path("docs/security").glob("*.md"),
        Path("README.md"),
        Path("CONTRIBUTING.md"),
        Path("SECURITY.md"),
        Path("CODE_OF_CONDUCT.md"),
        Path("apps/server/README.md"),
        Path("apps/web/README.md"),
        Path("agents/linux/README.md"),
        Path("agents/linux/deploy/README.md"),
    }
)

LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)\)")
HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)


def slugify(title: str) -> str:
    """Approximate GitHub's heading-to-anchor conversion."""
    text = re.sub(r"`([^`]*)`", r"\1", title)          # strip inline code
    text = re.sub(r"\*\*?([^*]*)\*\*?", r"\1", text)   # strip emphasis
    text = LINK.sub(lambda m: m.group("text"), text)   # links keep their text
    text = unicodedata.normalize("NFKD", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)               # drop punctuation
    # One hyphen per whitespace character, not per run: GitHub does not
    # collapse runs, so "binaries — the" (two spaces after dropping the em
    # dash) yields a double hyphen in the anchor.
    return re.sub(r"\s", "-", text.strip())


anchors: dict[Path, set[str]] = {}
for path in DOC_PATHS:
    if not path.exists():
        continue
    seen: dict[str, int] = {}
    found: set[str] = set()
    for match in HEADING.finditer(path.read_text()):
        slug = slugify(match.group("title"))
        # GitHub disambiguates repeats with -1, -2, …
        if slug in seen:
            seen[slug] += 1
            found.add(f"{slug}-{seen[slug]}")
        else:
            seen[slug] = 0
            found.add(slug)
    anchors[path] = found

# ---------------------------------------------------------------------------
section("Link targets")

broken_links = 0
broken_anchors = 0
for path in DOC_PATHS:
    if not path.exists():
        bad(f"{path} is referenced by this script but does not exist")
        continue
    for match in LINK.finditer(path.read_text()):
        target = match.group("target")
        if target.startswith(("http://", "https://", "mailto:")):
            continue

        file_part, _, anchor = target.partition("#")

        if not file_part:                     # same-document anchor
            if anchor and anchor not in anchors[path]:
                bad(f"{path}: anchor #{anchor} does not match a heading")
                broken_anchors += 1
            continue

        resolved = (path.parent / file_part).resolve()
        if not resolved.exists():
            bad(f"{path}: link target {file_part} does not exist")
            broken_links += 1
            continue

        if anchor:
            try:
                rel = resolved.relative_to(Path.cwd())
            except ValueError:
                continue
            if rel in anchors and anchor not in anchors[rel]:
                bad(f"{path}: {file_part}#{anchor} does not match a heading there")
                broken_anchors += 1

if broken_links == 0:
    ok("every relative link resolves to an existing file")
if broken_anchors == 0:
    ok("every anchor matches a real heading")

# ---------------------------------------------------------------------------
section("ADR consistency")

arch = Path("docs/ARCHITECTURE.md")
arch_text = arch.read_text()

defined = [int(n) for n in re.findall(r"^### ADR-(\d{3}):", arch_text, re.MULTILINE)]

if len(defined) != len(set(defined)):
    duplicates = sorted({n for n in defined if defined.count(n) > 1})
    bad(f"duplicate ADR numbers defined: {duplicates}")
else:
    ok(f"{len(defined)} ADRs defined, no duplicates")

expected = list(range(1, len(defined) + 1))
if sorted(defined) != expected:
    missing = sorted(set(expected) - set(defined))
    bad(f"ADR numbering is not contiguous; missing: {missing}")
else:
    ok(f"ADR numbering is contiguous (ADR-001..ADR-{len(defined):03d})")

referenced: set[int] = set()
for path in DOC_PATHS:
    if path.exists():
        referenced |= {int(n) for n in re.findall(r"ADR-(\d{3})", path.read_text())}

dangling = sorted(referenced - set(defined))
if dangling:
    bad(f"referenced but never defined: {[f'ADR-{n:03d}' for n in dangling]}")
else:
    ok("every referenced ADR is defined in ARCHITECTURE.md")

# ADRs must live under the section that claims to contain them. Checked
# because they silently did not: ADR-025..032 were appended after the
# "Open design questions" heading and nested under it for three milestones,
# while every other check passed.
sections = [
    (m.start(), m.group(1))
    for m in re.finditer(r"^## (\d+)\. ", arch_text, re.MULTILINE)
]
adr_section = next((num for _, num in sections if "13" == num), None)
misplaced = []
for match in re.finditer(r"^### ADR-(\d{3}):", arch_text, re.MULTILINE):
    enclosing = None
    for offset, num in sections:
        if offset < match.start():
            enclosing = num
        else:
            break
    if enclosing != adr_section:
        misplaced.append(f"ADR-{match.group(1)} (under section {enclosing})")
if misplaced:
    bad(f"ADRs outside section {adr_section}: {misplaced}")
else:
    ok(f"every ADR is under section {adr_section}")

# ---------------------------------------------------------------------------
section("Roadmap structure")

roadmap = Path("docs/ROADMAP.md").read_text()
phases = [int(n) for n in re.findall(r"^## Phase (\d+)", roadmap, re.MULTILINE)]

if len(phases) != len(set(phases)):
    duplicates = sorted({n for n in phases if phases.count(n) > 1})
    bad(f"duplicate phase numbers: {duplicates}")
elif phases != sorted(phases):
    bad(f"phases are out of order: {phases}")
elif phases != list(range(phases[0], phases[0] + len(phases))):
    bad(f"phase numbering has gaps: {phases}")
else:
    ok(f"phases {phases[0]}..{phases[-1]} sequential, no duplicates")

# Milestone ids must belong to the phase they sit under.
current = None
mismatched: list[str] = []
for line in roadmap.splitlines():
    phase_heading = re.match(r"^## Phase (\d+)", line)
    if phase_heading:
        current = int(phase_heading.group(1))
        continue
    milestone = re.match(r"^\|\s*(\d+)\.(\d+)\s*\|", line)
    if milestone and current is not None and int(milestone.group(1)) != current:
        mismatched.append(f"{milestone.group(1)}.{milestone.group(2)} under Phase {current}")

if mismatched:
    bad(f"milestone ids in the wrong phase: {mismatched[:6]}")
else:
    ok("every milestone id matches its phase")

# ---------------------------------------------------------------------------
section("Editing hygiene")

MARKERS = ("TODO(", "FIXME", "XXX", "<<<<<<<", ">>>>>>>", "TK TK", "LOREM IPSUM")
found_markers: list[str] = []
for path in DOC_PATHS:
    if not path.exists():
        continue
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        for marker in MARKERS:
            if marker in line:
                found_markers.append(f"{path}:{number} {marker}")

if found_markers:
    for entry in found_markers:
        bad(f"unresolved marker: {entry}")
else:
    ok("no unresolved editing markers")

# Every doc should open with a level-1 heading.
for path in DOC_PATHS:
    if path.exists() and not path.read_text().lstrip().startswith(("# ", "<div")):
        bad(f"{path} does not start with a top-level heading")
        break
else:
    ok("every document starts with a heading")

# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{RED}{len(failures)} of {checks} documentation checks failed.{RESET}")
    sys.exit(1)
print(f"{GREEN}All {checks} documentation checks passed.{RESET}")
PYTHON
