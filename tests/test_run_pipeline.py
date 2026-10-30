"""Offline orchestration tests: every stage is mocked, never a live LLM."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from jsonschema.exceptions import SchemaError, ValidationError

from scripts import run_pipeline as pipeline
from src.ingestion import SourceLoadError
from src.pipeline.step0_safety import ResultIntegrityError
from src.pipeline.step1_split import Step1Error, Step1LLMError
from src.pipeline.step2_extract import Step2Error, Step2LLMError


class RunPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pipeline-entry-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "machine failure.md"
        self.source.write_text("設備故障時記錄事件。", encoding="utf-8")
        self.output_root = self.root / "runs"
        self.enterContext(patch.object(pipeline, "DEFAULT_OUTPUT_DIR", self.output_root))
        self.enterContext(patch("src.llm.codex_client.subprocess.Popen", side_effect=AssertionError("LIVE LLM FORBIDDEN")))
        self.enterContext(patch("src.llm.codex_client.subprocess.run", side_effect=AssertionError("LIVE PROCESS FORBIDDEN")))
        self.step0_result = {"status": {"execution_state": "completed"}}
        self.step1_result = {"status": {"execution_state": "completed"}, "knowledge_statements": [{}, {}]}
        self.step2_result = {"status": {"execution_state": "completed", "reviewed_statement_count": 2,
            "kept_count": 1, "filtered_count": 1, "needs_review_count": 0, "knowledge_item_count": 3, "occurrence_count": 4}}
        self.paths0, self.paths1 = [], []
        self.step0 = self.enterContext(patch.object(pipeline, "run_step0", autospec=True, side_effect=self._step0))
        self.step1 = self.enterContext(patch.object(pipeline, "run_step1", autospec=True, side_effect=self._step1))
        self.step2 = self.enterContext(patch.object(pipeline, "run_step2", autospec=True, side_effect=self._step2))
        self.sequence = Mock()
        self.sequence.attach_mock(self.step0, "step0")
        self.sequence.attach_mock(self.step1, "step1")
        self.sequence.attach_mock(self.step2, "step2")

    def _publish(self, output_dir, name, result):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / name
        with path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream)
        return deepcopy(result), path

    def _step0(self, source, output_dir, **options):
        # Intentionally unlike source.stem: the entrypoint must use this return value.
        result, path = self._publish(output_dir, "actual-returned.step0.json", self.step0_result)
        self.paths0.append(path)
        return result, path

    def _step1(self, input_path, output_dir, **options):
        result, path = self._publish(output_dir, "actual-returned.step1.json", self.step1_result)
        self.paths1.append(path)
        return result, path

    def _step2(self, input_path, output_dir, **options):
        return self._publish(output_dir, "actual-returned.step2.json", self.step2_result)

    def run_cli(self, *options):
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = pipeline.main([str(self.source), *options])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_order_actual_json_path_and_default_options(self):
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        run_dir = self.paths0[0].parent.parent
        self.assertEqual(self.sequence.mock_calls, [
            call.step0(self.source.resolve(), run_dir / "step0", timeout=360.0, max_retries=0),
            call.step1(self.paths0[0], run_dir / "step1", timeout=360.0, max_retries=0),
        ])
        self.assertEqual(run_dir.parent, self.output_root)
        self.assertIn("Step 0：completed", stdout)
        self.assertIn("Step 1：completed", stdout)
        self.assertIn("知識條文數量：2", stdout)
        self.assertIn(str(self.paths0[0]), stdout)
        self.assertIn(str(self.paths1[0]), stdout)
        self.step2.assert_not_called()

    def test_until_two_order_options_actual_step1_path_and_statistics(self):
        code, stdout, stderr = self.run_cli("--until", "2", "--timeout", "420", "--max-retries", "1")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        run_dir = self.paths0[0].parent.parent
        self.assertEqual(self.sequence.mock_calls, [
            call.step0(self.source.resolve(), run_dir / "step0", timeout=420.0, max_retries=1),
            call.step1(self.paths0[0], run_dir / "step1", timeout=420.0, max_retries=1),
            call.step2(self.paths1[0], run_dir / "step2", timeout=420.0, max_retries=1),
        ])
        for value in ("審查條文數量：2", "kept：1 / filtered：1 / needs_review：0", "去重後知識項數量：3", "occurrences 數量：4", "actual-returned.step2.json"):
            self.assertIn(value, stdout)

    def test_step2_errors_preserve_both_upstream_outputs(self):
        for error, expected_code in ((Step2LLMError("Step 2 LLM 呼叫失敗（timeout）"), 1),
                                     (Step2Error("bad concept"), 2), (ValidationError("SECRET_RAW_DATA"), 2),
                                     (PermissionError("denied"), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error).__name__):
                self.step2.side_effect = error
                code, _, stderr = self.run_cli("--until", "2")
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(self.paths0[-1].read_text(encoding="utf-8")), self.step0_result)
                self.assertEqual(json.loads(self.paths1[-1].read_text(encoding="utf-8")), self.step1_result)
                self.assertIn(f"已保留 Step 0 JSON：{self.paths0[-1]}", stderr)
                self.assertIn(f"已保留 Step 1 JSON：{self.paths1[-1]}", stderr)
                self.assertIn("Step 2：失敗", stderr)
                self.assertNotIn("SECRET_RAW_DATA", stderr)
                self.assertFalse((self.paths1[-1].parent.parent / "step2").exists())

    def test_step2_warning_state_succeeds_and_failed_state_is_failure(self):
        for state, code in (("completed_with_warnings", 0), ("llm_failed", 1), ("unknown", 1)):
            with self.subTest(state=state):
                self.step2_result["status"]["execution_state"] = state
                self.assertEqual(self.run_cli("--until", "2")[0], code)

    def test_until_two_stops_on_upstream_failures(self):
        self.step0_result["status"]["execution_state"] = "llm_failed"
        code, stdout, _ = self.run_cli("--until", "2")
        self.assertEqual(code, 1)
        self.assertIn("Step 2：未執行", stdout)
        self.step2.assert_not_called()
        self.step0_result["status"]["execution_state"] = "completed"
        self.step1.side_effect = Step1Error("invalid")
        code, _, stderr = self.run_cli("--until", "2")
        self.assertEqual(code, 2)
        self.assertIn("Step 2：未執行", stderr)
        self.step2.assert_not_called()

    def test_until_zero_runs_only_step0_and_reports_skipped_step1(self):
        code, stdout, _ = self.run_cli("--until", "0")
        self.assertEqual(code, 0)
        self.step0.assert_called_once()
        self.step1.assert_not_called()
        self.assertTrue(self.paths0[0].is_file())
        self.assertFalse((self.paths0[0].parent.parent / "step1").exists())
        self.assertIn("Step 1：未執行（--until 0）", stdout)
        self.assertIn("知識條文數量：未產生", stdout)

    def test_explicit_until_one_and_custom_options_apply_to_both_steps(self):
        code, _, _ = self.run_cli("--until", "1", "--timeout", "600.5", "--max-retries", "2")
        self.assertEqual(code, 0)
        for step in (self.step0, self.step1):
            self.assertEqual(step.call_args.kwargs, {"timeout": 600.5, "max_retries": 2})

    def test_step0_completed_with_warnings_continues(self):
        self.step0_result["status"]["execution_state"] = "completed_with_warnings"
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.step1.assert_called_once()
        self.assertIn("Step 0：completed_with_warnings", stdout)

    def test_step1_completed_with_warnings_is_success(self):
        self.step1_result["status"]["execution_state"] = "completed_with_warnings"
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Step 1：completed_with_warnings", stdout)
        self.assertIn("知識條文數量：2", stdout)

    def test_step0_llm_failed_stops_and_retains_audit_json(self):
        self.step0_result["status"]["execution_state"] = "llm_failed"
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.step1.assert_not_called()
        self.assertTrue(self.paths0[0].is_file())
        self.assertIn("Step 0：llm_failed", stdout)
        self.assertIn("Step 1：未執行", stdout)
        self.assertIn(str(self.paths0[0]), stdout)

    def test_unrecognized_step0_state_is_not_treated_as_success(self):
        self.step0_result["status"]["execution_state"] = "unknown"
        code, _, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.step1.assert_not_called()

    def test_step0_failure_also_stops_with_until_zero(self):
        self.step0_result["status"]["execution_state"] = "llm_failed"
        code, _, _ = self.run_cli("--until", "0")
        self.assertEqual(code, 1)
        self.step1.assert_not_called()

    def test_step0_exceptions_stop_before_step1(self):
        exceptions = [FileNotFoundError("missing"), SourceLoadError("invalid source"),
                      ResultIntegrityError("unsafe result"), ValidationError("invalid schema"), SchemaError("broken schema")]
        for error in exceptions:
            with self.subTest(error=type(error).__name__):
                self.step0.side_effect = error
                code, _, stderr = self.run_cli()
                self.assertEqual(code, 2)
                self.assertIn("Step 0：失敗", stderr)
                self.assertIn("Step 1：未執行", stderr)
                self.step1.assert_not_called()

    def test_step1_llm_failure_preserves_successful_step0(self):
        self.step1.side_effect = Step1LLMError("LLM 呼叫失敗（timeout）")
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(self.paths0[0].read_text(encoding="utf-8")), self.step0_result)
        self.assertIn(str(self.paths0[0]), stdout)
        self.assertIn(f"已保留 Step 0 JSON：{self.paths0[0]}", stderr)
        self.assertIn("Step 1：失敗", stderr)

    def test_step1_validation_and_io_errors_preserve_step0(self):
        for error in (Step1Error("bad boundary"), ValidationError("SECRET_RAW_DATA"), PermissionError("denied")):
            with self.subTest(error=type(error).__name__):
                self.step1.side_effect = error
                code, _, stderr = self.run_cli()
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(self.paths0[-1].read_text(encoding="utf-8")), self.step0_result)
                self.assertIn("已保留 Step 0 JSON", stderr)
                self.assertNotIn("SECRET_RAW_DATA", stderr)

    def test_step1_failed_return_state_does_not_claim_success(self):
        self.step1_result["status"]["execution_state"] = "llm_failed"
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertTrue(self.paths0[0].is_file())
        self.assertIn("Step 1：失敗", stderr)
        self.assertIn("知識條文數量：未產生", stderr)

    def test_same_timestamp_uses_unique_run_directories_and_preserves_history(self):
        before = self.source.read_bytes()
        with patch.object(pipeline, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "20261029-120000-"
            self.assertEqual(self.run_cli()[0], 0)
            first0, first1 = self.paths0[0].read_bytes(), self.paths1[0].read_bytes()
            self.assertEqual(self.run_cli()[0], 0)
        run_dirs = [path.parent.parent for path in self.paths0]
        self.assertNotEqual(run_dirs[0], run_dirs[1])
        self.assertTrue(all(path.name.startswith("20261029-120000-") for path in run_dirs))
        self.assertEqual(self.paths0[0].read_bytes(), first0)
        self.assertEqual(self.paths1[0].read_bytes(), first1)
        self.assertEqual(len(list(self.output_root.rglob("*.json"))), 4)
        self.assertEqual(self.source.read_bytes(), before)

    def test_existing_history_directory_is_not_reused(self):
        history = self.output_root / "old-run" / "step0"
        history.mkdir(parents=True)
        historical_json = history / "actual-returned.step0.json"
        historical_json.write_bytes(b"HISTORICAL_JSON")
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(historical_json.read_bytes(), b"HISTORICAL_JSON")
        self.assertNotEqual(self.paths0[0].parent.parent, history.parent)

    def test_custom_output_root_is_created_and_remains_a_parent(self):
        output_root = self.root / "custom runs" / "nested"
        code, _, _ = self.run_cli("--output-dir", str(output_root))
        self.assertEqual(code, 0)
        self.assertEqual(self.paths0[0].parent.parent.parent, output_root)
        self.assertEqual(self.paths1[0].parent.parent, self.paths0[0].parent.parent)

    def test_blocked_output_root_does_not_call_either_step(self):
        blocked = self.root / "file"
        blocked.write_bytes(b"HISTORY")
        code, _, stderr = self.run_cli("--output-dir", str(blocked))
        self.assertEqual(code, 2)
        self.step0.assert_not_called()
        self.step1.assert_not_called()
        self.assertIn("初始化：失敗", stderr)
        self.assertEqual(blocked.read_bytes(), b"HISTORY")

    def test_directory_reservation_failure_does_not_call_either_step(self):
        with patch.object(pipeline.tempfile, "mkdtemp", side_effect=PermissionError("denied")):
            code, _, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.step0.assert_not_called()
        self.step1.assert_not_called()

    def test_step0_interrupt_stops_step1(self):
        self.step0.side_effect = KeyboardInterrupt()
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 130)
        self.step1.assert_not_called()
        self.assertIn("使用者中斷", stderr)

    def test_step1_interrupt_preserves_successful_step0(self):
        self.step1.side_effect = KeyboardInterrupt()
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 130)
        self.assertTrue(self.paths0[0].is_file())
        self.assertIn(str(self.paths0[0]), stderr)

    def test_zero_statements_is_reported_as_zero(self):
        self.step1_result["knowledge_statements"] = []
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("知識條文數量：0", stdout)

    def test_argument_errors_are_rejected_before_calls_or_directory_creation(self):
        cases = [[], [str(self.source), "--until", "3"], [str(self.source), "--until", "bad"]]
        cases.extend([str(self.source), flag, value] for flag, value in (
            ("--timeout", "0"), ("--timeout", "-1"), ("--timeout", "nan"), ("--timeout", "inf"),
            ("--timeout", "bad"), ("--max-retries", "-1"), ("--max-retries", "1.5")))
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                pipeline.main(args)
            self.assertEqual(caught.exception.code, 2)
        self.step0.assert_not_called()
        self.step1.assert_not_called()
        self.assertFalse(self.output_root.exists())


if __name__ == "__main__":
    unittest.main()
