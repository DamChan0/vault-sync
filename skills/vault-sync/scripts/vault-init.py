#!/usr/bin/env python3
"""vault-init.py — bootstrap a vault for vault-sync and wire it into your agents.

  vault-init.py init [--vault DIR]   create the _ai/ skeleton + 10_notes/ in DIR
                                     (default: the vault vault_config resolves, else the cwd).
                                     Existing files are never overwritten; re-running is safe.
  vault-init.py context [--vault DIR]
                                     print _ai/oracle_index.md wrapped in a short instruction,
                                     for a session-start hook or an agent's first step.
                                     Prints nothing and exits 0 if there is no vault yet,
                                     so a hook can never break a session.
  vault-init.py install-hooks [--vault DIR] [--agent claude|omp] [--dry-run]
                                     wire router injection + auto-capture: merge hooks into
                                     ~/.claude/settings.json (backup kept, existing hooks untouched)
                                     and/or copy the omp extension. --vault also pins the vault in
                                     ~/.config/vault-sync/profile.json. Default agents: those with a
                                     ~/.claude or ~/.omp directory.
"""
import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_config  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent

# (event, matcher, script + args, markers: a command containing one means the event is already wired)
CLAUDE_HOOKS = (
    ("SessionStart", None, "vault-init.py context", ("vault-init.py context", "vault-recall")),
    ("SessionEnd", None, "vault-capture.py hook claude", ("vault-capture.py hook claude",)),
    ("Notification", "idle_prompt", "vault-capture.py hook claude", ("vault-capture.py hook claude",)),
)
OMP_SKILL_ROOTS = (".agents", ".claude", ".codex", ".omp/agent")  # keep in sync with integrations/omp/vault-sync.ts

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
    "_ai/README_AGENT.md": """\
---
title: Agent entry
---

# Agent entry

1. Read `oracle_index.md` (router) — it is injected at session start when hooks are installed.
2. Match keywords → Read the note body (line ranges) → answer with `[[wikilinks]]`.
3. Not in the router → `vault-bm25.py query "<question>"` → folder scan → say so and mark claims ❓.
4. Changing the vault (ingest/lint/file-back) → read `CLAUDE.md` first.
5. `hot.md` = short recent-context cache; read it first after a restart or compaction.
""",
    "_ai/hot.md": """\
---
title: Hot cache
---

# Hot cache

Short pointer to what is in flight (goal, decisions, next step). Rewrite it completely when it goes stale;
durable knowledge belongs in notes, not here.
""",
    "_ai/topic_graph.md": """\
---
title: Topic graph
---

# Topic graph

Cross-cluster relations that wikilinks do not show. One line per edge: `[[a]] -> [[b]] — why`.
""",
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
    print(f"\nvault ready: {vault}\nnext: python3 {SCRIPTS / 'vault-init.py'} install-hooks --vault {vault}   (auto-reference + auto-capture)")


def context(vault_arg: Path | None = None) -> int:
    try:
        with contextlib.redirect_stderr(io.StringIO()):  # "no vault found" is not an error for a hook
            vault = vault_config.load_vault_dir(str(vault_arg) if vault_arg else None)
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


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"ERR: {path} is not valid JSON ({e}); fix it first, nothing was changed")
    if not isinstance(data, dict):
        sys.exit(f"ERR: {path} is not a JSON object; nothing was changed")
    return data


def _write_json(path: Path, data: dict) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def install_claude(settings: Path, dry_run: bool = False) -> bool:
    """Merge the router + capture hooks into a Claude Code settings.json. Existing hooks are kept;
    an event that already runs a vault-sync/recall command is left alone. Returns True if it changed."""
    data = _load_json(settings)
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, matcher, script, markers in CLAUDE_HOOKS:
        groups = hooks.setdefault(event, [])
        if any(m in h.get("command", "") for g in groups for h in g.get("hooks", []) for m in markers):
            print(f"keep    claude {event}")
            continue
        name, _, rest = script.partition(" ")
        group = {"hooks": [{"type": "command", "command": f"python3 {shlex.quote(str(SCRIPTS / name))} {rest}"}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        changed = True
        print(f"added   claude {event}" + (f" ({matcher})" if matcher else ""))
    if changed and not dry_run:
        _write_json(settings, data)
    return changed


def install_omp(ext_dir: Path, dry_run: bool = False) -> bool:
    """Copy the omp extension; an existing vault-sync.ts is never overwritten."""
    dest = ext_dir / "vault-sync.ts"
    if dest.exists():
        print("keep    omp extension")
        return False
    if not dry_run:
        ext_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SCRIPTS.parent / "integrations/omp/vault-sync.ts", dest)
    print(f"added   omp extension -> {dest}")
    skill = SCRIPTS.parent.resolve()
    if skill not in [(Path.home() / d / "skills/vault-sync").resolve() for d in OMP_SKILL_ROOTS]:
        print(f"note    skill is outside the standard dirs: export VAULT_SYNC_SKILL_DIR={skill}")
    return True


def pin_vault(vault: Path, dry_run: bool = False) -> None:
    """Record the vault in profile.json so every agent and script finds it without env vars."""
    profile = _load_json(vault_config.PROFILE_PATH)
    if profile.get("vault_dir") == str(vault):
        print("keep    profile vault_dir")
        return
    profile["vault_dir"] = str(vault)
    if not dry_run:
        _write_json(vault_config.PROFILE_PATH, profile)
    print(f"pinned  vault_dir={vault} in {vault_config.PROFILE_PATH}")


def install_hooks(agents: list[str], vault: Path | None, dry_run: bool = False) -> int:
    home = Path.home()
    if not agents:
        agents = [a for a in ("claude", "omp") if (home / f".{a}").is_dir()]
    if not agents:
        print("no ~/.claude or ~/.omp found; pass --agent claude|omp")
        return 1
    resolved = vault_config.load_vault_dir(str(vault) if vault else None).resolve()
    pin_vault(resolved, dry_run)
    for agent in agents:
        if agent == "claude":
            install_claude(home / ".claude/settings.json", dry_run)
        else:
            install_omp(home / ".omp/agent/extensions", dry_run)
    print("\n(dry run, nothing written)" if dry_run else "\nrestart the agent session for hooks to take effect")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").add_argument("--vault", type=Path)
    sub.add_parser("context").add_argument("--vault", type=Path)
    hk = sub.add_parser("install-hooks")
    hk.add_argument("--vault", type=Path, help="also pin this vault in ~/.config/vault-sync/profile.json")
    hk.add_argument("--agent", action="append", choices=("claude", "omp"), default=[],
                    help="repeatable; default: every agent whose ~/.<agent> dir exists")
    hk.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "context":
        return context(args.vault.expanduser() if args.vault else None)
    if args.cmd == "install-hooks":
        return install_hooks(args.agent, args.vault, args.dry_run)
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
