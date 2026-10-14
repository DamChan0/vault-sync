#!/usr/bin/env python3
"""Idle/session-end capture: file a finished session's durable knowledge into the vault.

This is the sanctioned automatic ingest path (vault _ai/CLAUDE.md invariant 3
auto-apply grant): it only CREATES new managed notes, never overwrites, never
touches user folders, appends an ALL NOTES catalog row and a `_ai/log.md` entry
for every write, then rebuilds the BM25 index.

Entry points (harness = free-form label for the source agent):
  vault-capture.py hook claude              # Claude Code hook (stdin JSON), detaches
  vault-capture.py hook HARNESS [FILE [ID]] # any other agent/script, detaches
  vault-capture.py run [--transcript FILE] [--session-id ID] [--harness H] [--dry-run] [--force]
FILE defaults to the newest session of any known agent (claude, omp, pi, codex).

Transcript formats (auto-detected): JSONL from Claude Code / omp / pi
(`message.role`), Codex rollout JSONL (`payload.role`), or plain text/markdown.

LLM backend: llm.py (any agent CLI or custom stdin→stdout command). Recursion
guard: it runs the backend with VAULT_CAPTURE_RUNNING=1, ephemerally; every
entry point exits immediately when that variable is set.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_config import load_vault_dir, profile_value  # noqa: E402
from llm import GUARD_ENV, complete_json  # noqa: E402

STATE_DIR = Path.home() / ".local/state/vault-capture"
SCRIPTS = Path(__file__).resolve().parent
LANG = os.environ.get("VAULT_SYNC_LANG") or profile_value("lang") or "English"  # language of generated note text


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()
# Managed knowledge folders are the vault's numbered top-level dirs (NN_name);
# anything else (user folders, Templates, control plane) is off limits.
MANAGED_FOLDER_RE = re.compile(r"^\d\d_")


def writable_folders(vault: Path) -> tuple[str, ...]:
    return tuple(sorted(p.name for p in vault.iterdir() if p.is_dir() and MANAGED_FOLDER_RE.match(p.name)))
MAX_NOTES = 3
TRANSCRIPT_CHARS = 90_000
MIN_DIALOGUE_CHARS = 2_000
# Context an agent injects into user turns; not part of the conversation.
_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>",
                      "<permissions", "<skills_instructions>", "<collaboration_mode>")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,60}$")


def log(msg: str) -> None:
    print(f"[vault-capture {_now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- transcript

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text")
    )


_INJECTED = re.compile(r"<system-reminder>.*?</system-reminder>|--- BEGIN `_ai/oracle_index.md`.*?--- END router ---", re.S)


def _role_content(entry):
    msg = entry.get("message")  # Claude Code, omp, pi
    if isinstance(msg, dict):
        return msg.get("role"), msg.get("content")
    payload = entry.get("payload")  # Codex rollout
    if entry.get("type") == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
        return payload.get("role"), payload.get("content")
    return None, None


def load_dialogue(path: Path) -> str:
    """User/assistant prose only; tool calls/results and injected context dropped.

    A file with no recognizable JSONL turns is taken as a plain-text transcript.
    """
    raw = path.read_text(errors="replace")
    turns, parsed_any = [], False
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        parsed_any = True
        role, content = _role_content(entry)
        if role not in ("user", "assistant"):
            continue
        text = _INJECTED.sub("", _text_of(content)).strip()
        if text and not text.startswith(_INJECTED_PREFIXES):
            turns.append(f"### {role.upper()}\n{text}")
    if turns:
        dialogue = "\n\n".join(turns)
    elif parsed_any:
        dialogue = ""
    else:
        dialogue = raw.strip()
    return dialogue[-TRANSCRIPT_CHARS:]


# ---------------------------------------------------------------- vault facts

def allowed_tags(vault: Path) -> set[str]:
    return set(re.findall(r"`([a-z0-9_-]+/[a-z0-9_/-]+)`", (vault / "_ai/tag_map.md").read_text()))


def bm25(query: str) -> list[dict]:
    """Top hits; builds the index on first use and rebuilds once if it is missing or stale."""
    script = str(SCRIPTS / "vault-bm25.py")

    def ask():
        return subprocess.run([sys.executable, script, "query", "--top", "3", query], capture_output=True, text=True)

    out = ask()
    if out.returncode != 0:
        if subprocess.run([sys.executable, script, "build"], capture_output=True).returncode != 0:
            return []  # nothing indexable (empty vault): nothing can already cover it
        out = ask()
        out.check_returncode()
    return json.loads(out.stdout)


def llm_json(prompt: str):
    return complete_json(prompt)


EXTRACT_PROMPT = """You curate an Obsidian knowledge vault ({lang} body, English keywords).
Below are (1) the vault's router listing every existing note with its keywords,
(2) the allowed tag vocabulary, (3) a finished agent session transcript.

