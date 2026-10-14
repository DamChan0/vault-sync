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

Ask your agent: `/vault-sync`, "vault lint", "recall ...", or the Korean triggers listed in `SKILL.md`. All bundled scripts are Python 3 stdlib only and run from any agent or a plain shell.

### Optional: auto-capture (any agent)

`vault-capture.py` turns a finished session into new seed notes. It is not tied to one agent:

| Setting | Meaning |
|---|---|
| `VAULT_SYNC_LLM` | LLM backend: a preset (`claude`, `omp`, `pi`, `codex`, `gemini`) or any command that reads the prompt on stdin and prints the answer, e.g. `ollama run llama3.1`. Auto-detected from PATH if unset. |
| `VAULT_SYNC_MODEL` | Model passed to the preset as `--model`. |
| `VAULT_SYNC_LANG` | Language of generated notes (default `English`). |
| `VAULT_SYNC_WORK_NOTES` | Optional extra directory indexed read-only by BM25. |

Each of these can also be set in `~/.config/vault-sync/profile.json` (`llm`, `lang`).

```bash
python3 skills/vault-sync/scripts/vault-capture.py run --transcript session.md --dry-run
python3 skills/vault-sync/scripts/vault-capture.py hook claude                  # Claude Code hook, stdin JSON
python3 skills/vault-sync/scripts/vault-capture.py hook myagent /path/to/file   # any end-of-session hook
```

Transcripts: Claude Code / omp / pi JSONL, Codex rollout JSONL, or plain text/markdown. Presets run ephemerally (no session saved, no tools, no extensions). Verified end-to-end with `omp`; `pi`, `codex`, `gemini` presets use their documented non-interactive flags and were checked for flag acceptance only.

## Test

```bash
python3 -m unittest discover -s skills/vault-sync/tests
```

## License

MIT
