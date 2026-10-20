# vault-sync

Agent skill that operates an Obsidian vault as a Karpathy-style LLM wiki: ingest, lint, query file-back, recall, resume. Includes BM25 search, a wikilink graph, a catalog-invariant checker, and optional idle auto-capture.

## Install

```bash
npx skills add DamChan0/vault-sync
```

Works with Claude Code, Codex, and other agents supported by the [`skills`](https://github.com/vercel-labs/skills) CLI.

## Quick start

```bash
npx skills add DamChan0/vault-sync
S=<skill_dir>/scripts        # where your agent installed it: ./.agents/skills/vault-sync, ~/.claude/skills/vault-sync, ...
python3 $S/vault-init.py init --vault ~/my-vault                 # creates _ai/ + 10_notes/ (existing files are kept)
python3 $S/vault-init.py install-hooks --vault ~/my-vault        # router at session start + auto-capture (Claude Code, omp)
```

Restart the agent session, then ask anything; it sees the vault router first and finished sessions are filed as new notes. Already have a vault? Skip `init`; it needs `_ai/oracle_index.md` with a `## ALL NOTES` section and `_ai/tag_map.md`.

`skills add` only copies skill files: it cannot activate hooks by itself. On the first write-authorized skill invocation, the agent now runs the idempotent hook installer for existing vaults too. Read-only requests never change setup. Restart/reload the host agent after activation; re-running `install-hooks --dry-run` should report `keep`, and `context` should print your router.

Existing non-numbered layouts can opt in schema-approved writable folders using `"capture_folders": "memory"` in `~/.config/vault-sync/profile.json` (comma-separated top-level names, existing real directories only; control-plane and symlink folders are rejected). Without this setting only numbered folders are writable. This is an explicit write-scope grant, not permission to capture into arbitrary personal folders. Set `"lang": "Korean"` for a Korean vault.

## Configure

Nothing is required if you run inside the vault or Obsidian knows it. The vault is found in this order: `--vault` → `$VAULT_SYNC_DIR` → `vault_dir` in `~/.config/vault-sync/profile.json` → a vault above the current directory → the vault registered in Obsidian's config.

The vault needs `_ai/CLAUDE.md` (schema/invariants) and `_ai/oracle_index.md` (router + `## ALL NOTES` catalog). See `skills/vault-sync/SKILL.md`.

## Use

Ask your agent: `/vault-sync`, "vault lint", "recall ...". Scripts are Python 3 stdlib only and run from any agent or a plain shell.

### Optional: auto-capture (any agent)

`vault-capture.py` turns a finished session into new seed notes. Auto-detected, no setup:

- **LLM backend**: installed CLI among `claude`, `omp`, `pi`, `codex`, `gemini` (next one is tried if a call fails).
- **Transcript**: newest session of those agents when none is given; format (Claude/omp/pi JSONL, Codex rollout, plain text) detected per file.

Overrides (env or `~/.config/vault-sync/profile.json`): `VAULT_SYNC_LLM`/`llm` (preset or any stdin→stdout command, e.g. `ollama run llama3.1`), `VAULT_SYNC_MODEL`, `VAULT_SYNC_LANG`/`lang` (default English), `VAULT_SYNC_WORK_NOTES` (extra read-only BM25 directory).

```bash
python3 skills/vault-sync/scripts/vault-capture.py run --dry-run            # newest session, preview
python3 skills/vault-sync/scripts/vault-capture.py hook claude              # Claude Code hook, stdin JSON
python3 skills/vault-sync/scripts/vault-capture.py hook myagent [FILE]      # any end-of-session hook
```

Verified end-to-end with `omp` and stub backends; `pi`, `codex`, `gemini` presets were checked for flag acceptance only.

## Make it automatic

Two halves: the agent **sees the router** at session start, and finished sessions **update the vault**. Skill instructions alone only apply once the skill is invoked, so wire these once per agent.

`vault-init.py install-hooks [--vault DIR] [--agent claude|omp] [--dry-run]` does it for **Claude Code** and **omp** (default: whichever of `~/.claude`, `~/.omp` exists). It resolves and pins the vault in `~/.config/vault-sync/profile.json` even without `--vault`, merges into `~/.claude/settings.json` (timestamped backup, existing hooks untouched, an event that already runs a vault-sync/recall command is skipped), and copies the omp extension without overwriting. Idempotent; `--dry-run` previews without writing.

What it adds (verified: the router reaches the model and it answers from it):

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "python3 S/vault-init.py context"}]}],
  "SessionEnd":   [{"hooks": [{"type": "command", "command": "python3 S/vault-capture.py hook claude"}]}],
  "Notification": [{"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "python3 S/vault-capture.py hook claude"}]}]
}}
```

**omp**: installs `vault-sync.ts` into `~/.omp/agent/extensions/` (source: `skills/vault-sync/integrations/omp/`). Injects the router on the first turn and captures after 10 idle minutes or at shutdown. Verified: with it the model names the right note; without it, `NONE`.

**Any other agent** (codex, pi, gemini, ...) — not automated, untested:
- Router: add to its `AGENTS.md`/rules file: `At session start run python3 S/vault-init.py context and use its output.`
- Capture: end-of-session hook `vault-capture.py hook <name> [FILE]`, or cron, which picks the newest session of any known agent and skips unchanged ones: `*/10 * * * * VAULT_SYNC_DIR=~/my-vault PATH=... python3 S/vault-capture.py run`.

Capture only creates new notes (never overwrites), adds the catalog row and a log entry, and skips what the vault already covers.

## Test

```bash
python3 -m unittest discover -s skills/vault-sync/tests
```

## License

MIT
