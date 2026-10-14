#!/usr/bin/env python3
"""Agent-agnostic one-shot LLM call for vault-sync's bundled scripts.

A backend is any command that reads the prompt on stdin and prints the answer
on stdout. Nothing assumes a particular coding agent.

Selection:
  - $VAULT_SYNC_LLM or profile "llm": exactly that backend (a preset name or a
    command line such as 'ollama run llama3.1'); no fallback.
  - otherwise: every preset whose CLI is on PATH, in PRESETS order; if one
    fails (not logged in, quota, timeout) the next is tried.

$VAULT_SYNC_MODEL picks a model for presets (--model). Presets run ephemerally
(no session, tools or extensions) from /tmp with VAULT_CAPTURE_RUNNING=1 so a
hook that reaches vault-capture.py exits instead of recursing.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_config import profile_value  # noqa: E402

GUARD_ENV = "VAULT_CAPTURE_RUNNING"
TIMEOUT_S = 600

# Each preset takes the prompt on stdin in non-interactive mode.
PRESETS = {
    "claude": ["claude", "-p", "--setting-sources", "", "--no-session-persistence", "--tools", ""],
    "omp": ["omp", "-p", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-rules"],
    "pi": ["pi", "-p", "--no-session", "--no-tools", "--no-extensions", "--no-skills"],
    "codex": ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-"],
    "gemini": ["gemini", "-p", ""],
}
DEFAULT_MODEL = {"claude": "sonnet"}
MODEL_INSERT_AT = {"codex": 2}  # after `exec`; other presets: right after the binary


def _preset_command(name: str) -> list[str]:
    cmd = list(PRESETS[name])
    model = os.environ.get("VAULT_SYNC_MODEL") or DEFAULT_MODEL.get(name)
    if model:
        at = MODEL_INSERT_AT.get(name, 1)
        cmd[at:at] = ["--model", model]
    return cmd


def candidates() -> list[list[str]]:
    spec = os.environ.get("VAULT_SYNC_LLM") or profile_value("llm")
    if spec:
        return [_preset_command(spec) if spec in PRESETS else shlex.split(spec)]
    found = [_preset_command(name) for name, cmd in PRESETS.items() if shutil.which(cmd[0])]
    if not found:
        raise RuntimeError(
            f"no LLM backend: install one of {', '.join(PRESETS)} or set VAULT_SYNC_LLM "
            "to a command that reads the prompt on stdin and prints the answer"
        )
    return found


def _run(cmd: list[str], prompt: str) -> str:
    out = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S,
        env={**os.environ, GUARD_ENV: "1"}, cwd="/tmp",
    )
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed rc={out.returncode}: {out.stderr.strip()[:300]}")
    return out.stdout


def last_json_object(text: str):
    """Last valid {...} in text. Agents add progress lines or echo the prompt around the answer."""
    decoder, found, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            if isinstance(obj, dict):
                found = obj
            i = text.find("{", end)
        except ValueError:
            i = text.find("{", i + 1)
    if found is None:
        raise RuntimeError(f"LLM returned no JSON object: {text[:300]!r}")
    return found


def complete_json(prompt: str):
    """First backend that yields a JSON object wins; raises the last error if none do."""
    error = None
    for cmd in candidates():
        try:
            return last_json_object(_run(cmd, prompt))
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            error = RuntimeError(f"{cmd[0]}: {e}")
    raise error
