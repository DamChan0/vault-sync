#!/usr/bin/env python3
"""Shared config helpers for the vault-sync skill's bundled scripts.

Why a shared module: vault-sync runs on multiple machines, each with a
differently named, differently laid-out Obsidian vault. No script here may
hardcode one machine's vault path or folder taxonomy — the path comes from
$VAULT_SYNC_DIR or ~/.config/vault-sync/profile.json and, for folder
taxonomy, from the vault's own _ai/CLAUDE.md.
"""
import json
import os
import sys
from pathlib import Path

PROFILE_PATH = Path(os.path.expanduser("~/.config/vault-sync/profile.json"))
ENV_VAR = "VAULT_SYNC_DIR"

# Directories every vault-sync install treats as control-plane/generated,
# regardless of what a given vault calls its knowledge folders. Kept separate
# from any one vault's own do-not-touch list (that list is user content and
# lives in _ai/CLAUDE.md, not here).
CONTROL_PLANE_DIRS = frozenset({
    "_ai", ".vault-meta", ".obsidian", ".trash", ".git",
    ".makemd", ".space", "Templates", "Excalidraw",
})


def profile_value(key):
    """Optional string setting from profile.json; None if the file or key is absent."""
    try:
        value = json.loads(PROFILE_PATH.read_text(encoding="utf-8")).get(key)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _fail(message):
    print(f"ERR: {message}", file=sys.stderr)
    sys.exit(1)


def _is_vault(path):
    return (path / "_ai").is_dir() or (path / ".obsidian").is_dir()


def _discover():
    """Vault above the cwd, else one registered in Obsidian's own config. None if not found."""
    for parent in (Path.cwd(), *Path.cwd().parents):
        if _is_vault(parent):
            return parent
    for config in (Path("~/.config/obsidian/obsidian.json"),
                   Path("~/Library/Application Support/obsidian/obsidian.json"),
                   Path("~/.var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json"),
                   Path(os.environ.get("APPDATA", "~") + "/obsidian/obsidian.json")):
        try:
            vaults = json.loads(config.expanduser().read_text(encoding="utf-8"))["vaults"].values()
        except (OSError, ValueError, KeyError, AttributeError):
            continue
        found = [(v.get("open", False), v.get("ts", 0), Path(v["path"]))
                 for v in vaults if isinstance(v, dict) and v.get("path") and Path(v["path"]).is_dir()]
        # Prefer a vault already laid out for vault-sync (_ai/), then the open/most recent one.
        found.sort(key=lambda t: ((t[2] / "_ai").is_dir(), t[0], t[1]), reverse=True)
        if found:
            return found[0][2]
    return None


def load_vault_dir(override=None):
    """Return the vault root as a Path.

    Order: `override` (--vault flag) → $VAULT_SYNC_DIR → profile `vault_dir` →
    a vault above the cwd → Obsidian's registered vaults. An explicitly
    configured path that is not a directory fails loud rather than falling
    through to a different vault.
    """
    explicit = override or os.environ.get(ENV_VAR) or profile_value("vault_dir")
    if explicit:
        path = Path(os.path.expanduser(explicit))
        if not path.is_dir():
            _fail(f"vault path {explicit!r} is not a directory")
        return path
    found = _discover()
    if found is None:
        _fail(f"no vault found: run inside one, pass --vault, set ${ENV_VAR}, "
              f"or put {{'vault_dir': '~/path/to/vault'}} in {PROFILE_PATH}")
    return found


def default_indexed_dirs(vault_dir, exclude=()):
    """Every top-level vault directory except control-plane and caller-excluded ones.

    The caller (agent or script invoker) is expected to read the vault's
    _ai/CLAUDE.md for its documented do-not-touch/personal folders and pass
    them via `exclude`; this taxonomy is vault content, not something this
    kit hardcodes for one machine. With no `exclude`, everything not
    structurally control-plane is included — favors the catalog-completeness
    invariant (missing a real note) over search-index tidiness.
    """
    excluded = CONTROL_PLANE_DIRS | {name.strip() for name in exclude if name.strip()}
    return sorted(
        p.name for p in vault_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in excluded
    )