Extract at most {max_notes} pieces of DURABLE knowledge from the session that the
vault does NOT already cover: confirmed diagnoses/root causes, decisions with
their reason, verified code facts (file:line, symbols), ticket context, reusable
procedures, user conventions/corrections. Skip: status chatter, one-off checks,
anything the router already covers, unverified speculation, secrets/tokens.
If nothing qualifies, return {{"notes": []}}. Most sessions yield 0-1 notes.

Return ONLY a JSON object:
{{"notes": [{{
  "folder": one of {folders},
  "slug": "snake_case filename without .md (tickets: ticket_<KEY> e.g. ticket_ESE123)",
  "title": "short title",
  "aliases": ["..."], "keywords": ["search words in English and {lang}"],
  "tags": [only from allowed vocabulary],
  "type": "source|entity|concept",
  "summary": "one-line {lang} description for the catalog",
  "query": "BM25 query (English keywords) to find an existing note on the same topic",
  "body": "{lang} markdown body: facts with evidence (file:line, commands, outputs seen in the session); mark unverified claims with a leading ❓. No frontmatter."
}}]}}

=== (1) ROUTER ===
{router}

=== (2) ALLOWED TAGS ===
{tags}

=== (3) SESSION TRANSCRIPT ===
{dialogue}
"""

DEDUPE_PROMPT = """Decide whether a candidate note is already covered by existing vault notes.
"covered" = the existing notes already state the candidate's key facts. "new" = the
candidate adds a substantive fact/decision absent from them.
Return ONLY {{"verdict": "new"|"covered", "reason": "..."}}.

=== CANDIDATE ===
{candidate}

