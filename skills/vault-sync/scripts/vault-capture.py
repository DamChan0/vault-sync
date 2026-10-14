#!/usr/bin/env python3
"""Idle/session-end capture: file a finished session's durable knowledge into the vault.

This is the sanctioned automatic ingest path (vault _ai/CLAUDE.md invariant 3
auto-apply grant): it only CREATES new managed notes, never overwrites, never
touches user folders, appends an ALL NOTES catalog row and a `_ai/log.md` entry
for every write, then rebuilds the BM25 index.

Entry points:
  vault-capture.py hook claude      # Claude Code hook (stdin JSON), detaches
  vault-capture.py hook omp FILE ID # omp extension, detaches
  vault-capture.py run --transcript FILE --session-id ID --harness H [--dry-run] [--force]

Recursion guard: the capture itself runs `claude -p` with VAULT_CAPTURE_RUNNING=1,
no setting sources (no hooks) and no session persistence; every entry point
exits immediately when that variable is set.
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
from vault_config import load_vault_dir  # noqa: E402

GUARD_ENV = "VAULT_CAPTURE_RUNNING"
STATE_DIR = Path.home() / ".local/state/vault-capture"
SCRIPTS = Path(__file__).resolve().parent
KST = datetime.timezone(datetime.timedelta(hours=9))
# Managed knowledge folders are the vault's numbered top-level dirs (NN_name);
# anything else (user folders, Templates, control plane) is off limits.
MANAGED_FOLDER_RE = re.compile(r"^\d\d_")


def writable_folders(vault: Path) -> tuple[str, ...]:
    return tuple(sorted(p.name for p in vault.iterdir() if p.is_dir() and MANAGED_FOLDER_RE.match(p.name)))
MAX_NOTES = 3
TRANSCRIPT_CHARS = 90_000
MIN_DIALOGUE_CHARS = 2_000
MODEL = "sonnet"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,60}$")


def log(msg: str) -> None:
    print(f"[vault-capture {datetime.datetime.now(KST):%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- transcript

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


_INJECTED = re.compile(r"<system-reminder>.*?</system-reminder>|--- BEGIN `_ai/oracle_index.md`.*?--- END router ---", re.S)


def load_dialogue(path: Path) -> str:
    """User/assistant prose only; tool calls/results and injected context dropped."""
    turns = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        text = _INJECTED.sub("", _text_of(msg.get("content"))).strip()
        if text:
            turns.append(f"### {msg['role'].upper()}\n{text}")
    dialogue = "\n\n".join(turns)
    return dialogue[-TRANSCRIPT_CHARS:]


# ---------------------------------------------------------------- vault facts

def allowed_tags(vault: Path) -> set[str]:
    return set(re.findall(r"`([a-z0-9_-]+/[a-z0-9_/-]+)`", (vault / "_ai/tag_map.md").read_text()))


def bm25(query: str) -> list[dict]:
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "vault-bm25.py"), "query", "--top", "3", query],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def claude_json(prompt: str):
    env = {**os.environ, GUARD_ENV: "1"}
    out = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--setting-sources", "", "--no-session-persistence", "--tools", ""],
        input=prompt, capture_output=True, text=True, env=env, timeout=600, cwd="/tmp",
    )
    if out.returncode != 0:
        raise RuntimeError(f"claude -p failed rc={out.returncode}: {out.stderr.strip()[:500]}")
    m = re.search(r"\{.*\}", out.stdout, re.S)
    if not m:
        raise RuntimeError(f"claude -p returned no JSON object: {out.stdout[:300]!r}")
    return json.loads(m.group(0))


EXTRACT_PROMPT = """You curate an Obsidian knowledge vault (Korean body, English keywords).
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
  "aliases": ["..."], "keywords": ["EN and KO search words"],
  "tags": [only from allowed vocabulary],
  "type": "source|entity|concept",
  "summary": "one-line Korean description for the catalog",
  "query": "BM25 query (English keywords) to find an existing note on the same topic",
  "body": "Korean markdown body: facts with evidence (file:line, commands, outputs seen in the session); mark unverified as ❓ 추정. No frontmatter."
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
    verdict = claude_json(DEDUPE_PROMPT.format(
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
        "> [!note] idle 자동 캡처 (vault-capture) — 세션 transcript에서 추출, 검토 전 seed.\n\n"
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
            lines += ["", f"### {folder}", "| 노트 | 경로 | 설명 | keywords |", "|---|---|---|---|", row]
    oracle.write_text("\n".join(lines) + "\n")


def apply(vault: Path, notes: list[dict], source: str) -> list[str]:
    now = datetime.datetime.now(KST)
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
        f"\n### {now:%Y-%m-%d %H:%M} KST | capture | idle 자동 캡처: "
        f"{'; '.join(c['title'] for c in notes)} | {len(created) + 1} pages\n"
        f"- Created: {', '.join(created)}\n"
        f"- Updated: _ai/oracle_index.md (ALL NOTES 행 {len(rows)}개)\n"
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
    folders = writable_folders(vault)
    source = f"session {harness}:{session_id} ({transcript})"
    result = {"session": source, "dry_run": dry_run, "accepted": [], "rejected": [], "created": []}
    if len(dialogue) < MIN_DIALOGUE_CHARS:
        result["rejected"].append({"reason": f"dialogue too short ({len(dialogue)} chars)"})
    else:
        tags = allowed_tags(vault)
        extracted = claude_json(EXTRACT_PROMPT.format(
            max_notes=MAX_NOTES, folders=list(folders), tags=" ".join(sorted(tags)),
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
        state_file.write_text(json.dumps({"size": size, "at": datetime.datetime.now(KST).isoformat(),
                                          "created": result["created"]}))
    return 0


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
    hook.add_argument("harness", choices=["claude", "omp"])
    hook.add_argument("transcript", nargs="?")
    hook.add_argument("session_id", nargs="?")
    r = sub.add_parser("run")
    r.add_argument("--transcript", required=True, type=Path)
    r.add_argument("--session-id", required=True)
    r.add_argument("--harness", required=True, choices=["claude", "omp"])
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--force", action="store_true", help="ignore the per-session state file")
    args = ap.parse_args()

    if args.cmd == "run":
        return run(args.transcript, args.session_id, args.harness, args.dry_run, args.force)
    if args.harness == "claude":
        payload = json.load(sys.stdin)
        # idle_prompt is the only Notification type that means "user went idle".
        if payload.get("hook_event_name") == "Notification" and payload.get("notification_type", "idle_prompt") != "idle_prompt":
            return 0
        transcript, session_id = payload.get("transcript_path"), payload.get("session_id")
    else:
        transcript, session_id = args.transcript, args.session_id
    if not transcript or not session_id or not Path(transcript).is_file():
        log(f"hook {args.harness}: no transcript ({transcript!r}); skip")
        return 0
    detach(transcript, session_id, args.harness)
    return 0


if __name__ == "__main__":
    sys.exit(main())
