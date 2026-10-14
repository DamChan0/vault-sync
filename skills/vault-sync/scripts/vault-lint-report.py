#!/usr/bin/env python3
"""vault-lint-report.py — read-only weekly lint report for the vault.

Runs the existing catalog check (vault-catalog-check.py) plus broken
wikilinks, tag drift, frontmatter gaps and stale claims over the managed
folders (numbered folders, NN_name), and adds:
  (a) missing concepts: unresolved [[wikilinks]], and frontmatter keywords
      shared by >= MIN_KEYWORD_NOTES notes that have no page of that name;
  (b) web-fill gap candidates: `[!gap]` callouts, `❓` (unverified) lines, seed notes;
and lists due reviews (vault-review.py logic).

Never writes inside the vault. Output: <out-dir>/YYYY-MM-DD.md
(default ~/.local/state/vault-sync/lint/).

Usage:
  vault-lint-report.py [--vault PATH] [--out-dir DIR] [--today YYYY-MM-DD]
"""
import argparse
import importlib.util
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import vault_config

SCRIPT_DIR = Path(__file__).resolve().parent
MANAGED_DIR_RE = re.compile(r"^\d\d_")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.S)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
CODE_FENCE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
REQUIRED_KEYS = ("type", "status", "created", "updated", "tags")
MIN_KEYWORD_NOTES = 3
STALE_WEEKS = 6
MAX_LIST = 25


