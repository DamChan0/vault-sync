#!/usr/bin/env python3
"""vault-review.py — resurfacing: list managed notes whose review is due.

Managed notes (top-level numbered folders, NN_name) carry `next_review: YYYY-MM-DD`
and optionally `review_every: <N>d|w|m` in frontmatter. Without `review_every`
the interval comes from `status`: seed 2w, developing 1m, stable 3m;
`superseded` is never scheduled.

Default mode is READ-ONLY: list notes with next_review <= today.
Writes happen only with an explicit flag:
  --bump NOTE...  mark NOTE(s) reviewed: next_review = today + interval
  --init          schedule notes that have no next_review yet
                  (updated + interval; if already past, staggered over the
                  next 14 days so the first run is not a flood)

Usage:
  vault-review.py [--vault PATH] [--today YYYY-MM-DD] [--top N] [--summary]
  vault-review.py --bump NOTE [NOTE...]     (NOTE = stem or vault-relative path)
  vault-review.py --init
"""
import argparse
import calendar
import re
import sys
import zlib
from datetime import date, timedelta
from pathlib import Path

import vault_config

MANAGED_DIR_RE = re.compile(r"^\d\d_")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.S)
DEFAULT_EVERY = {"seed": "2w", "developing": "1m", "stable": "3m"}
INTERVAL_RE = re.compile(r"^(\d+)\s*([dwm])$")
INIT_STAGGER_DAYS = 14


def parse_interval(text):
    match = INTERVAL_RE.match(text.strip().lower())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def add_interval(start, interval):
    count, unit = interval
    if unit == "d":
        return start + timedelta(days=count)
    if unit == "w":
        return start + timedelta(weeks=count)
    month_index = start.month - 1 + count
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def interval_days(interval):
    return (add_interval(date(2000, 1, 1), interval) - date(2000, 1, 1)).days


def field(frontmatter, key):
    match = re.search(rf"^{key}:[ \t]*(.*?)[ \t]*$", frontmatter, re.M)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def parse_date(text):
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class Note:
    def __init__(self, vault, path):
        self.path = path
        self.rel = path.relative_to(vault).as_posix()
        self.text = path.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(self.text)
        self.frontmatter = match.group(1) if match else None
        self.title = self.stem = path.stem
        self.status = self.every = None
        self.next_review = self.updated = self.created = None
        if self.frontmatter is None:
            return
        self.title = field(self.frontmatter, "title") or path.stem
        self.status = (field(self.frontmatter, "status") or "developing").lower()
        self.every = field(self.frontmatter, "review_every")
        self.next_review = parse_date(field(self.frontmatter, "next_review"))
        self.updated = parse_date(field(self.frontmatter, "updated"))
        self.created = parse_date(field(self.frontmatter, "created"))

    def interval(self):
        if self.every:
            parsed = parse_interval(self.every)
            if parsed:
                return parsed
            print(f"WARN: {self.rel}: bad review_every {self.every!r}, using status default", file=sys.stderr)
        spec = DEFAULT_EVERY.get(self.status, None if self.status == "superseded" else DEFAULT_EVERY["developing"])
        return parse_interval(spec) if spec else None

    def set_next_review(self, value):
        iso = value.isoformat()
        if re.search(r"^next_review:", self.frontmatter, re.M):
            new_fm = re.sub(r"^next_review:.*$", f"next_review: {iso}", self.frontmatter, count=1, flags=re.M)
        elif re.search(r"^updated:.*$", self.frontmatter, re.M):
            new_fm = re.sub(r"^(updated:.*)$", rf"\1\nnext_review: {iso}", self.frontmatter, count=1, flags=re.M)
        else:
            new_fm = self.frontmatter + f"\nnext_review: {iso}"
        self.text = self.text.replace(self.frontmatter, new_fm, 1)
        self.frontmatter = new_fm
        self.path.write_text(self.text, encoding="utf-8")
        self.next_review = value


def load_notes(vault):
    notes = []
    for folder in sorted(vault.iterdir()):
        if folder.is_dir() and MANAGED_DIR_RE.match(folder.name):
            for path in sorted(folder.rglob("*.md")):
                notes.append(Note(vault, path))
    return notes


def resolve(notes, names):
    chosen = []
    for name in names:
        hits = [n for n in notes if n.stem == name or n.rel == name or n.rel == name + ".md"]
        if len(hits) != 1:
            print(f"ERR: {name!r} matches {len(hits)} managed notes", file=sys.stderr)
            sys.exit(1)
        chosen.append(hits[0])
    return chosen


def due_notes(notes, today):
    due = [n for n in notes if n.next_review and n.next_review <= today]
    return sorted(due, key=lambda n: (n.next_review, n.rel))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vault")
    parser.add_argument("--today", help="Override today (YYYY-MM-DD), for testing.")
    parser.add_argument("--top", type=int, default=0, help="Show only the N most overdue.")
    parser.add_argument("--summary", action="store_true", help="One line: due count + top 5 titles.")
    parser.add_argument("--bump", nargs="+", metavar="NOTE", help="Mark notes reviewed (writes).")
    parser.add_argument("--init", action="store_true", help="Schedule unscheduled notes (writes).")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    vault = vault_config.load_vault_dir(args.vault)
    notes = load_notes(vault)

    if args.bump:
        for note in resolve(notes, args.bump):
            interval = note.interval()
            if note.frontmatter is None or interval is None:
                print(f"SKIP {note.rel}: no frontmatter or unschedulable status {note.status!r}")
                continue
            note.set_next_review(add_interval(today, interval))
            print(f"bumped {note.rel}: next_review {note.next_review.isoformat()}")
        return 0

    if args.init:
        count = 0
        for note in notes:
            if note.frontmatter is None or note.next_review:
                continue
            interval = note.interval()
            if interval is None:
                continue
            base = note.updated or note.created or today
            target = add_interval(base, interval)
            if target < today:
                stagger = zlib.crc32(note.rel.encode("utf-8")) % INIT_STAGGER_DAYS
                target = today + timedelta(days=stagger)
            note.set_next_review(target)
            count += 1
        print(f"scheduled {count} notes")
        return 0

    due = due_notes(notes, today)
    unscheduled = [n for n in notes if n.next_review is None and n.status != "superseded"]
    if args.summary:
        if not due:
            print("vault review: 0 due")
        else:
            titles = "; ".join(n.title for n in due[:5])
            print(f"vault review: {len(due)} due — {titles}")
        return 0

    shown = due[: args.top] if args.top else due
    print(f"# Due reviews as of {today.isoformat()}: {len(due)} (of {len(notes)} managed notes)")
    for note in shown:
        late = (today - note.next_review).days
        print(f"- {note.next_review.isoformat()} (+{late}d) [{note.status}] {note.rel} — {note.title}")
    if unscheduled:
        print(f"# Unscheduled (no next_review): {len(unscheduled)} — run --init to schedule")
    return 0


if __name__ == "__main__":
    sys.exit(main())
