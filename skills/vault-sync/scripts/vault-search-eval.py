#!/usr/bin/env python3
"""Recall@k eval for vault retrieval: realistic KO/EN/synonym queries -> expected note.

Usage: vault-search-eval.py --cases cases.json [--k 3] [--backend bm25|<cmd>]
`--backend` other than bm25 is a shell command template with {q} that prints
the vault-relative paths of its ranked hits one per line (used to trial
alternative engines without wiring them in).
"""
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def bm25_hits(query, k):
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "vault-bm25.py"), "query", "--top", "20", query],
        capture_output=True, text=True, check=True,
    )
    # Vault notes only: work-notes (incl. this eval's own report) would otherwise self-match.
    return [h["path"] for h in json.loads(out.stdout) if h["source"] == "vault"][:k]


def cmd_hits(template, query, k):
    out = subprocess.run(template.format(q=shlex.quote(query)), shell=True, capture_output=True, text=True, check=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()][:k]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--backend", default="bm25")
    ap.add_argument("--cases", required=True, type=Path,
                    help='JSON file: [["query", "expected_note_stem"], ...] — phrase queries as a user would ask, not copying note keywords')
    args = ap.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    hits_total = 0
    print("| # | query | expected | hit@k | top-k |\n|---|---|---|---|---|")
    for i, (query, expected) in enumerate(cases, 1):
        hits = bm25_hits(query, args.k) if args.backend == "bm25" else cmd_hits(args.backend, query, args.k)
        ok = any(Path(h).stem == expected for h in hits)
        hits_total += ok
        print(f"| {i} | {query} | {expected} | {'✅' if ok else '❌'} | {', '.join(Path(h).stem for h in hits)} |")
    print(f"\nrecall@{args.k} = {hits_total}/{len(cases)} = {hits_total / len(cases):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
