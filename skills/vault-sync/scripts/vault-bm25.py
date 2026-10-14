#!/usr/bin/env python3
"""vault-bm25.py — BM25 retrieval over the Obsidian LLM-wiki notes.

Adapted from AgriciDaniel/claude-obsidian scripts/bm25-index.py (MIT).
Differences from upstream: indexes whole notes (they are atomic) instead of
contextualized chunks, no rerank/ollama stage, Hangul character-bigram
tokenization for Korean recall.

Index lives at <vault>/.vault-meta/bm25/index.json (dot-hidden from Obsidian).

Usage:
  vault-bm25.py build
  vault-bm25.py query "Mixamo Skeleton3D 진행 상황" [--top 5]
  vault-bm25.py stats

Exit codes: 0 ok / 1 lock failed / 3 index missing, corrupt, or empty build
"""

import argparse
import fcntl
import json
import math
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tests load this file by path
import vault_config  # noqa: E402

# Resolved once in main() via init_vault(): the vault differs per machine
# (vault-sync profile) and the folder scope differs per vault (_ai/CLAUDE.md
# documents each vault's own knowledge/do-not-touch folders), so neither can
# be a literal baked into this file.
VAULT_ROOT = None
VAULT_ROOT_RESOLVED = None
MANAGED_DIRS = None
EXCLUDED_PARTS = vault_config.CONTROL_PLANE_DIRS
META_DIR = None
BM25_DIR = None
INDEX_PATH = None
LOCK_PATH = None


def init_vault(vault_override=None, exclude=()):
    """Resolve vault root, indexed folders, and derived paths for this run."""
    global VAULT_ROOT, VAULT_ROOT_RESOLVED, MANAGED_DIRS, EXCLUDED_PARTS
    global META_DIR, BM25_DIR, INDEX_PATH, LOCK_PATH
    VAULT_ROOT = vault_config.load_vault_dir(vault_override)
    VAULT_ROOT_RESOLVED = VAULT_ROOT.resolve()
    EXCLUDED_PARTS = vault_config.CONTROL_PLANE_DIRS | {n.strip() for n in exclude if n.strip()}
    MANAGED_DIRS = vault_config.default_indexed_dirs(VAULT_ROOT, exclude=exclude)
    META_DIR = VAULT_ROOT / ".vault-meta"
    BM25_DIR = META_DIR / "bm25"
    INDEX_PATH = BM25_DIR / "index.json"
    LOCK_PATH = META_DIR / ".bm25.lock"


# Optional: a directory of session spec/diagnosis/handoff notes kept outside the
# vault, set via $VAULT_SYNC_WORK_NOTES (e.g. ~/work-notes). Indexed read-only
# under this prefix so hits are visibly not vault notes and nothing is copied in.
_work_notes_env = os.environ.get("VAULT_SYNC_WORK_NOTES")
WORK_NOTES_ROOT = Path(os.path.expanduser(_work_notes_env)) if _work_notes_env else None
EXTERNAL_PREFIX = "work-notes:"


def is_external(rel):
    return rel.startswith(EXTERNAL_PREFIX)


def doc_path(rel):
    if is_external(rel):
        return WORK_NOTES_ROOT / rel[len(EXTERNAL_PREFIX):]
    return VAULT_ROOT / rel

K1 = 1.5
B = 0.75
FRONTMATTER_BOOST = 3  # title/aliases/keywords repeated this many times

STOPWORDS = frozenset("""
a an and are as at be by for from has have he her him his i if in is it its
of on or that the their them they this to was were will with you your
""".split())

TOKEN_RE = re.compile(r"\w[\w'\-]*", re.UNICODE)
HANGUL_RE = re.compile(r"[가-힣]{2,}")

EXIT_OK, EXIT_LOCK, EXIT_INDEX_MISSING = 0, 1, 3
MAX_TOP = 20
MAX_NOTE_BYTES = 16 * 1024 * 1024


def log(msg):
    print(msg, file=sys.stderr)


