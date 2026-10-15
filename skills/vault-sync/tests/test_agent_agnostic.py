import contextlib
import importlib.util
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vault_config = load("vault_config", "vault_config.py")
llm = load("llm", "llm.py")
capture = load("vault_capture", "vault-capture.py")
vault_init = load("vault_init", "vault-init.py")


class InitTest(unittest.TestCase):
    def test_init_is_idempotent_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "v"
            vault.mkdir()
            (vault / "_ai").mkdir()
            (vault / "_ai/log.md").write_text("mine")
            with mock.patch("builtins.print"):
                vault_init.init(vault)
                vault_init.init(vault)
            self.assertEqual((vault / "_ai/log.md").read_text(), "mine")
            self.assertTrue((vault / "_ai/oracle_index.md").is_file())

    def test_fresh_skeleton_accepts_capture_catalog_rows_and_is_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            with mock.patch("builtins.print"):
                vault_init.init(vault)
            capture.add_catalog_rows(vault / "_ai/oracle_index.md", [("10_notes", "| [[a]] | 10_notes/a.md | d | k |")])
            self.assertIn("| [[a]] |", (vault / "_ai/oracle_index.md").read_text())
            self.assertEqual(capture.writable_folders(vault), ("10_notes",))
            self.assertIn("workflow/ops", capture.allowed_tags(vault))

    def test_context_is_silent_without_a_vault(self):
        vc = vault_init.vault_config
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}, clear=True), \
                mock.patch.object(vc, "profile_value", lambda key: None), \
                mock.patch.object(vc.Path, "cwd", classmethod(lambda cls: Path(tmp))), \
                contextlib.redirect_stdout(out):
            self.assertEqual(vault_init.context(), 0)
        self.assertEqual(out.getvalue(), "")


class CandidatesTest(unittest.TestCase):
    def candidates(self, env, which=lambda _: None, profile=None):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(llm.shutil, "which", which), \
                mock.patch.object(llm, "profile_value", lambda key: profile):
            return llm.candidates()

    def test_custom_command_is_split_and_used_verbatim(self):
        self.assertEqual(self.candidates({"VAULT_SYNC_LLM": "ollama run 'llama 3'"}), [["ollama", "run", "llama 3"]])

    def test_preset_model_flag_precedes_stdin_marker_for_codex(self):
        cmd = self.candidates({"VAULT_SYNC_LLM": "codex", "VAULT_SYNC_MODEL": "gpt-x"})[0]
        self.assertEqual(cmd[:4], ["codex", "exec", "--model", "gpt-x"])
        self.assertEqual(cmd[-1], "-")

    def test_autodetect_lists_every_installed_preset_in_order(self):
        cmds = self.candidates({}, which=lambda name: "/bin/x" if name in ("pi", "codex") else None)
        self.assertEqual([c[0] for c in cmds], ["pi", "codex"])

    def test_profile_overrides_autodetect_but_env_overrides_profile(self):
        self.assertEqual(self.candidates({}, which=lambda _: "/bin/x", profile="gemini")[0][0], "gemini")
        self.assertEqual(self.candidates({"VAULT_SYNC_LLM": "omp"}, profile="gemini")[0][0], "omp")

    def test_no_backend_is_an_actionable_error(self):
        with self.assertRaisesRegex(RuntimeError, "VAULT_SYNC_LLM"):
            self.candidates({})


class CompleteTest(unittest.TestCase):
    def test_json_extracted_from_noisy_stdout_and_guard_env_set(self):
        script = (
            "import os,sys;sys.stdin.read();"
            "print('Working...');print('{\"guard\": \"%s\"}' % os.environ['VAULT_CAPTURE_RUNNING'])"
        )
        with mock.patch.dict(os.environ, {"VAULT_SYNC_LLM": f"{sys.executable} -c {shlex.quote(script)}"}):
            self.assertEqual(llm.complete_json("prompt"), {"guard": "1"})

    def test_last_valid_object_wins_over_echoed_prompt_braces(self):
        text = 'prompt echo {{"notes": []}} and {not json}\n{"notes": [1]}\n'
        self.assertEqual(llm.last_json_object(text), {"notes": [1]})

    def test_autodetected_backend_falls_back_when_first_fails(self):
        bad = f"{sys.executable} -c 'import sys; sys.exit(3)'"
        good = f"{sys.executable} -c 'import sys; sys.stdin.read(); print(1); print(\"{{}}\")'"
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(llm, "candidates", lambda: [shlex.split(bad), shlex.split(good)]):
            self.assertEqual(llm.complete_json("x"), {})

    def test_explicit_backend_failure_is_not_masked(self):
        with mock.patch.dict(os.environ, {"VAULT_SYNC_LLM": f"{sys.executable} -c 'import sys; sys.exit(3)'"}):
            with self.assertRaisesRegex(RuntimeError, "rc=3"):
                llm.complete_json("x")


