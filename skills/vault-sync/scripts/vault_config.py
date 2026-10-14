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


def load_vault_dir(override=None):
    """Return the vault root as a Path.

    `override` (e.g. a --vault CLI flag) takes precedence over the profile,
    for read-only inspection of another host's vault. Otherwise reads
    $VAULT_SYNC_DIR, then vault_dir from ~/.config/vault-sync/profile.json.
    A missing or malformed profile fails loud (stderr + non-zero exit)
    rather than silently no-op'ing.
    """
    if override:
        vault_dir = os.path.expanduser(override)
        if not Path(vault_dir).is_dir():
            print(f"ERR: --vault {override!r} is not a directory", file=sys.stderr)
            sys.exit(1)
        return Path(vault_dir)
    env_dir = os.environ.get(ENV_VAR)
    if env_dir:
        resolved = Path(os.path.expanduser(env_dir))
        if not resolved.is_dir():
            print(f"ERR: ${ENV_VAR}={env_dir!r} is not a directory", file=sys.stderr)
            sys.exit(1)
        return resolved
    try:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        vault_dir = profile["vault_dir"]
    except FileNotFoundError:
        print(f"ERR: no vault configured: set ${ENV_VAR} or create {PROFILE_PATH} with {{'vault_dir': '~/path/to/vault'}}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"ERR: malformed profile {PROFILE_PATH}: {error}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(vault_dir, str) or not vault_dir:
        print(f"ERR: profile.json vault_dir must be a non-empty string, got {vault_dir!r}", file=sys.stderr)
        sys.exit(1)
    resolved = Path(os.path.expanduser(vault_dir))
    if not resolved.is_dir():
        print(f"ERR: profile vault_dir {vault_dir!r} is not a directory", file=sys.stderr)
        sys.exit(1)
    return resolved


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
