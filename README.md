# vault-sync

Agent skill that operates an Obsidian vault as a Karpathy-style LLM wiki: ingest, lint, query file-back, recall, resume. Includes BM25 search, a wikilink graph, a catalog-invariant checker, and optional idle auto-capture.

## Install

```bash
npx skills add DamChan0/vault-sync
```

Works with Claude Code, Codex, and other agents supported by the [`skills`](https://github.com/vercel-labs/skills) CLI.

## Configure

Point the scripts at your vault, either way:

```bash
export VAULT_SYNC_DIR="$HOME/path/to/vault"
# or
mkdir -p ~/.config/vault-sync
echo '{"vault_dir": "~/path/to/vault"}' > ~/.config/vault-sync/profile.json
```

The vault needs `_ai/CLAUDE.md` (schema/invariants) and `_ai/oracle_index.md` (router + `## ALL NOTES` catalog). See `skills/vault-sync/SKILL.md`.

## Use

Ask your agent: `/vault-sync`, "vault 업데이트", "vault lint", "recall ...". Scripts need Python 3 only (stdlib).

`scripts/vault-capture.py` (auto-capture from session transcripts) additionally needs the `claude` CLI; wiring it to a hook is up to you.

## Test

```bash
python3 -m unittest discover -s skills/vault-sync/tests
```

## License

MIT
