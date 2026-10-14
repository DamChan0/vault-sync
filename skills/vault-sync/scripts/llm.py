#!/usr/bin/env python3
"""Agent-agnostic one-shot LLM call for vault-sync's bundled scripts.

vault-sync never assumes a particular coding agent. A backend is any command
that reads the prompt on stdin and prints the model's answer on stdout.

Selection, first match wins:
  1. $VAULT_SYNC_LLM
  2. "llm" in ~/.config/vault-sync/profile.json
  3. first preset whose CLI is on PATH (claude, omp, pi, codex, gemini)

The value is either a preset name or a custom command line, e.g.
  VAULT_SYNC_LLM='ollama run llama3.1'
  VAULT_SYNC_LLM='my-wrapper --json'
A custom command must read the prompt from stdin and write the answer to stdout.

$VAULT_SYNC_MODEL picks a model for presets (passed as --model). Presets run
ephemerally (no session saved, no tools, no extensions/hooks) from a temp cwd,
with VAULT_CAPTURE_RUNNING=1 so any hook that reaches vault-capture.py exits
immediately instead of recursing.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_config import profile_value  # noqa: E402

GUARD_ENV = "VAULT_CAPTURE_RUNNING"
LLM_ENV = "VAULT_SYNC_LLM"
MODEL_ENV = "VAULT_SYNC_MODEL"
TIMEOUT_S = 600

# Verified flags: claude, omp, pi, codex, gemini each accept the prompt on stdin
# in non-interactive mode. Detection order below follows this dict order.
PRESETS = {
    "claude": ["claude", "-p", "--setting-sources", "", "--no-session-persistence", "--tools", ""],
    "omp": ["omp", "-p", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-rules"],
    "pi": ["pi", "-p", "--no-session", "--no-tools", "--no-extensions", "--no-skills"],
    "codex": ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-"],
    "gemini": ["gemini", "-p", ""],
}
# Model flag goes before the preset's trailing "-"/prompt arg where order matters.
DEFAULT_MODEL = {"claude": "sonnet"}
MODEL_INSERT_AT = {"codex": 2}  # after the `exec` subcommand; other presets: after the binary


def resolve_command() -> list[str]:
    spec = os.environ.get(LLM_ENV) or profile_value("llm")
    if not spec:
        spec = next((name for name, cmd in PRESETS.items() if shutil.which(cmd[0])), None)
        if not spec:
            raise RuntimeError(
                f"no LLM backend: install one of {', '.join(PRESETS)} or set ${LLM_ENV} "
                "to a command that reads the prompt on stdin and prints the answer"
            )
    if spec in PRESETS:
        cmd = list(PRESETS[spec])
        model = os.environ.get(MODEL_ENV) or DEFAULT_MODEL.get(spec)
        if model:
            at = MODEL_INSERT_AT.get(spec, 1)
            cmd[at:at] = ["--model", model]
        return cmd
    return shlex.split(spec)


def complete(prompt: str) -> str:
    cmd = resolve_command()
    out = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S,
        env={**os.environ, GUARD_ENV: "1"}, cwd="/tmp",
    )
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed rc={out.returncode}: {out.stderr.strip()[:500]}")
    return out.stdout


def complete_json(prompt: str):
    """Return the first {...} object in the answer (agents often wrap JSON in prose or progress lines)."""
    text = complete(prompt)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"LLM returned no JSON object: {text[:300]!r}")
    return json.loads(m.group(0))