def tokenize(text):
    """Lowercase word tokens + character bigrams for Hangul runs.

    Korean eojeol carry attached particles ("사운드가" vs "사운드"), so exact
    word match misses; bigrams of each Hangul run recover recall.
    """
    tokens = []
    for t in TOKEN_RE.findall(text):
        low = t.lower()
        if low in STOPWORDS or len(low) < 2:
            continue
        tokens.append(low)
        for run in HANGUL_RE.findall(low):
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def read_note_text(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptor = None
    directory_fd = None
    try:
        if len(path.parts) == 1:
            descriptor = os.open(path, flags)
        else:
            directory_fd = os.open(path.anchor, directory_flags)
            for part in path.parts[1:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            descriptor = os.open(path.parts[-1], flags, dir_fd=directory_fd)
        info = os.fstat(descriptor)
        if info.st_size > MAX_NOTE_BYTES:
            raise OSError(f"note exceeds {MAX_NOTE_BYTES} byte limit")
        if not stat.S_ISREG(info.st_mode):
            raise OSError("note is not a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_NOTE_BYTES + 1)
            if len(raw) > MAX_NOTE_BYTES:
                raise OSError(f"note exceeds {MAX_NOTE_BYTES} byte limit")
            return raw.decode("utf-8")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def parse_note_parts(path):
    """Return title, boosted scoring text, and unmodified body with its line offset."""
    raw = read_note_text(path)
    title = path.stem
    frontmatter = ""
    body = raw
    body_start_line = 1
    m = re.match(r"^---\n(.*?)\n---\n?", raw, re.S)
    if m:
        frontmatter, body = m.group(1), raw[m.end():]
        body_start_line = raw[:m.end()].count("\n") + 1
        tm = re.search(r"^title:\s*(.+)$", frontmatter, re.M)
        if tm:
            title = tm.group(1).strip().strip('"')
    fields = []
    for key in ("title", "aliases", "keywords", "tags"):
        km = re.search(rf"^{key}:\s*(.+)$", frontmatter, re.M)
        if km:
            fields.append(km.group(1))
    boosted = ("\n".join(fields) + "\n") * FRONTMATTER_BOOST
    return title, boosted + body, body, body_start_line


def parse_note(path):
    """Return the legacy (title, scoring text) interface used by index builds."""
    title, scoring_text, _, _ = parse_note_parts(path)
    return title, scoring_text


def discover_notes():
    seen = set()
    excluded_lower = {part.lower() for part in EXCLUDED_PARTS}
    for directory in MANAGED_DIRS:
        root = VAULT_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(VAULT_ROOT)
            if (
                path.is_symlink()
                or any(part.startswith(".") for part in rel.parts)
                or any(
                    Path(part).name.lower() in excluded_lower
                    for part in rel.parts[:-1]
                )
            ):
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError as error:
                log(f"  skip {rel}: {error}")
                continue
            if VAULT_ROOT_RESOLVED not in resolved.parents:
                log(f"  skip {rel}: symlink escapes vault")
                continue
            if resolved in seen:
                log(f"  skip {rel}: duplicate note")
                continue
            seen.add(resolved)
            yield str(rel), path
    yield from discover_work_notes()


def discover_work_notes():
    if WORK_NOTES_ROOT is None or not WORK_NOTES_ROOT.is_dir():
        return
    for path in sorted(WORK_NOTES_ROOT.rglob("*.md")):
        rel = path.relative_to(WORK_NOTES_ROOT)
        if path.is_symlink() or any(part.startswith(".") for part in rel.parts):
            continue
        yield EXTERNAL_PREFIX + rel.as_posix(), path


def is_current_indexed_note(rel):
    if is_external(rel):
        if WORK_NOTES_ROOT is None:  # external notes disabled; entries from an older index are stale
            return False
        rel_path = Path(rel[len(EXTERNAL_PREFIX):])
        path = WORK_NOTES_ROOT / rel_path
        return (
            bool(rel_path.parts)
            and not rel_path.is_absolute()
            and ".." not in rel_path.parts
            and not path.is_symlink()
            and path.is_file()
        )
    path = VAULT_ROOT / rel
    excluded_lower = {part.lower() for part in EXCLUDED_PARTS}
    rel_path = Path(rel)
    if (
        rel_path.is_absolute()
        or ".." in rel_path.parts
        or not rel_path.parts
        or rel_path.parts[0] not in MANAGED_DIRS
        or path.is_symlink()
        or not path.is_file()
        or any(part.startswith(".") for part in rel_path.parts)
        or any(Path(part).name.lower() in excluded_lower for part in rel_path.parts[:-1])
    ):
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return VAULT_ROOT_RESOLVED in resolved.parents


def acquire_lock():
    META_DIR.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_CREAT
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = None
    try:
        fd = os.open(str(LOCK_PATH), flags, 0o644)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("bm25 lock is not a regular file")
    except OSError as error:
        if fd is not None:
            os.close(fd)
        log(f"ERR: could not open bm25 lock: {error}")
        sys.exit(EXIT_LOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        log("ERR: could not acquire bm25 lock")
        sys.exit(EXIT_LOCK)
    return fd


def build_index():
    docs, df, postings = {}, Counter(), defaultdict(list)
    for rel, path in discover_notes():
        try:
            title, text = parse_note(path)
        except (OSError, UnicodeError) as e:
            log(f"  skip {rel}: {e}")
            continue
        tokens = tokenize(text)
        docs[rel] = {"title": title, "dl": len(tokens)}
        for term, count in Counter(tokens).items():
            df[term] += 1
            postings[term].append([rel, count])
    if not docs:
        log("WARN: no notes indexed")
        return None
    return {
        "schema_version": 1,
        "params": {"k1": K1, "b": B},
        "doc_count": len(docs),
        "avg_dl": sum(d["dl"] for d in docs.values()) / len(docs),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vocab": {t: {"df": df[t], "postings": postings[t]} for t in sorted(df)},
        "docs": docs,
    }


def write_index(index):
    BM25_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(f".{os.getpid()}.tmp")
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = None
    created = False
    try:
        descriptor = os.open(tmp, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(index, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, INDEX_PATH)
        try:
            directory_fd = os.open(
                BM25_DIR,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            print(f"WARN: index directory sync failed: {error}", file=sys.stderr)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            tmp.unlink(missing_ok=True)


def validate_index(index):
    try:
        if type(index["schema_version"]) is not int or index["schema_version"] != 1:
            raise ValueError("unsupported schema")
        docs = index["docs"]
        vocab = index["vocab"]
        params = index["params"]
        if not isinstance(docs, dict) or not docs:
            raise ValueError("docs must be a non-empty object")
        if type(index["doc_count"]) is not int or index["doc_count"] != len(docs):
            raise ValueError("doc_count does not match docs")
        if (
            type(index["avg_dl"]) not in (int, float)
            or not math.isfinite(index["avg_dl"])
            or index["avg_dl"] < 0
        ):
            raise ValueError("avg_dl must be finite and non-negative")
        datetime.strptime(index["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
        if not isinstance(params, dict) or not all(
            type(params[key]) in (int, float) and math.isfinite(params[key])
            for key in ("k1", "b")
        ) or params["k1"] <= 0 or not 0 <= params["b"] <= 1:
            raise ValueError("invalid BM25 params")
        if not isinstance(vocab, dict):
            raise ValueError("vocab must be an object")
        active_rel_paths = {
            rel for rel in docs
            if isinstance(rel, str) and is_current_indexed_note(rel)
        }
        for rel, doc in docs.items():
            rel_path = Path(rel) if isinstance(rel, str) else None
            if (
                not isinstance(rel, str)
                or rel_path.is_absolute()
                or ".." in rel_path.parts
                or not isinstance(doc, dict)
            ):
                raise ValueError("invalid or disallowed docs entry")
            if (
                rel in active_rel_paths
                and (
                    not isinstance(doc.get("title"), str)
                    or type(doc.get("dl")) is not int
                    or doc["dl"] < 0
                )
            ):
                raise ValueError("invalid document metadata")
        for term, value in vocab.items():
            if not isinstance(term, str) or not isinstance(value, dict):
                raise ValueError("invalid vocab entry")
            postings = value.get("postings")
            df = value.get("df")
            if (
                type(df) is not int
                or not isinstance(postings, list)
                or df <= 0
                or df > index["doc_count"]
                or df != len(postings)
            ):
                raise ValueError("invalid postings metadata")
            for posting in postings:
                if (
                    not isinstance(posting, list)
                    or len(posting) != 2
                    or posting[0] not in docs
                    or type(posting[1]) is not int
                    or posting[1] <= 0
                ):
                    raise ValueError("invalid posting")
            if len({posting[0] for posting in postings}) != len(postings):
                raise ValueError("duplicate posting")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid index schema: {error}") from error

    active_docs = {
        rel: doc for rel, doc in docs.items() if rel in active_rel_paths
    }
    if not active_docs:
        raise ValueError("no indexed documents remain in vault")
    if len(active_docs) != len(docs):
        log(f"WARN: ignoring {len(docs) - len(active_docs)} missing or excluded indexed documents")
    active_vocab = {}
    for term, value in vocab.items():
        postings = [posting for posting in value["postings"] if posting[0] in active_docs]
        if postings:
            active_vocab[term] = {"df": len(postings), "postings": postings}
    return {
        **index,
        "doc_count": len(active_docs),
        "avg_dl": sum(doc["dl"] for doc in active_docs.values()) / len(active_docs),
        "vocab": active_vocab,
        "docs": active_docs,
    }


def load_index():
    if not INDEX_PATH.is_file():
        log(f"ERR: no index at {INDEX_PATH}. Run `vault-bm25.py build` first.")
        sys.exit(EXIT_INDEX_MISSING)
    try:
        index = json.loads(read_note_text(INDEX_PATH))
        return validate_index(index)
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as error:
        log(f"ERR: index corrupt: {error}")
        sys.exit(EXIT_INDEX_MISSING)


def clip_body_excerpt(text, query_terms, max_chars):
    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    positions = [position for term in query_terms if (position := lowered.find(term)) >= 0]
    if not positions:
        return text[:max_chars - 1].rstrip() + "…"
    budget = max(1, max_chars - 2)
    center = min(positions)
    start = max(0, center - budget // 3)
    end = min(len(text), start + budget)
    start = max(0, end - budget)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def body_excerpt(rel, query_text, max_chars=400):
    """Return a query-focused literal body excerpt and original Markdown lines."""
    try:
        _, _, body, body_start_line = parse_note_parts(doc_path(rel))
    except (OSError, UnicodeError):
        return "", None, None, "body_unavailable"
    query_terms = set(tokenize(query_text))
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    best_index, best_score = None, 0
    for index, line in enumerate(lines):
        score = len(set(tokenize(line)) & query_terms)
        if score > best_score:
            best_index, best_score = index, score
    if best_index is None:
        return "", None, None, "metadata"
    first = max(0, best_index - 1)
    last = min(len(lines), best_index + 2)
    excerpt = "\n".join(lines[first:last]).strip()
    if len(excerpt) > max_chars:
        return (
            clip_body_excerpt(lines[best_index], query_terms, max_chars),
            body_start_line + best_index,
            body_start_line + best_index,
            "body",
        )
    return excerpt, body_start_line + first, body_start_line + last - 1, "body"


def query(text, top_k=5):
    top_k = max(0, min(top_k, MAX_TOP))
    idx = load_index()
    vocab, docs = idx["vocab"], idx["docs"]
    k1, b = idx["params"]["k1"], idx["params"]["b"]
    N, avg_dl = idx["doc_count"], idx["avg_dl"] or 1.0
    scores = defaultdict(float)
    for term in set(tokenize(text)):
        v = vocab.get(term)
        if not v:
            continue
        idf = math.log(1 + (N - v["df"] + 0.5) / (v["df"] + 0.5))
        for rel, cnt in v["postings"]:
            dl = docs[rel]["dl"]
            scores[rel] += idf * (cnt * (k1 + 1)) / (cnt + k1 * (1 - b + b * dl / avg_dl))
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    results = []
    for rel, score in ranked:
        excerpt, line_start, line_end, match_kind = body_excerpt(rel, text)
        results.append({
            "source": "work-notes" if is_external(rel) else "vault",
            "path": rel,
            "absolute_path": str(doc_path(rel)),
            "title": docs[rel]["title"],
            "score": round(score, 4),
            "snippet": excerpt,
            "line_start": line_start,
            "line_end": line_end,
            "match_kind": match_kind,
        })
    return results


def main():
    parser = argparse.ArgumentParser(description="BM25 retrieval over the vault.")
    parser.add_argument("--vault", help="Vault root override (default: $VAULT_SYNC_DIR or ~/.config/vault-sync/profile.json).")
    parser.add_argument(
        "--exclude", default="",
        help="Comma-separated top-level folder names to keep out of the index "
             "(the do-not-touch/personal folders documented in _ai/CLAUDE.md).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    qp = sub.add_parser("query", help="Search the bounded BM25 index.")
    qp.add_argument("text")
    qp.add_argument("--top", type=int, default=5, help=f"Maximum results, 0..{MAX_TOP} (default: 5).")
    sub.add_parser("stats")
    args = parser.parse_args()
    if args.cmd == "query" and not 0 <= args.top <= MAX_TOP:
        parser.error(f"--top must be between 0 and {MAX_TOP}")
    if VAULT_ROOT is None or args.vault or args.exclude:  # tests pre-set the globals
        init_vault(args.vault, exclude=args.exclude.split(","))

    if args.cmd == "build":
        fd = acquire_lock()
        try:
            index = build_index()
            if index is None:
                log("ERR: no notes indexed; existing index left unchanged")
                return EXIT_INDEX_MISSING
            write_index(index)
            log(f"Wrote {INDEX_PATH}  docs={index['doc_count']}  vocab={len(index['vocab'])}")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return EXIT_OK
    if args.cmd == "query":
        print(json.dumps(query(args.text, top_k=args.top), ensure_ascii=False, indent=2))
        return EXIT_OK
    if args.cmd == "stats":
        idx = load_index()
        print(json.dumps({
            "doc_count": idx["doc_count"], "vocab_size": len(idx["vocab"]),
            "avg_dl": round(idx["avg_dl"], 1), "updated_at": idx["updated_at"],
        }, indent=2))
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
