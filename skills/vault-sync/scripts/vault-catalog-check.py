#!/usr/bin/env python3
"""vault-catalog-check.py — audit the oracle_index.md catalog invariant.

Invariant (see _ai/CLAUDE.md and the oracle_index.md preamble): every
knowledge note is reachable from QUICK ROUTES or the "ALL NOTES — 전체
카탈로그" section of _ai/oracle_index.md. Empty, template, and generated
artifacts (.vault-meta/**, Excalidraw) are excluded from the invariant, not
from the vault.

Only _ai/oracle_index.md is injected at session start (not hot.md), so a
note the catalog doesn't reach is, for retrieval purposes, a note that does
not exist. This script is the automatable half of that invariant: it finds
notes the catalog misses and catalog links that resolve to nothing.

Usage:
  vault-catalog-check.py [--vault PATH]

Exit codes: 0 = invariant holds, 1 = missing note(s) or broken link(s).
"""
import argparse
import os
import re
import sys
from pathlib import Path

import vault_config

# Directories excluded from the invariant outright: Obsidian/vault-sync
# control plane and generated artifacts, never user knowledge notes.
EXCLUDED_DIR_NAMES = {".obsidian", ".trash", "_ai", ".vault-meta", "Templates"}

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
ALIASES_RE = re.compile(r"^aliases:\s*\[(.*?)\]\s*$", re.M)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
# A stub (just a heading, a slash, a TOC of links) carries no knowledge to route to.
STUB_MAX_CHARS = 30
MARKUP_RE = re.compile(r"\[\[[^\]]*\]\]|\[![A-Za-z]+\][-+]?|[#>*_`\-|/\s]")


def is_stub(body: str) -> bool:
    return len(MARKUP_RE.sub("", body)) < STUB_MAX_CHARS


def is_excluded(rel_path: Path) -> bool:
    """Path (relative to vault root) excluded from the catalog invariant."""
    if rel_path.name.endswith(".excalidraw.md"):
        return True
    return any(part in EXCLUDED_DIR_NAMES for part in rel_path.parts[:-1])


def split_frontmatter(text: str):
    """Return (frontmatter_text, body_text); frontmatter_text is '' if absent."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


def parse_aliases(frontmatter: str):
    match = ALIASES_RE.search(frontmatter)
    if not match:
        return []
    return [a.strip().strip("\"'") for a in match.group(1).split(",") if a.strip().strip("\"'")]


def scan_vault(vault_dir: Path):
    """Return (resolvable, live_notes, empty_notes).

    `resolvable` covers every real .md file's (rel_no_ext, stem, aliases) —
    including control-plane/Templates/Excalidraw — because a wikilink to one
    of those is a real, non-broken link; it just doesn't satisfy the catalog
    invariant. `live_notes`/`empty_notes` are catalog-eligible files only
    (is_excluded() filtered), split on whether the body has content.
    """
    resolvable = set()
    live_notes, empty_notes = [], []
    for path in sorted(vault_dir.rglob("*.md")):
        rel = path.relative_to(vault_dir)
        rel_no_ext = rel.with_suffix("").as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            print(f"WARN: skipping unreadable {rel}: {error}", file=sys.stderr)
            continue
        frontmatter, body = split_frontmatter(text)
        aliases = parse_aliases(frontmatter)
        resolvable.add(rel_no_ext)
        resolvable.add(path.stem)
        resolvable.update(aliases)
        if is_excluded(rel):
            continue
        if not is_stub(body):
            live_notes.append((rel_no_ext, path.stem, aliases))
        else:
            empty_notes.append(rel_no_ext)
    return resolvable, live_notes, empty_notes


def load_oracle_targets(vault_dir: Path):
    """Return the set of raw [[wikilink]] target strings in oracle_index.md."""
    oracle_path = vault_dir / "_ai" / "oracle_index.md"
    if not oracle_path.is_file():
        print(f"ERR: no {oracle_path}", file=sys.stderr)
        sys.exit(1)
    text = oracle_path.read_text(encoding="utf-8")
    return [m.strip() for m in WIKILINK_RE.findall(text)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vault", help="Vault root override (default: $VAULT_SYNC_DIR or ~/.config/vault-sync/profile.json).")
    args = parser.parse_args()
    vault_dir = vault_config.load_vault_dir(args.vault)

    resolvable, live_notes, empty_notes = scan_vault(vault_dir)

    targets = load_oracle_targets(vault_dir)
    target_set = set(targets)

    missing = [
        rel for rel, stem, aliases in live_notes
        if rel not in target_set and stem not in target_set and not (target_set & set(aliases))
    ]
    broken = sorted({t for t in targets if t not in resolvable})

    print(f"vault: {vault_dir}")
    print(f"notes: {len(live_notes)} ({len(empty_notes)} empty/template excluded)")
    print(f"missing from oracle_index: {len(missing)}")
    for rel in sorted(missing):
        print(f"  {rel}")
    print(f"broken oracle_index links: {len(broken)}")
    for target in broken:
        print(f"  [[{target}]]")
    if empty_notes:
        print(f"empty/template notes (informational, excluded): {len(empty_notes)}")
        for rel in sorted(empty_notes):
            print(f"  {rel}")

    return 1 if (missing or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
