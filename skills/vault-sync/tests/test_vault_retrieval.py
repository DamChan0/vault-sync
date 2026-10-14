import importlib.util
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SOURCE_QUERY = SCRIPTS / "vault-source-query.py"
BM25 = SCRIPTS / "vault-bm25.py"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VaultRetrievalTests(unittest.TestCase):
    def run_source_query(self, *args):
        return subprocess.run(
            [sys.executable, str(SOURCE_QUERY), "query", *args],
            check=False, text=True, capture_output=True,
        )

    def test_bm25_body_excerpt_uses_original_markdown_lines(self):
        bm25 = load_module(BM25, "vault_bm25_test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "memory" / "note.md"
            note.parent.mkdir()
            note.write_text("---\ntitle: Metadata only\nkeywords: searchterm\n---\nfirst body\nneedle line\nlast body\n", encoding="utf-8")
            old_root, old_resolved = bm25.VAULT_ROOT, bm25.VAULT_ROOT_RESOLVED
            bm25.VAULT_ROOT, bm25.VAULT_ROOT_RESOLVED = root, root.resolve()
            try:
                excerpt, start, end, kind = bm25.body_excerpt("memory/note.md", "needle")
                self.assertEqual((start, end, kind), (5, 7, "body"))
                self.assertEqual(excerpt, "first body\nneedle line\nlast body")
                self.assertLessEqual(len(bm25.clip_body_excerpt("x" * 1000 + " needle", {"needle"}, 400)), 400)
                self.assertEqual(bm25.body_excerpt("memory/note.md", "searchterm")[3], "metadata")
                self.assertEqual(bm25.body_excerpt("memory/note.md", "last body")[1:3], (6, 7))
                note.write_text("x" * 600 + " needle\n", encoding="utf-8")
                excerpt, start, end, kind = bm25.body_excerpt("memory/note.md", "needle")
                self.assertEqual((start, end, kind), (1, 1, "body"))
                self.assertIn("needle", excerpt)
            finally:
                bm25.VAULT_ROOT, bm25.VAULT_ROOT_RESOLVED = old_root, old_resolved

    def test_bm25_note_size_bound(self):
        bm25 = load_module(BM25, "vault_bm25_size_test")
        with tempfile.TemporaryDirectory() as directory:
            note = Path(directory) / "note.md"
            note.write_text("0123456789", encoding="utf-8")
            old_limit = bm25.MAX_NOTE_BYTES
            bm25.MAX_NOTE_BYTES = 4
            try:
                with self.assertRaises(OSError):
                    bm25.read_note_text(note)
            finally:
                bm25.MAX_NOTE_BYTES = old_limit

    def test_source_query_returns_source_lines_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.log"
            source.write_text("context\nneedle response\ntrailer\n", encoding="utf-8")
            before = source.read_bytes()
            result = self.run_source_query("needle", "--source", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["results"][0]["line_start"], 1)
            self.assertEqual(payload["results"][0]["line_end"], 3)
            self.assertEqual(payload["results"][0]["snippet"], "context\nneedle response\ntrailer")
            trailer_result = self.run_source_query("trailer", "--source", str(source))
            trailer_payload = json.loads(trailer_result.stdout)
            self.assertEqual(trailer_payload["results"][0]["line_end"], 3)
            self.assertEqual(source.read_bytes(), before)

    def test_source_query_clip_respects_maximum_length(self):
        source_query = load_module(SOURCE_QUERY, "vault_source_clip_test")
        clipped = source_query.clip_match("x" * 1000 + " needle", {"needle"})
        self.assertLessEqual(len(clipped), source_query.MAX_SNIPPET_CHARS)

    def test_source_query_preserves_timestamps_and_redacts_json_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.log"
            source.write_text(
                "preamble\n"
                'timestamp=2026-09-07 12:00:00 epoch=1757260800 {"api_key": "json-secret"}\n'
                "needle response\n"
                "trailer\n",
                encoding="utf-8",
            )
            result = self.run_source_query("needle", "--source", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            snippet = payload["results"][0]["snippet"]
            self.assertIn("2026-09-07 12:00:00", snippet)
            self.assertNotIn("json-secret", result.stdout)
            self.assertIn("[REDACTED SECRET]", snippet)

    def test_source_query_redacts_prefixed_and_escaped_secret_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.log"
            source.write_text(
                'client_secret = "client-value"; aws_secret_access_key: "hunter2 value"; '
                'privateKey = "private-value"; '
                'phone=010-1234-5678 email=user@example.com ssn=123-45-6789; '
                + json.dumps({"token": 'ab"cd'})
                + '; {"password": {"a": "s1", "b": "s2"}}\nneedle response\n',
                encoding="utf-8",
            )
            result = self.run_source_query("needle", "--source", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("client-value", result.stdout)
            self.assertNotIn("private-value", result.stdout)
            self.assertNotIn("hunter2 value", result.stdout)
            self.assertNotIn("010-1234-5678", result.stdout)
            self.assertNotIn("user@example.com", result.stdout)
            self.assertNotIn("123-45-6789", result.stdout)
            self.assertIn("[REDACTED PHONE]", result.stdout)
            self.assertIn("[REDACTED EMAIL]", result.stdout)
            self.assertIn("[REDACTED SSN]", result.stdout)
            self.assertNotIn('ab"cd', result.stdout)
            self.assertNotIn("s1", result.stdout)
            self.assertNotIn("s2", result.stdout)

    def test_source_query_rejects_sensitive_parent_path(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "secrets" / "session.log"
            source.parent.mkdir()
            source.write_text("needle\n", encoding="utf-8")
            result = self.run_source_query("needle", "--source", str(source))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sensitive_source", result.stdout)

    def test_source_query_reports_jsonl_provenance_without_crossing_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.jsonl"
            source.write_text(
                '{"id":"msg-1","role":"assistant","content":"first needle"}\n'
                '{"id":"msg-2","role":"user","content":"nearby context"}\n',
                encoding="utf-8",
            )
            result = self.run_source_query("needle", "--source", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            hit = json.loads(result.stdout)["results"][0]
            self.assertEqual(hit["snippet"], "first needle")
            self.assertEqual((hit["line_start"], hit["line_end"]), (1, 1))
            self.assertEqual((hit["role"], hit["message_id"]), ("assistant", "msg-1"))
            self.assertNotIn("nearby context", hit["snippet"])

    def test_source_query_bounds_multiline_jsonl_context(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "long.jsonl"
            content = "\n".join("needle" if index % 2 == 0 else "context" for index in range(2000))
            source.write_text(json.dumps({"role": "assistant", "content": content}) + "\n", encoding="utf-8")
            result = self.run_source_query("needle", "--source", str(source), "--top", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            hit = json.loads(result.stdout)["results"][0]
            self.assertLessEqual(len(hit["snippet"]), 500)
            self.assertIn("needle", hit["snippet"])
            self.assertEqual((hit["line_start"], hit["line_end"]), (1, 1))

    def test_source_query_handles_hangul_particles(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.log"
            source.write_text("원문을 사운드가v2를 확인했다\n", encoding="utf-8")
            for query in ("원문", "사운드"):
                result = self.run_source_query(query, "--source", str(source))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(json.loads(result.stdout)["results"]), 1)

    def test_bm25_filters_deleted_documents(self):
        bm25 = load_module(BM25, "vault_bm25_deleted_test")
        index = {
            "schema_version": 1,
            "params": {"k1": 1.2, "b": 0.75},
            "doc_count": 2,
            "avg_dl": 1.0,
            "updated_at": "2026-09-07T00:00:00Z",
            "docs": {
                "memory/live.md": {"title": "Live", "dl": 1},
                "memory/deleted.md": {"title": "Deleted", "dl": 1},
            },
            "vocab": {
                "needle": {
                    "df": 2,
                    "postings": [["memory/live.md", 1], ["memory/deleted.md", 1]],
                }
            },
        }
        with mock.patch.object(
            bm25,
            "is_current_indexed_note",
            side_effect=lambda rel: rel == "memory/live.md",
        ):
            pruned = bm25.validate_index(index)
        self.assertEqual(set(pruned["docs"]), {"memory/live.md"})
        self.assertEqual(pruned["doc_count"], 1)
        self.assertEqual(pruned["vocab"]["needle"]["postings"], [["memory/live.md", 1]])

    def test_bm25_empty_build_preserves_existing_index(self):
        bm25 = load_module(BM25, "vault_bm25_empty_test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meta = root / "meta"
            index_path = meta / "index.json"
            index_path.parent.mkdir()
            index_path.write_text("sentinel", encoding="utf-8")
            old_values = {
                name: getattr(bm25, name)
                for name in ("VAULT_ROOT", "VAULT_ROOT_RESOLVED", "MANAGED_DIRS", "META_DIR", "BM25_DIR", "INDEX_PATH", "LOCK_PATH")
            }
            bm25.VAULT_ROOT = root
            bm25.VAULT_ROOT_RESOLVED = root.resolve()
            bm25.MANAGED_DIRS = ("missing",)
            bm25.META_DIR = meta
            bm25.BM25_DIR = meta
            bm25.INDEX_PATH = index_path
            bm25.LOCK_PATH = meta / "lock"
            bm25.WORK_NOTES_ROOT = root / "no-work-notes"
            try:
                with mock.patch.object(sys, "argv", ["vault-bm25.py", "build"]):
                    self.assertEqual(bm25.main(), bm25.EXIT_INDEX_MISSING)
                self.assertEqual(index_path.read_text(encoding="utf-8"), "sentinel")
            finally:
                for name, value in old_values.items():
                    setattr(bm25, name, value)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_bm25_lock_rejects_fifo_without_blocking(self):
        bm25 = load_module(BM25, "vault_bm25_lock_fifo_test")
        with tempfile.TemporaryDirectory() as directory:
            meta = Path(directory) / "meta"
            meta.mkdir()
            lock_path = meta / "lock"
            os.mkfifo(lock_path)
            old_meta, old_lock = bm25.META_DIR, bm25.LOCK_PATH
            bm25.META_DIR, bm25.LOCK_PATH = meta, lock_path
            try:
                with self.assertRaises(SystemExit) as raised:
                    bm25.acquire_lock()
                self.assertEqual(raised.exception.code, bm25.EXIT_LOCK)
            finally:
                bm25.META_DIR, bm25.LOCK_PATH = old_meta, old_lock

    def test_bm25_allows_excluded_word_as_filename(self):
        bm25 = load_module(BM25, "vault_bm25_filename_test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "memory" / "work.md"
            note.parent.mkdir()
            note.write_text("needle", encoding="utf-8")
            old_values = {
                name: getattr(bm25, name)
                for name in ("VAULT_ROOT", "VAULT_ROOT_RESOLVED", "MANAGED_DIRS")
            }
            bm25.VAULT_ROOT = root
            bm25.VAULT_ROOT_RESOLVED = root.resolve()
            bm25.MANAGED_DIRS = ("memory",)
            try:
                self.assertEqual(list(bm25.discover_notes())[0][0], "memory/work.md")
            finally:
                for name, value in old_values.items():
                    setattr(bm25, name, value)

    def test_bm25_work_notes_are_tagged_external_and_resolve_outside_vault(self):
        bm25 = load_module(BM25, "vault_bm25_work_notes_test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault, work = root / "vault", root / "work-notes"
            (vault / "memory").mkdir(parents=True)
            (vault / "memory" / "note.md").write_text("unrelated", encoding="utf-8")
            (work / "ese-1").mkdir(parents=True)
            (work / "ese-1" / "handoff.md").write_text("needle handoff", encoding="utf-8")
            (work / ".hidden").mkdir()
            (work / ".hidden" / "x.md").write_text("needle", encoding="utf-8")
            meta = vault / ".vault-meta"
            bm25.VAULT_ROOT, bm25.VAULT_ROOT_RESOLVED = vault, vault.resolve()
            bm25.MANAGED_DIRS = ("memory",)
            bm25.META_DIR = bm25.BM25_DIR = meta
            bm25.INDEX_PATH, bm25.LOCK_PATH = meta / "index.json", meta / "lock"
            bm25.WORK_NOTES_ROOT = work
            bm25.write_index(bm25.build_index())
            [hit] = bm25.query("needle")
            self.assertEqual(hit["source"], "work-notes")
            self.assertEqual(hit["path"], "work-notes:ese-1/handoff.md")
            self.assertEqual(hit["absolute_path"], str(work / "ese-1" / "handoff.md"))
            self.assertEqual(hit["match_kind"], "body")
            (work / "ese-1" / "handoff.md").unlink()
            self.assertEqual(bm25.query("needle"), [])

    def test_source_query_rejects_symlinks_and_unsupported_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsupported = root / "session.csv"
            unsupported.write_text("needle", encoding="utf-8")
            rejected = self.run_source_query("needle", "--source", str(unsupported))
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(json.loads(rejected.stdout)["error"]["code"], "unsupported_format")
            linked = root / "linked.log"
            linked.symlink_to(unsupported)
            rejected = self.run_source_query("needle", "--source", str(linked))
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(json.loads(rejected.stdout)["error"]["code"], "symlink_source")

    def test_source_query_classifies_open_race_errors(self):
        source_query = load_module(SOURCE_QUERY, "vault_source_open_error_test")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.log"
            source.write_text("needle\n", encoding="utf-8")
            with mock.patch.object(
                source_query,
                "open_source_nofollow",
                side_effect=OSError(errno.ELOOP, "symlink race"),
            ):
                with self.assertRaises(source_query.SourceError) as raised:
                    source_query.read_raw_source(source)
            self.assertEqual(raised.exception.code, "symlink_source")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_source_query_rejects_fifo_without_blocking(self):
        source_query = load_module(SOURCE_QUERY, "vault_source_fifo_test")
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "session.log"
            os.mkfifo(fifo)
            with self.assertRaises(source_query.SourceError) as raised:
                source_query.read_raw_source(fifo)
            self.assertEqual(raised.exception.code, "non_regular_source")

    def test_source_query_rejects_malformed_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.jsonl"
            source.write_text('{"role":"user","content":"needle"}\nnot-json\n', encoding="utf-8")
            result = self.run_source_query("needle", "--source", str(source))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "malformed_jsonl")

    def test_source_query_bounds_top_and_redacts_multiline_and_inline_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.log"
            source.write_text(
                "-----BEGIN PRIVATE KEY-----\nprivate key material\n-----END PRIVATE KEY-----\n"
                'api_token = "quoted secret with spaces"\n'
                "Authorization: Bearer inline-secret\n"
                + "x" * 600 + " needle\nsecond needle\n",
                encoding="utf-8",
            )
            result = self.run_source_query("needle", "--source", str(source), "--top", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["results"]), 1)
            self.assertIn("needle", payload["results"][0]["snippet"])
            self.assertNotIn("private key material", result.stdout)
            self.assertNotIn("quoted secret with spaces", result.stdout)
            self.assertNotIn("inline-secret", result.stdout)

    def test_source_query_rejects_nonregular_root_source(self):
        result = self.run_source_query("needle", "--source", "/")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "non_regular_source")

    def test_jsonl_metadata_secrets_are_not_searchable_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.jsonl"
            source.write_text(
                '{"role":"assistant","content":"safe answer","metadata":{"token":"metadata-secret"}}\n',
                encoding="utf-8",
            )
            result = self.run_source_query("metadata-secret", "--source", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["results"], [])
            self.assertNotIn("metadata-secret", result.stdout)

    def test_secret_only_query_never_reveals_raw_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.log"
            raw_secret = "password=super-secret-value\n"
            source.write_text(raw_secret, encoding="utf-8")
            result = self.run_source_query("super-secret-value", "--source", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("super-secret-value", result.stdout)
            self.assertNotIn(raw_secret, result.stdout)


if __name__ == "__main__":
    unittest.main()
