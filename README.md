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
python3 <skill_dir>/scripts/vault-init.py init --vault ~/my-vault   # creates _ai/ + 10_notes/ (existing files are kept)
export VAULT_SYNC_DIR=~/my-vault
```

Then ask your agent `/vault-sync`. `<skill_dir>` is where your agent installed the skill (`./.agents/skills/vault-sync`, `~/.claude/skills/vault-sync`, ...). Already have a vault? Skip `init`; it needs `_ai/oracle_index.md` with a `## ALL NOTES` section and `_ai/tag_map.md`.

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

**Claude Code** (`~/.claude/settings.json`; `S` = `<skill_dir>/scripts`). Verified: the router reaches the model and it answers from it.

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "python3 S/vault-init.py context"}]}],
  "Notification": [{"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "python3 S/vault-capture.py hook claude"}]}]
}}
```

**omp**: copy `integrations/omp/vault-sync.ts` to `~/.omp/agent/extensions/`. Injects the router on the first turn and captures after 10 idle minutes or at shutdown. Verified: with it the model names the right note; without it, `NONE`.

**Any other agent** (codex, pi, gemini, ...):
- Router: add to its `AGENTS.md`/rules file: `At session start run python3 S/vault-init.py context and use its output.` (Hook-based injection is not wired or tested for these.)
- Capture: end-of-session hook `vault-capture.py hook <name> [FILE]`, or cron, which picks the newest session of any known agent and skips unchanged ones: `*/10 * * * * VAULT_SYNC_DIR=~/my-vault PATH=... python3 S/vault-capture.py run`.

Capture only creates new notes (never overwrites), adds the catalog row and a log entry, and skips what the vault already covers.

## Test

```bash
python3 -m unittest discover -s skills/vault-sync/tests
```

## License

MIT
