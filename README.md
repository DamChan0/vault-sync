# vault-sync

Agent skill that operates an Obsidian vault as a Karpathy-style LLM wiki: ingest, lint, query file-back, recall, resume. Includes BM25 search, a wikilink graph, a catalog-invariant checker, and optional idle auto-capture.

## Install

```bash
npx skills add DamChan0/vault-sync
```

Works with Claude Code, Codex, and other agents supported by the [`skills`](https://github.com/vercel-labs/skills) CLI.

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

## Test

```bash
python3 -m unittest discover -s skills/vault-sync/tests
```

## License

MIT