def load_review_module():
    spec = importlib.util.spec_from_file_location("vault_review", SCRIPT_DIR / "vault-review.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def list_field(frontmatter, key):
    match = re.search(rf"^{key}:\s*\[(.*?)\]\s*$", frontmatter, re.M)
    if not match:
        return []
    return [x.strip().strip("\"'") for x in match.group(1).split(",") if x.strip()]


def norm(text):
    return re.sub(r"[\s_\-]+", "", text.lower())


def all_targets(vault):
    """Every note stem and alias in the vault (Obsidian resolves [[x]] by either)."""
    targets = set()
    for path in vault.rglob("*"):
        if ".git" in path.parts or ".obsidian" in path.parts or not path.is_file():
            continue
        targets.add(path.name)
        if path.suffix != ".md":
            continue
        targets.add(path.stem)
        match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            targets.update(list_field(match.group(1), "aliases"))
    return targets


def registered_tags(vault):
    tag_map = vault / "_ai" / "tag_map.md"
    if not tag_map.is_file():
        return None
    text = tag_map.read_text(encoding="utf-8")
    return set(re.findall(r"`([a-z\-]+/[a-z0-9\-]+(?:/[a-z0-9\-]+)?)`", text))


def section(title, lines, empty="None"):
    out = [f"## {title}", ""]
    out.extend(lines if lines else [f"- {empty}"])
    out.append("")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vault")
    parser.add_argument("--out-dir", default="~/.local/state/vault-sync/lint")
    parser.add_argument("--today")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    vault = vault_config.load_vault_dir(args.vault)
    review = load_review_module()

    catalog = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "vault-catalog-check.py"), "--vault", str(vault)],
        capture_output=True, text=True,
    )

    notes = []
    for folder in sorted(vault.iterdir()):
        if folder.is_dir() and MANAGED_DIR_RE.match(folder.name):
            notes.extend(sorted(folder.rglob("*.md")))

    targets = all_targets(vault)
    target_norms = {norm(t) for t in targets}
    tags_ok = registered_tags(vault)

    broken = defaultdict(list)
    tag_drift = defaultdict(list)
    fm_gaps, stale, gaps, seeds = [], [], [], []
    keyword_notes = defaultdict(set)
    stale_cutoff = today - timedelta(weeks=STALE_WEEKS)

    for path in notes:
        rel = path.relative_to(vault).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        match = FRONTMATTER_RE.match(text)
        if not match:
            fm_gaps.append(f"- `{rel}`: no frontmatter")
            continue
        fm = match.group(1)
        body = text[match.end():]
        missing = [k for k in REQUIRED_KEYS if not re.search(rf"^{k}:", fm, re.M)]
        if missing:
            fm_gaps.append(f"- `{rel}`: missing {', '.join(missing)}")
        if tags_ok is not None:
            for tag in list_field(fm, "tags"):
                if "/" in tag and tag not in tags_ok:
                    tag_drift[tag].append(rel)
        for keyword in list_field(fm, "keywords"):
            keyword_notes[keyword.lower()].add(rel)
        status = (re.search(r"^status:\s*(\S+)", fm, re.M) or [None, ""])[1]
        updated = review.parse_date((re.search(r"^updated:\s*(\S+)", fm, re.M) or [None, ""])[1])
        if status == "stable" and updated and updated < stale_cutoff:
            stale.append((updated, rel))
        if status == "seed":
            seeds.append(rel)
        for link in set(WIKILINK_RE.findall(CODE_FENCE_RE.sub("", text))):
            link = link.strip()
            if link and link not in targets:
                broken[link].append(rel)
        for number, line in enumerate(body.splitlines(), 1):
            if "[!gap]" in line or "❓" in line:
                gaps.append(f"- `{rel}` L{number}: {line.strip()[:140]}")

    # (a) repeated keywords without a page of that name
    missing_concepts = sorted(
        ((k, v) for k, v in keyword_notes.items()
         if len(v) >= MIN_KEYWORD_NOTES and norm(k) not in target_norms),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )

    notes_scanned = [review.Note(vault, p) for p in notes]
    due = review.due_notes(notes_scanned, today)
    unscheduled = [n for n in notes_scanned if n.next_review is None and n.status != "superseded"]

    out = [f"# Vault lint report — {today.isoformat()}", "",
           f"Read-only check. Scope: numbered folders (NN_name), {len(notes)} notes. Vault files are not modified.", ""]
    out += section(
        "Catalog invariant (vault-catalog-check.py)",
        [f"- exit {catalog.returncode} ({'OK' if catalog.returncode == 0 else 'violation'})"]
        + [f"  {line}" for line in (catalog.stdout + catalog.stderr).strip().splitlines()[:MAX_LIST]],
    )
    out += section(
        f"Due reviews ({len(due)}, unscheduled {len(unscheduled)})",
        [f"- {n.next_review.isoformat()} [{n.status}] `{n.rel}` — {n.title}" for n in due[:MAX_LIST]]
        + ([f"- … and {len(due) - MAX_LIST} more"] if len(due) > MAX_LIST else []),
    )
    out += section(
        f"Broken wikilinks ({len(broken)}) / (a) concept candidates without a page",
        [f"- [[{k}]] ← {', '.join(f'`{r}`' for r in sorted(v)[:3])}" for k, v in sorted(broken.items())[:MAX_LIST]],
    )
    out += section(
        f"(a) keywords repeated across notes with no dedicated page (>= {MIN_KEYWORD_NOTES} notes, top {MAX_LIST})",
        [f"- `{k}` — {len(v)} notes" for k, v in missing_concepts[:MAX_LIST]],
    )
    out += section(
        f"(b) gap candidates to fill via web/source check ([!gap]·❓ {len(gaps)} lines, {len(seeds)} seed notes)",
        gaps[:MAX_LIST] + [f"- seed: `{r}`" for r in seeds[:MAX_LIST]],
    )
    out += section(f"Stale (stable, updated > {STALE_WEEKS} weeks ago) {len(stale)}",
                   [f"- {d.isoformat()} `{r}`" for d, r in sorted(stale)[:MAX_LIST]]
                   + ([f"- … and {len(stale) - MAX_LIST} more"] if len(stale) > MAX_LIST else []))
    out += section(f"Tag drift ({len(tag_drift)})",
                   [f"- `{t}` ← {', '.join(f'`{r}`' for r in v[:3])}" for t, v in sorted(tag_drift.items())[:MAX_LIST]])
    out += section(f"Frontmatter gaps ({len(fm_gaps)})", fm_gaps[:MAX_LIST])

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{today.isoformat()}.md"
    target.write_text("\n".join(out), encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
