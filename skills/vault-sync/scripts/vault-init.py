#!/usr/bin/env python3
"""vault-init.py — bootstrap a vault for vault-sync and feed its router to an agent.

  vault-init.py init [--vault DIR]   create the _ai/ skeleton + 10_notes/ in DIR
                                     (default: the vault vault_config resolves, else the cwd).
                                     Existing files are never overwritten; re-running is safe.
  vault-init.py context              print _ai/oracle_index.md wrapped in a short instruction,
                                     for a session-start hook or an agent's first step.
                                     Prints nothing and exits 0 if there is no vault yet,
                                     so a hook can never break a session.
"""
import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_config  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent

FILES = {
    "_ai/CLAUDE.md": """\
---
title: Vault schema
---

# Vault schema (authoritative for vault-sync)

## Invariants
1. Every knowledge note is reachable from `oracle_index.md` (QUICK ROUTES or the `## ALL NOTES` catalog). A note the catalog does not reach does not exist for retrieval.
2. `log.md` is append-only.
3. Tags come from `tag_map.md`; register a tag there before using it.
4. New notes start as `status: seed` with frontmatter `title, type, status, created, updated, tags` (+ `aliases, keywords, related, sources`).
5. Automatic capture only creates new notes in managed folders; it never overwrites or deletes.

## Managed folders
Numbered top-level folders (`10_notes/`, `20_...`). Create more as `NN_name`.

## Do-not-touch folders
None yet. List personal/user-owned folders here (the skill never writes to them).
""",
    "_ai/oracle_index.md": """\
---
title: Oracle Index
---

# ORACLE INDEX

> Match keywords from the user's question below and Read the matching notes.
> Invariant: every knowledge note is reachable from QUICK ROUTES or ALL NOTES.
> A topic that is not listed here has no note in the vault.

## QUICK ROUTES BY KEYWORD

| keywords | note |
|---|---|

## ALL NOTES — catalog
""",
    "_ai/tag_map.md": """\
---
title: Tag map
---

# Tag vocabulary

Register every tag here (in backticks) before use. Format: `area/topic`.

- `workflow/ops` — procedures and how-tos
- `decision/design` — decisions and their reasons
- `incident/debug` — failures, causes, fixes
""",
    "_ai/log.md": "# Log\n\nAppend-only. One entry per ingest/capture/lint.\n",
    "10_notes/.gitkeep": "",
}


def init(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    for rel, text in FILES.items():
        path = vault / rel
        if path.exists():
            print(f"keep    {rel}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"created {rel}")
    print(f"\nvault ready: {vault}\nnext: set VAULT_SYNC_DIR={vault} (or run inside it), then ask your agent: /vault-sync")


def context() -> int:
    try:
        with contextlib.redirect_stderr(io.StringIO()):  # "no vault found" is not an error for a hook
            vault = vault_config.load_vault_dir()
        router = (vault / "_ai" / "oracle_index.md").read_text(encoding="utf-8")
    except (SystemExit, OSError):
        return 0
    print(
        f"Obsidian vault router for `{vault}` (loaded once at session start). Decide from it whether the vault holds "
        "anything relevant; if so, Read the matching notes before acting and cite them with [[wikilinks]]. For topics "
        f'it does not cover run `python3 {SCRIPTS}/vault-bm25.py query "<question>"`. Vault first, then external '
        "search. Unverified claims are marked ❓.\n\n--- BEGIN _ai/oracle_index.md ---\n"
        f"{router}\n--- END ---"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").add_argument("--vault", type=Path)
    sub.add_parser("context")
    args = ap.parse_args()
    if args.cmd == "context":
        return context()
    if args.vault:
        target = args.vault.expanduser()
    elif os.environ.get(vault_config.ENV_VAR) or vault_config.profile_value("vault_dir"):
        target = vault_config.load_vault_dir()
    else:
        target = Path.cwd()
    init(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
