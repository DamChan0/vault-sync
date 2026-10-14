#!/usr/bin/env python3
"""Query explicitly selected, redacted session/source files without indexing or writes.

This helper never discovers files: callers must pass every source explicitly and
may only select sources they are authorized to inspect.
"""

import argparse
import heapq
import errno
import json
import os
import re
import stat
import sys
from pathlib import Path

MAX_SOURCES = 16
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOP = 20
MAX_SNIPPET_CHARS = 500
MAX_MESSAGE_CONTEXT_LINES = 64
MAX_MESSAGE_CONTEXT_CHARS = 4096
SUPPORTED_SUFFIXES = {".txt", ".md", ".log", ".jsonl"}
MESSAGE_ROLES = {"user", "assistant", "system", "developer"}
SENSITIVE_NAME_RE = re.compile(
    r"(^|[-_.])(credential|credentials|secret|secrets|password|passwd|token|"
    r"cookie|cookies|authorization|auth|id_rsa|id_ed25519|config)([-_.]|$)|^\.env",
    re.I,
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.S,
)
SECRET_KEY_RE = (
    r"(?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|token|key|secret|password|passwd|cookie|authorization|"
    r"credentials?|session[_-]?id|signature|"
    r"[A-Za-z0-9]+(?:secret|token|password|passwd|cookie|authorization|sessionid)|"
    r"(?:private|session|signing|encryption|secret|api|access|auth|client|server|public|ssh|deploy)key)"
    r"(?:[_-][A-Za-z0-9]+)*"
)
SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?im)(['\"`]?(?<!\w){SECRET_KEY_RE}(?!\w)['\"`]?\s*(?:=|:)\s*)"
    rf"(?:\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|`(?:\\.|[^`\\\r\n])*`|[^\r\n]+)"
)
BEARER_RE = re.compile(r"(?im)(\bauthorization\s*:\s*bearer\s+|\bbearer\s+)[^\s,;]+")
COOKIE_HEADER_RE = re.compile(r"(?im)((?:set-)?cookie\s*:\s*)[^\r\n]+")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d .()-]{7,}\d)(?!\d)")
ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-]+)?$")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
TOKEN_RE = re.compile(r"\w[\w'\-]*", re.UNICODE)
HANGUL_RUN_RE = re.compile(r"[가-힣]+")
PROVENANCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
JSON_ERROR_CODES = (
    "missing_source, too_many_sources, invalid_top, empty_query, "
    "unsupported_format, sensitive_source, symlink_source, non_regular_source, "
    "source_too_large, unreadable_source, malformed_jsonl, unsupported_jsonl"
)


