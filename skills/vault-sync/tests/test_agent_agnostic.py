import importlib.util
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


llm = load("llm", "llm.py")
capture = load("vault_capture", "vault-capture.py")


class ResolveCommandTest(unittest.TestCase):
    def resolve(self, env, which=lambda _: None, profile=None):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(llm.shutil, "which", which), \
                mock.patch.object(llm, "profile_value", lambda key: profile):
            return llm.resolve_command()

    def test_custom_command_is_split_and_used_verbatim(self):
        self.assertEqual(self.resolve({"VAULT_SYNC_LLM": "ollama run 'llama 3'"}), ["ollama", "run", "llama 3"])

    def test_preset_model_flag_precedes_stdin_marker_for_codex(self):
        cmd = self.resolve({"VAULT_SYNC_LLM": "codex", "VAULT_SYNC_MODEL": "gpt-x"})
        self.assertEqual(cmd[:4], ["codex", "exec", "--model", "gpt-x"])
        self.assertEqual(cmd[-1], "-")

    def test_autodetect_picks_first_installed_preset_in_order(self):
        cmd = self.resolve({}, which=lambda name: "/bin/x" if name in ("pi", "codex") else None)
        self.assertEqual(cmd[0], "pi")

    def test_profile_overrides_autodetect_but_env_overrides_profile(self):
        self.assertEqual(self.resolve({}, which=lambda _: "/bin/x", profile="gemini")[0], "gemini")
        self.assertEqual(self.resolve({"VAULT_SYNC_LLM": "omp"}, profile="gemini")[0], "omp")

    def test_no_backend_is_an_actionable_error(self):
        with self.assertRaisesRegex(RuntimeError, "VAULT_SYNC_LLM"):
            self.resolve({})


class CompleteTest(unittest.TestCase):
    def test_json_extracted_from_noisy_stdout_and_guard_env_set(self):
        script = (
            "import os,sys;sys.stdin.read();"
            "print('Working...');print('{\"guard\": \"%s\"}' % os.environ['VAULT_CAPTURE_RUNNING'])"
        )
        with mock.patch.dict(os.environ, {"VAULT_SYNC_LLM": f"{sys.executable} -c {shlex.quote(script)}"}):
            self.assertEqual(llm.complete_json("prompt"), {"guard": "1"})

    def test_nonzero_exit_raises(self):
        with mock.patch.dict(os.environ, {"VAULT_SYNC_LLM": f"{sys.executable} -c 'import sys; sys.exit(3)'"}):
            with self.assertRaisesRegex(RuntimeError, "rc=3"):
                llm.complete("x")


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