=== EXISTING NOTES (BM25 top hits) ===
{existing}
"""


def is_covered(cand: dict) -> tuple[bool, str]:
    hits = bm25(cand["query"] or cand["title"])
    if not hits:
        return False, "no BM25 hits"
    existing = "\n\n".join(
        f"--- {h['path']} ---\n{Path(h['absolute_path']).read_text(errors='replace')[:5000]}" for h in hits
    )
    verdict = llm_json(DEDUPE_PROMPT.format(
        candidate=json.dumps({k: cand[k] for k in ("title", "body")}, ensure_ascii=False), existing=existing,
    ))
    return verdict.get("verdict") != "new", f"{verdict.get('verdict')}: {verdict.get('reason', '')} (vs {[h['path'] for h in hits]})"


def validate(cand: dict, vault: Path, tags: set[str], folders: tuple[str, ...]) -> str | None:
    """Return a rejection reason, or None if the candidate is writable."""
    for key in ("folder", "slug", "title", "summary", "body", "type"):
        if not isinstance(cand.get(key), str) or not cand[key].strip():
            return f"missing {key}"
    if cand["folder"] not in folders:
        return f"folder {cand['folder']!r} not writable"
    if not SLUG_RE.match(cand["slug"]):
        return f"bad slug {cand['slug']!r}"
    if (vault / cand["folder"] / f"{cand['slug']}.md").exists():
        return "note already exists (never overwrite)"
    if any(vault.rglob(f"{cand['slug']}.md")):
        return "slug collides with an existing note elsewhere"
    bad = [t for t in cand.get("tags", []) if t not in tags]
    if bad:
        log(f"dropping unregistered tags {bad} from {cand['slug']}")
    cand["tags"] = [t for t in cand.get("tags", []) if t in tags]
    if not cand["tags"]:
        return "no registered tags"
    return None


# ---------------------------------------------------------------- writes

def _ylist(items) -> str:
    return "[" + ", ".join(json.dumps(str(i), ensure_ascii=False) for i in items) + "]"


def render_note(cand: dict, source: str, today: str) -> str:
    return (
        "---\n"
        f"title: {json.dumps(cand['title'], ensure_ascii=False)}\n"
        f"aliases: {_ylist(cand.get('aliases', []))}\n"
        f"tags: {_ylist(cand['tags'])}\n"
        f"keywords: {_ylist(cand.get('keywords', []))}\n"
        "related: []\n"
        f"type: {cand['type']}\n"
        "status: seed\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"sources: {_ylist([source])}\n"
        "---\n\n"
        "> [!note] Auto-captured (vault-capture) from a session transcript; unreviewed seed.\n\n"
        f"{cand['body'].strip()}\n"
    )


def add_catalog_rows(oracle: Path, rows: list[tuple[str, str]]) -> None:
    """rows: (folder, table row). Append under `### <folder>` in ALL NOTES, creating it if absent."""
    lines = oracle.read_text().splitlines()
    if not any(l.startswith("## ALL NOTES") for l in lines):
        raise RuntimeError(f"{oracle} has no '## ALL NOTES' section; refusing to guess catalog layout")
    for folder, row in rows:
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == f"### {folder}")
            end = start + 1
            while end < len(lines) and not lines[end].startswith("#"):
                end += 1
            last_row = max(i for i in range(start, end) if lines[i].startswith("|"))
            lines.insert(last_row + 1, row)
        except (StopIteration, ValueError):
            lines += ["", f"### {folder}", "| Note | Path | Description | keywords |", "|---|---|---|---|", row]
    oracle.write_text("\n".join(lines) + "\n")


def apply(vault: Path, notes: list[dict], source: str) -> list[str]:
    now = _now()
    today = now.strftime("%Y-%m-%d")
    created, rows = [], []
    for cand in notes:
        rel = f"{cand['folder']}/{cand['slug']}.md"
        path = vault / rel
        with open(path, "x") as f:  # "x": never overwrite, even on a race
            f.write(render_note(cand, source, today))
        created.append(rel)
        kw = ", ".join(cand.get("keywords", [])[:6]).replace("|", "/")
        rows.append((cand["folder"], f"| [[{cand['slug']}]] | {rel} | {cand['summary'].replace('|', '/')} | {kw} |"))
    add_catalog_rows(vault / "_ai/oracle_index.md", rows)
    entry = (
        f"\n### {now:%Y-%m-%d %H:%M} {now:%Z} | capture | auto-capture: "
        f"{'; '.join(c['title'] for c in notes)} | {len(created) + 1} pages\n"
        f"- Created: {', '.join(created)}\n"
        f"- Updated: _ai/oracle_index.md (ALL NOTES: {len(rows)} row(s))\n"
        f"- Sources: {source}\n"
    )
    with open(vault / "_ai/log.md", "a") as f:
        f.write(entry)
    subprocess.run([sys.executable, str(SCRIPTS / "vault-bm25.py"), "build"], check=True, capture_output=True)
    return created


# ---------------------------------------------------------------- run

def run(transcript: Path, session_id: str, harness: str, dry_run: bool, force: bool) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "capture.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # serialize captures: catalog/log edits are read-modify-write

    state_file = STATE_DIR / f"{harness}-{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)}.json"
    size = transcript.stat().st_size
    if not force and state_file.exists() and json.loads(state_file.read_text()).get("size") == size:
        log(f"{harness}:{session_id} unchanged since last capture; skip")
        return 0

    dialogue = load_dialogue(transcript)
    vault = load_vault_dir()
    os.environ["VAULT_SYNC_DIR"] = str(vault)  # child scripts must act on the vault we resolved
    folders = writable_folders(vault)
    source = f"session {harness}:{session_id} ({transcript})"
    result = {"session": source, "dry_run": dry_run, "accepted": [], "rejected": [], "created": []}
    if len(dialogue) < MIN_DIALOGUE_CHARS:
        result["rejected"].append({"reason": f"dialogue too short ({len(dialogue)} chars)"})
    else:
        tags = allowed_tags(vault)
        extracted = llm_json(EXTRACT_PROMPT.format(
            max_notes=MAX_NOTES, lang=LANG, folders=list(folders), tags=" ".join(sorted(tags)),
            router=(vault / "_ai/oracle_index.md").read_text(), dialogue=dialogue,
        ))
        for cand in extracted.get("notes", [])[:MAX_NOTES]:
            reason = validate(cand, vault, tags, folders)
            if reason is None:
                covered, why = is_covered(cand)
                reason = f"already covered — {why}" if covered else None
                if not covered:
                    cand["dedupe"] = why
            if reason:
                result["rejected"].append({"slug": cand.get("slug"), "reason": reason})
            else:
                result["accepted"].append(cand)
        if result["accepted"] and not dry_run:
            result["created"] = apply(vault, result["accepted"], source)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not dry_run:
        state_file.write_text(json.dumps({"size": size, "at": _now().isoformat(),
                                          "created": result["created"]}))
    return 0


SESSION_GLOBS = {  # where each known agent keeps session transcripts
    "claude": ".claude/projects/*/*.jsonl",
    "omp": ".omp/agent/sessions/*/*.jsonl",
    "pi": ".pi/agent/sessions/*/*.jsonl",
    "codex": ".codex/sessions/*/*/*/rollout-*.jsonl",
}


def latest_session() -> tuple[Path, str] | None:
    """Newest session file across known agents, with its harness label."""
    return max(((p, name) for name, pattern in SESSION_GLOBS.items() for p in Path.home().glob(pattern)),
               key=lambda t: t[0].stat().st_mtime, default=None)


def detach(transcript: str, session_id: str, harness: str) -> None:
    """Hooks must return fast; the capture (two model calls) runs in the background."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "capture.log", "a") as out:
        subprocess.Popen(
            [sys.executable, __file__, "run", "--transcript", transcript, "--session-id", session_id, "--harness", harness],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )


def main() -> int:
    if os.environ.get(GUARD_ENV):
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    hook = sub.add_parser("hook")
    hook.add_argument("harness")
    hook.add_argument("transcript", nargs="?", help="default: newest session of any known agent")
    hook.add_argument("session_id", nargs="?")
    r = sub.add_parser("run")
    r.add_argument("--transcript", type=Path, help="default: newest session of any known agent")
    r.add_argument("--session-id", help="default: transcript file stem")
    r.add_argument("--harness", default="generic")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--force", action="store_true", help="ignore the per-session state file")
    args = ap.parse_args()

    if args.cmd == "run":
        transcript, harness = args.transcript, args.harness
        if transcript is None:
            auto = latest_session()
            if auto is None:
                log("no transcript given and no known agent session found")
                return 1
            transcript = auto[0]
            harness = auto[1] if harness == "generic" else harness
        return run(transcript, args.session_id or transcript.stem, harness, args.dry_run, args.force)

    if args.harness == "claude":
        payload = json.load(sys.stdin)
        # idle_prompt is the only Notification type that means "user went idle".
        if payload.get("hook_event_name") == "Notification" and payload.get("notification_type", "idle_prompt") != "idle_prompt":
            return 0
        transcript, session_id = payload.get("transcript_path"), payload.get("session_id")
    else:
        transcript, session_id = args.transcript, args.session_id
        if not transcript and (auto := latest_session()):
            transcript = str(auto[0])
        session_id = session_id or (Path(transcript).stem if transcript else None)
    if not transcript or not session_id or not Path(transcript).is_file():
        log(f"hook {args.harness}: no transcript ({transcript!r}); skip")
        return 0
    detach(transcript, session_id, args.harness)
    return 0


if __name__ == "__main__":
    sys.exit(main())