class VaultDiscoveryTest(unittest.TestCase):
    def discover(self, cwd, home_files=None):
        with tempfile.TemporaryDirectory() as home:
            for rel, text in (home_files or {}).items():
                path = Path(home) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            cwd = cwd(Path(home)) if callable(cwd) else cwd
            with mock.patch.dict(os.environ, {"HOME": home}, clear=True), \
                    mock.patch.object(vault_config.Path, "cwd", classmethod(lambda cls: cwd)):
                return vault_config._discover()

    def test_vault_above_cwd_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "v/_ai").mkdir(parents=True)
            (Path(tmp) / "v/sub/deep").mkdir(parents=True)
            self.assertEqual(self.discover(Path(tmp) / "v/sub/deep"), Path(tmp) / "v")

    def test_obsidian_registry_prefers_vault_sync_layout_then_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain, wired = Path(tmp, "plain"), Path(tmp, "wired")
            plain.mkdir()
            (wired / "_ai").mkdir(parents=True)
            registry = json.dumps({"vaults": {
                "a": {"path": str(plain), "ts": 9, "open": True},
                "b": {"path": str(wired), "ts": 1},
            }})
            found = self.discover(Path(tmp), {".config/obsidian/obsidian.json": registry})
            self.assertEqual(found, wired)

    def test_nothing_found_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self.discover(Path(tmp)))


class LatestSessionTest(unittest.TestCase):
    def test_newest_file_across_agents_with_harness_label(self):
        with tempfile.TemporaryDirectory() as home:
            old = Path(home, ".claude/projects/p/a.jsonl")
            new = Path(home, ".codex/sessions/2026/10/14/rollout-x.jsonl")
            for i, path in enumerate((old, new)):
                path.parent.mkdir(parents=True)
                path.write_text("{}")
                os.utime(path, (1000 + i, 1000 + i))
            with mock.patch.object(capture.Path, "home", classmethod(lambda cls: Path(home))):
                self.assertEqual(capture.latest_session(), (new, "codex"))
                new.unlink(); old.unlink()
                self.assertIsNone(capture.latest_session())


class LoadDialogueTest(unittest.TestCase):
    def dialogue(self, lines=None, text=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t"
            path.write_text(text if text is not None else "\n".join(json.dumps(l) for l in lines))
            return capture.load_dialogue(path)

    def test_omp_pi_claude_message_shape(self):
        out = self.dialogue([
            {"type": "message", "message": {"role": "system", "content": "secret system prompt"}},
            {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "why?"}]}},
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "x"}]}},
            {"type": "message", "message": {"role": "assistant", "content": "because"}},
        ])
        self.assertEqual(out, "### USER\nwhy?\n\n### ASSISTANT\nbecause")

    def test_codex_rollout_drops_developer_and_injected_context(self):
        def item(role, text, kind="input_text"):
            return {"type": "response_item", "payload": {"type": "message", "role": role,
                                                         "content": [{"type": kind, "text": text}]}}
        out = self.dialogue([
            item("developer", "policy"),
            item("user", "# AGENTS.md instructions\n..."),
            item("user", "fix the bug"),
            item("assistant", "done", "output_text"),
        ])
        self.assertEqual(out, "### USER\nfix the bug\n\n### ASSISTANT\ndone")

    def test_plain_text_transcript_is_used_whole(self):
        self.assertEqual(self.dialogue(text="User: hi\nAssistant: hello\n"), "User: hi\nAssistant: hello")

    def test_jsonl_without_turns_is_empty_not_raw_json(self):
        self.assertEqual(self.dialogue([{"type": "session", "id": "x"}]), "")


if __name__ == "__main__":
    unittest.main()
