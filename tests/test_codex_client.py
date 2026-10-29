"""Offline-only tests. Both subprocess entry points are mocked in every test."""

from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.llm import CodexClient, ErrorKind
from src.llm import codex_client as module


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"success": {"type": "boolean", "const": True}},
    "required": ["success"],
    "additionalProperties": False,
}
PROMPT = '測試 "quotes" & | < > %PATH% !VAR! ^\n只回傳 JSON。'


class CodexClientTests(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.TemporaryDirectory(prefix="kg-codex-test-")
        self.addCleanup(self.parent.cleanup)
        self.client = CodexClient(temp_dir=self.parent.name)
        self.outcomes = []
        self.launches = []
        self.folders = []
        self.processes = []
        self.schemas = []
        self.inputs = []
        self.which = self.enterContext(patch.object(
            module.shutil, "which", return_value=r"C:\Program Files\工具 & Codex\codex.cmd",
        ))
        # No test is allowed to invoke the real Codex or real taskkill command.
        self.popen = self.enterContext(patch.object(module.subprocess, "Popen", side_effect=self.launch))
        self.run = self.enterContext(patch.object(
            module.subprocess, "run", return_value=Mock(returncode=0),
        ))
        self.killpg = self.enterContext(patch.object(module.os, "killpg", create=True))
        self.sleep = self.enterContext(patch.object(module.time, "sleep"))

    def launch(self, command, **kwargs):
        self.launches.append((command, kwargs))
        work = Path(kwargs["cwd"])
        self.folders.append(work)
        self.schemas.append(json.loads((work / "schema.json").read_text(encoding="utf-8")))
        outcome = self.outcomes.pop(0) if self.outcomes else {"raw": '{"success":true}'}
        if isinstance(outcome, BaseException):
            raise outcome
        process = Mock(pid=9876, returncode=None)
        self.processes.append(process)

        def communicate(input=None, timeout=None):
            if input is None:  # Reap after termination.
                process.returncode = -1
                return None, None
            self.inputs.append((input, timeout))
            if outcome.get("timeout"):
                raise subprocess.TimeoutExpired("mocked-codex", timeout, stderr=b"SECRET")
            if outcome.get("interrupt"):
                raise KeyboardInterrupt
            if not outcome.get("missing"):
                raw = outcome.get("raw", '{"success":true}')
                (work / "response.json").write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
            process.returncode = outcome.get("returncode", 0)
            return None, None

        process.communicate.side_effect = communicate
        return process

    def generate(self, **kwargs):
        return self.client.generate_json(PROMPT, SCHEMA, **kwargs)

    def assert_clean(self):
        self.assertTrue(all(not folder.exists() for folder in self.folders))
        self.assertEqual(list(Path(self.parent.name).iterdir()), [])

    def test_success_json_and_records(self):
        result = self.generate()
        self.assertTrue(result.success)
        self.assertEqual(result.data, {"success": True})
        self.assertIsNone(result.error)
        self.assertEqual((result.attempt_count, result.retry_count), (1, 0))
        self.assertEqual(len(result.attempts), 1)
        record = result.attempts[0]
        self.assertTrue(record.success)
        self.assertEqual((record.number, record.returncode), (1, 0))
        self.assertGreaterEqual(record.elapsed_seconds, 0)
        self.assertEqual(self.schemas, [SCHEMA])
        self.assertEqual(self.inputs, [(PROMPT, 120.0)])
        self.assert_clean()

    def test_schema_constraints_are_enforced(self):
        for raw in ('{}', '{"success":"true"}', '{"success":false}',
                    '{"success":true,"extra":1}', '[]', 'null'):
            with self.subTest(raw=raw):
                self.outcomes = [{"raw": raw}]
                result = self.generate(max_retries=3)
                self.assertFalse(result.success)
                self.assertEqual(result.error.kind, ErrorKind.SCHEMA_VALIDATION)
                self.assertIsNone(result.data)
                self.assertEqual(result.attempt_count, 1)
                self.assert_clean()

    def test_local_refs_are_supported(self):
        schema = {"$defs": {"ok": SCHEMA}, "$ref": "#/$defs/ok"}
        result = self.client.generate_json(PROMPT, schema)
        self.assertTrue(result.success)

    def test_array_root_json_is_supported(self):
        self.outcomes = [{"raw": "[1,2]"}]
        schema = {"type": "array", "items": {"type": "integer"}}
        result = self.client.generate_json(PROMPT, schema)
        self.assertTrue(result.success)
        self.assertEqual(result.data, [1, 2])

    def test_null_is_distinct_from_failure(self):
        self.outcomes = [{"raw": "null"}]
        result = self.client.generate_json(PROMPT, {"type": "null"})
        self.assertTrue(result.success)
        self.assertIsNone(result.data)
        self.assertIsNone(result.error)

    def test_invalid_json_is_rejected_without_repair(self):
        for raw in ('', '{"success":', '```json\n{"success":true}\n```',
                    '{"success":true}\n{"success":true}',
                    '{"success":true,"success":false}',
                    '{"success":NaN}', '{"success":Infinity}', '{"success":1e999}',
                    b'\xff', b'\xef\xbb\xbf{"success":true}'):
            with self.subTest(raw=raw):
                self.outcomes = [{"raw": raw}]
                result = self.generate(max_retries=3)
                self.assertEqual(result.error.kind, ErrorKind.INVALID_JSON)
                self.assertEqual(result.attempt_count, 1)
                self.assert_clean()

    def test_invalid_schema_fails_before_cli(self):
        for schema in ({"type": "not-a-json-type"}, {"required": "success"},
                       {"$schema": "https://example.invalid/unknown"},
                       {"$schema": None}, {"$schema": 42}, {"$schema": []},
                       {"const": float("nan")}, {"const": object()}, []):
            with self.subTest(schema=schema):
                result = self.client.generate_json(PROMPT, schema, max_retries=3)
                self.assertEqual(result.error.kind, ErrorKind.INVALID_SCHEMA)
                self.assertEqual((result.attempt_count, result.retry_count), (0, 0))
        self.popen.assert_not_called()
        self.which.assert_not_called()

    def test_external_schema_resource_is_never_fetched(self):
        for ref in ("https://example.invalid/private.json", "file:///private.json"):
            with self.subTest(ref=ref):
                result = self.client.generate_json(PROMPT, {"$ref": ref}, max_retries=3)
                self.assertEqual(result.error.kind, ErrorKind.INVALID_SCHEMA)
                self.assertEqual(result.attempt_count, 1)
                self.assert_clean()

    def test_cli_failure_is_terminal(self):
        self.outcomes = [{"returncode": 17, "raw": '{"success":true}'}]
        result = self.generate(max_retries=5)
        self.assertFalse(result.success)
        self.assertEqual(result.error.kind, ErrorKind.CLI_EXECUTION)
        self.assertEqual(result.attempts[0].returncode, 17)
        self.assertEqual((result.attempt_count, result.retry_count), (1, 0))
        self.assertIsNone(result.data)
        self.assert_clean()

    def test_timeout_has_no_default_retry(self):
        self.outcomes = [{"timeout": True}]
        result = self.generate(timeout=0.1)
        self.assertEqual(result.error.kind, ErrorKind.TIMEOUT)
        self.assertEqual((result.attempt_count, result.retry_count), (1, 0))
        self.assertEqual(self.inputs, [(PROMPT, 0.1)])
        self.assert_clean()

    def test_retry_limit_is_additional_attempts(self):
        self.outcomes = [{"timeout": True}] * 3
        result = self.generate(max_retries=2, retry_delay=0.25)
        self.assertEqual(result.error.kind, ErrorKind.TIMEOUT)
        self.assertEqual((result.attempt_count, result.retry_count), (3, 2))
        self.assertEqual([a.number for a in result.attempts], [1, 2, 3])
        self.assertTrue(all(a.error.kind == ErrorKind.TIMEOUT for a in result.attempts))
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(len(set(self.folders)), 3)
        self.assert_clean()

    def test_retry_can_succeed(self):
        self.outcomes = [{"timeout": True}, {"raw": '{"success":true}'}]
        result = self.generate(max_retries=2)
        self.assertTrue(result.success)
        self.assertEqual((result.attempt_count, result.retry_count), (2, 1))
        self.assertEqual(result.attempts[0].error.kind, ErrorKind.TIMEOUT)
        self.assertTrue(result.attempts[1].success)
        self.assert_clean()

    def test_explicit_json_and_schema_retry(self):
        for raw, kind in (("bad", ErrorKind.INVALID_JSON), ('{}', ErrorKind.SCHEMA_VALIDATION)):
            with self.subTest(kind=kind):
                self.outcomes = [{"raw": raw}, {"raw": '{"success":true}'}]
                result = self.generate(max_retries=1, retry_on=(kind,))
                self.assertTrue(result.success)
                self.assertEqual((result.attempt_count, result.retry_count), (2, 1))
                self.assert_clean()

    def test_retry_can_be_disabled_for_timeout(self):
        self.outcomes = [{"timeout": True}]
        result = self.generate(max_retries=5, retry_on=())
        self.assertEqual(result.attempt_count, 1)

    def test_missing_cli_does_not_launch(self):
        self.which.return_value = None
        result = self.generate(max_retries=5)
        self.assertEqual(result.error.kind, ErrorKind.CLI_NOT_FOUND)
        self.assertEqual(result.attempt_count, 0)
        self.popen.assert_not_called()
        self.assert_clean()

    def test_spawn_failure_is_terminal_and_cleaned(self):
        self.outcomes = [PermissionError("SECRET")]
        result = self.generate(max_retries=5)
        self.assertEqual(result.error.kind, ErrorKind.IO_ERROR)
        self.assertEqual(result.attempt_count, 1)
        self.assertNotIn("SECRET", repr(result))
        self.assert_clean()

    def test_temporary_parent_failure_is_reported(self):
        client = CodexClient(temp_dir=Path(self.parent.name) / "missing")
        result = client.generate_json(PROMPT, SCHEMA, max_retries=5)
        self.assertEqual(result.error.kind, ErrorKind.IO_ERROR)
        self.assertEqual(result.attempt_count, 1)
        self.popen.assert_not_called()

    def test_missing_response_is_an_error(self):
        self.outcomes = [{"missing": True}]
        result = self.generate()
        self.assertEqual(result.error.kind, ErrorKind.INVALID_JSON)
        self.assert_clean()

    def test_temp_cleanup_error_cannot_return_success_or_retry(self):
        real_temporary_directory = tempfile.TemporaryDirectory

        def failing_cleanup_directory(**kwargs):
            temporary = real_temporary_directory(**kwargs)
            real_cleanup = temporary.cleanup

            def cleanup():
                real_cleanup()
                raise PermissionError("SENSITIVE_PATH")

            temporary.cleanup = cleanup
            return temporary

        with patch.object(module.tempfile, "TemporaryDirectory", side_effect=failing_cleanup_directory):
            result = self.generate(max_retries=5)
        self.assertFalse(result.success)
        self.assertIsNone(result.data)
        self.assertEqual(result.error.kind, ErrorKind.IO_ERROR)
        self.assertEqual(result.attempt_count, 1)
        self.assertNotIn("SENSITIVE_PATH", repr(result))
        self.assert_clean()

    def test_output_size_is_bounded_and_not_retryable(self):
        client = CodexClient(temp_dir=self.parent.name, max_output_bytes=5)
        result = client.generate_json(PROMPT, SCHEMA, max_retries=5)
        self.assertEqual(result.error.kind, ErrorKind.OUTPUT_LIMIT)
        self.assertEqual(result.attempt_count, 1)
        self.assert_clean()

    def test_windows_command_quotes_paths_and_keeps_prompt_out(self):
        with patch.object(module, "_IS_WINDOWS", True):
            result = self.generate(model="example-model")
        self.assertTrue(result.success)
        command, kwargs = self.launches[0]
        self.assertIsInstance(command, str)
        self.assertIn('"C:\\Program Files\\工具 & Codex\\codex.cmd"', command)
        self.assertIn('/d /s /v:off /c ""', command)
        self.assertIn('"exec"', command)
        for flag in ("--output-schema", "--output-last-message", "--ephemeral",
                     "--skip-git-repo-check", "--ignore-user-config", "--no-daemon"):
            self.assertIn('"' + flag + '"', command)
        self.assertIn('"--ask-for-approval" "never"', command)
        self.assertIn('"--sandbox" "read-only"', command)
        self.assertIn('"--model" "example-model"', command)
        self.assertTrue(command.endswith('"-""'))
        self.assertNotIn(PROMPT, command)
        self.assertNotIn("powershell", command.lower())
        self.assertNotIn(".ps1", command.lower())
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotEqual(Path(kwargs["cwd"]), Path.cwd())
        self.assert_clean()

    def test_windows_path_expansion_characters_are_rejected(self):
        for executable in ('C:\\%SECRET%\\codex.cmd', 'C:\\!SECRET!\\codex.cmd',
                           'C:\\bad"\\codex.cmd', 'C:\\bad\n\\codex.cmd'):
            with self.subTest(executable=executable), patch.object(module, "_IS_WINDOWS", True):
                self.which.return_value = executable
                result = self.generate(max_retries=5)
                self.assertEqual(result.error.kind, ErrorKind.IO_ERROR)
                self.assertEqual(result.attempt_count, 1)
        self.popen.assert_not_called()

    def test_windows_timeout_terminates_tree_before_retry(self):
        self.outcomes = [{"timeout": True}, {"raw": '{"success":true}'}]
        with patch.object(module, "_IS_WINDOWS", True):
            result = self.generate(max_retries=1)
        self.assertTrue(result.success)
        self.run.assert_called_once()
        command = self.run.call_args.args[0]
        self.assertEqual(command[1:], ["/PID", "9876", "/T", "/F"])
        self.assertEqual(Path(command[0]).name, "taskkill.exe")
        self.assertEqual(self.run.call_args.kwargs["timeout"], 5)
        self.assertFalse(self.run.call_args.kwargs["shell"])
        self.assertEqual(self.processes[0].communicate.call_count, 2)
        self.assert_clean()

    def test_cleanup_failure_prevents_retry(self):
        for failure in (Mock(returncode=1), OSError("SECRET"), subprocess.TimeoutExpired("taskkill", 5)):
            with self.subTest(failure=failure), patch.object(module, "_IS_WINDOWS", True):
                self.outcomes = [{"timeout": True}]
                self.run.side_effect = failure if isinstance(failure, BaseException) else None
                self.run.return_value = failure
                result = self.generate(max_retries=3)
                self.assertEqual(result.error.kind, ErrorKind.PROCESS_CLEANUP)
                self.assertEqual(result.attempt_count, 1)
                self.processes[-1].kill.assert_called_once()
                self.assert_clean()

    def test_interrupt_also_cleans_up(self):
        self.outcomes = [{"interrupt": True}]
        with patch.object(module, "_IS_WINDOWS", True):
            with self.assertRaises(KeyboardInterrupt):
                self.generate()
        self.run.assert_called_once()
        self.assert_clean()

    def test_invalid_arguments_never_launch(self):
        for kwargs in (
            {"timeout": 0}, {"timeout": -1}, {"timeout": float("nan")},
            {"timeout": float("inf")}, {"timeout": True},
            {"max_retries": -1}, {"max_retries": 1.5}, {"max_retries": True},
            {"retry_delay": -0.1}, {"retry_delay": float("inf")},
            {"retry_on": (ErrorKind.CLI_EXECUTION,)}, {"retry_on": ("unknown",)},
            {"retry_on": None}, {"model": "model & whoami"}, {"model": "-flag"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.generate(**kwargs)
        for prompt in ("", "  ", None, "\ud800"):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                self.client.generate_json(prompt, SCHEMA)
        for limit in (0, -1, True, 1.2):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                CodexClient(max_output_bytes=limit)
        self.popen.assert_not_called()

    def test_result_and_logs_do_not_contain_user_or_raw_error_content(self):
        self.outcomes = [{"raw": '{"success":"SENSITIVE_VALUE"}'}]
        logger_name = module.__name__
        with self.assertLogs(logger_name, level=logging.INFO) as captured:
            result = self.generate()
        serialized = json.dumps(asdict(result), ensure_ascii=False)
        logs = "\n".join(captured.output)
        for output in (serialized, logs):
            self.assertNotIn(PROMPT, output)
            self.assertNotIn("SENSITIVE_VALUE", output)
            self.assertNotIn(self.parent.name, output)
        self.assertIn("schema_validation", logs)
        self.assertIn("attempt=1", logs)

    def test_api_key_environment_is_not_forwarded(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "SECRET_A", "CODEX_API_KEY": "SECRET_B",
            "CODEX_HOME": "existing-auth-location",
        }):
            result = self.generate()
        self.assertTrue(result.success)
        child_env = self.launches[0][1]["env"]
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("CODEX_API_KEY", child_env)
        self.assertEqual(child_env["CODEX_HOME"], "existing-auth-location")
        self.assertNotIn("SECRET", repr(result))

    def test_results_do_not_share_call_history(self):
        first = self.generate()
        second = self.generate()
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(second.attempt_count, 1)
        self.assertIsNot(first.attempts, second.attempts)

    def test_smoke_schema_matches_expected_payload(self):
        path = Path(__file__).resolve().parents[1] / "schemas" / "codex_smoke.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        module.Draft202012Validator.check_schema(schema)
        module.Draft202012Validator(schema).validate({"success": True})


if __name__ == "__main__":
    unittest.main()