class SourceError(Exception):
    def __init__(self, code, message, source=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.source = source


def mask_private_key(match):
    return "[REDACTED PRIVATE KEY]" + "\n" * match.group(0).count("\n")


def mask_phone(match):
    value = match.group(0)
    digits = re.sub(r"\D", "", value)
    if ISO_TIMESTAMP_RE.fullmatch(value.strip()) or not 8 <= len(digits) <= 15:
        return value
    if not re.search(r"[+().\s-]", value) and not (
        value.startswith("0") and len(digits) in {10, 11}
    ):
        return value
    return "[REDACTED PHONE]"


def redact(text):
    """Best-effort redaction before query matching and output, not PII detection."""
    text = PRIVATE_KEY_RE.sub(mask_private_key, text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED SECRET]", text)
    text = BEARER_RE.sub(r"\1[REDACTED SECRET]", text)
    text = COOKIE_HEADER_RE.sub(r"\1[REDACTED SECRET]", text)
    text = EMAIL_RE.sub("[REDACTED EMAIL]", text)
    text = SSN_RE.sub("[REDACTED SSN]", text)
    return PHONE_RE.sub(mask_phone, text)


def tokenize(text):
    tokens = []
    for token in TOKEN_RE.findall(text):
        token = token.lower()
        if len(token) < 2:
            continue
        tokens.append(token)
        for run in HANGUL_RUN_RE.findall(token):
            if len(run) > 1:
                tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def source_path_error(error, source):
    code = "missing_source" if error.errno == errno.ENOENT else "unreadable_source"
    return SourceError(code, str(error), source)

def validate_source(source):
    path = Path(os.path.abspath(os.path.expanduser(source)))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise source_path_error(error, source) from error
        if stat.S_ISLNK(info.st_mode):
            raise SourceError("symlink_source", "symbolic links are not allowed", source)
    try:
        info = path.lstat()
    except OSError as error:
        raise source_path_error(error, source) from error
    if not stat.S_ISREG(info.st_mode):
        raise SourceError("non_regular_source", "source must be a regular file", source)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise SourceError("unsupported_format", "supported formats: .txt, .md, .log, .jsonl", source)
    if any(SENSITIVE_NAME_RE.search(part) for part in path.parts[1:]):
        raise SourceError("sensitive_source", "credential/config-like path components are not allowed", source)
    return path


def open_source_nofollow(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    if len(path.parts) == 1:
        return os.open(path, flags)
    directory_fd = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(path.parts[-1], flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def read_raw_source(path):
    try:
        descriptor = open_source_nofollow(path)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SourceError("symlink_source", str(error), str(path)) from error
        raise source_path_error(error, str(path)) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SourceError("non_regular_source", "source must be a regular file", str(path))
        if info.st_size > MAX_SOURCE_BYTES:
            raise SourceError("source_too_large", f"source exceeds {MAX_SOURCE_BYTES} byte limit", str(path))
        chunks, remaining = [], MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_SOURCE_BYTES:
            raise SourceError("source_too_large", f"source exceeds {MAX_SOURCE_BYTES} byte limit", str(path))
    except OSError as error:
        raise SourceError("unreadable_source", str(error), str(path)) from error
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeError as error:
        raise SourceError("unreadable_source", f"invalid UTF-8: {error}", str(path)) from error


def content_text(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts) if parts else None


def provenance_id(*containers):
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("id", "message_id", "uuid"):
            value = container.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                candidate = str(value)
                if PROVENANCE_ID_RE.fullmatch(candidate):
                    return candidate
    return None


def jsonl_text(record):
    """Extract allowlisted text plus role and bounded message provenance."""
    if not isinstance(record, dict):
        return None
    candidates = [record, record.get("message")]
    payload = record.get("payload")
    if isinstance(payload, dict):
        candidates.extend((payload, payload.get("message")))
    item = record.get("item")
    if isinstance(item, dict):
        candidates.extend((item, item.get("message")))
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("role") not in MESSAGE_ROLES:
            continue
        text = content_text(candidate.get("content"))
        if text is None and isinstance(candidate.get("text"), str):
            text = candidate["text"]
        if text is not None:
            return text, candidate["role"], provenance_id(candidate, record)
    return None


def read_source(path):
    raw = read_raw_source(path)
    if path.suffix.lower() != ".jsonl":
        redacted = redact(raw)
        lines = redacted.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return [
            (line_number, line, None, None)
            for line_number, line in enumerate(lines, 1)
        ], "ok"
    records, unsupported = [], 0
    for line_number, line in enumerate(raw.split("\n"), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SourceError("malformed_jsonl", f"line {line_number}: {error.msg}", str(path)) from error
        parsed = jsonl_text(record)
        if parsed is None:
            unsupported += 1
            continue
        text, role, message_id = parsed
        for text_line in redact(text).split("\n"):
            records.append((line_number, text_line, role, message_id))
    if not records:
        raise SourceError("unsupported_jsonl", "no allowlisted role/content/text messages", str(path))
    return records, "partial_unsupported_records" if unsupported else "ok"




def clip_match(text, terms):
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    lowered = text.lower()
    positions = [position for term in terms if (position := lowered.find(term)) >= 0]
    if not positions:
        return text[:MAX_SNIPPET_CHARS - 1].rstrip() + "…"
    budget = max(1, MAX_SNIPPET_CHARS - 2)
    center = min(positions)
    start = max(0, center - budget // 3)
    end = min(len(text), start + budget)
    start = max(0, end - budget)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def excerpt(lines, match_index, terms):
    line_number, _, role, message_id = lines[match_index]
    if role is not None:
        first = last = match_index
        context_chars = len(lines[match_index][1])
        while (
            first > 0
            and lines[first - 1][0] == line_number
            and last - first + 1 < MAX_MESSAGE_CONTEXT_LINES
            and context_chars + len(lines[first - 1][1]) + 1 <= MAX_MESSAGE_CONTEXT_CHARS
        ):
            first -= 1
            context_chars += len(lines[first][1]) + 1
        while (
            last + 1 < len(lines)
            and lines[last + 1][0] == line_number
            and last - first + 1 < MAX_MESSAGE_CONTEXT_LINES
            and context_chars + len(lines[last + 1][1]) + 1 <= MAX_MESSAGE_CONTEXT_CHARS
        ):
            last += 1
            context_chars += len(lines[last][1]) + 1
    else:
        first = max(0, match_index - 1)
        last = min(len(lines) - 1, match_index + 1)
    snippet = "\n".join(item[1] for item in lines[first:last + 1]).strip()
    if len(snippet) > MAX_SNIPPET_CHARS:
        snippet = clip_match(lines[match_index][1], terms)
        return snippet, line_number, line_number, role, message_id
    return snippet, lines[first][0], lines[last][0], role, message_id


def query(text, sources, top):
    top = max(0, min(top, MAX_TOP))
    terms = set(tokenize(text))
    if not terms:
        raise SourceError("empty_query", "query must contain a token of at least two characters")
    all_results, source_status = [], []
    for source in sources:
        path = validate_source(source)
        lines, status = read_source(path)
        source_status.append({"source": str(path), "status": status})
        source_heap, sequence = [], 0
        if top:
            for index, (_, line, _, _) in enumerate(lines):
                score = len(set(tokenize(line)) & terms)
                if not score:
                    continue
                snippet, line_start, line_end, role, message_id = excerpt(lines, index, terms)
                result = {
                    "source": str(path),
                    "line_start": line_start,
                    "line_end": line_end,
                    "score": score,
                    "snippet": snippet,
                    "role": role,
                    "message_id": message_id,
                    "match_kind": "redacted_text",
                }
                sequence += 1
                entry = ((score, -line_start, -sequence), result)
                if len(source_heap) < top:
                    heapq.heappush(source_heap, entry)
                elif entry[0] > source_heap[0][0]:
                    heapq.heapreplace(source_heap, entry)
        all_results.extend(result for _, result in source_heap)
    all_results.sort(key=lambda result: (-result["score"], result["source"], result["line_start"]))
    return {"results": all_results[:top], "sources": source_status,
            "limitations": "Redaction is best-effort and cannot guarantee detection of every secret or PII value."}


def emit_error(error):
    payload = {"error": {"code": error.code, "message": error.message}}
    if error.source is not None:
        payload["error"]["source"] = error.source
    print(json.dumps(payload, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Query explicit redacted source files without indexing them.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    query_parser = subparsers.add_parser(
        "query",
        help="Search explicitly selected redacted source files.",
        description=(
            f"Search UTF-8 .txt, .md, .log, or .jsonl sources only. "
            f"Limits: {MAX_SOURCES} sources, {MAX_SOURCE_BYTES} bytes/source, "
            f"--top 0..{MAX_TOP}. JSON error codes: {JSON_ERROR_CODES}."
        ),
    )
    query_parser.add_argument("text", help="Text to search after redaction.")
    query_parser.add_argument("--source", action="append", metavar="PATH", help="Explicit source path; repeat per file.")
    query_parser.add_argument("--top", type=int, default=5, help=f"Maximum results, 0..{MAX_TOP} (default: 5).")
    args = parser.parse_args()
    if not args.source:
        emit_error(SourceError("missing_source", "at least one explicit --source is required"))
        return 2
    if len(args.source) > MAX_SOURCES:
        emit_error(SourceError("too_many_sources", f"at most {MAX_SOURCES} explicit sources are allowed"))
        return 2
    if args.top < 0 or args.top > MAX_TOP:
        emit_error(SourceError("invalid_top", f"--top must be between 0 and {MAX_TOP}"))
        return 2
    try:
        print(json.dumps(query(args.text, args.source, args.top), ensure_ascii=False, indent=2))
    except SourceError as error:
        emit_error(error)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
