"""Offline Step 0 acceptance tests. Fixtures live only in temporary directories."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from src.ingestion import SourceLoadError, load_source
from src.llm import AttemptRecord, ErrorKind, GenerationError, GenerationResult
from src.pipeline import step0_preprocess as pipeline
from src.pipeline.step0_preprocess import Step0Preprocessor, run_step0, save_result, validate_result
from src.pipeline.step0_safety import ResultIntegrityError, reconstruct_processed_text
from scripts.run_step0 import main


BASE = "北區控制器負責調節。\n控制器接收資料。\n"


def span(text, needle, start=None):
    position = text.rindex(needle) if start is None else start
    return {"start": position, "end": position + len(needle)}


def candidate(text, original="控制器", replacement="北區控制器", *, start=None, evidence=None, **changes):
    evidence = text.splitlines()[0] if evidence is None else evidence
    result = {
        "original_text": original, "replacement_text": replacement,
        "source_range": span(text, original, start), "evidence_text": evidence,
        "evidence_range": span(text, evidence, text.index(evidence)),
        "reference_type": "elliptical_name", "context": "specific_reference",
        "is_unique": True, "reason": "上下文明確指向證據中的完整名稱。",
    }
    return result | changes


class FakeClient:
    def __init__(self, data=None, *, error=None, attempt_count=1):
        self.data = data if data is not None else {"candidates": [], "unresolved": []}
        self.error = error
        self.attempt_count = attempt_count
        self.calls = []

    def generate_json(self, prompt, schema, **options):
        self.calls.append((prompt, deepcopy(schema), options))
        records = tuple(
            AttemptRecord(number, 0.01, 0 if self.error is None and number == self.attempt_count else 1,
                          self.error if number == self.attempt_count else GenerationError(ErrorKind.TIMEOUT, "timeout"))
            for number in range(1, self.attempt_count + 1)
        )
        return GenerationResult(self.error is None, deepcopy(self.data) if self.error is None else None,
                                self.error, records)


class Step0Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="step0-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Any accidental attempt to use a real process fails the test immediately.
        self.popen = self.enterContext(patch("src.llm.codex_client.subprocess.Popen", side_effect=AssertionError("LIVE LLM FORBIDDEN")))
        self.subprocess_run = self.enterContext(patch("src.llm.codex_client.subprocess.run", side_effect=AssertionError("LIVE PROCESS FORBIDDEN")))

    def source(self, text=BASE, *, name="input.md", raw=None):
        path = self.root / name
        path.write_bytes(text.encode("utf-8") if raw is None else raw)
        return load_source(path)

    def process(self, text=BASE, candidates=None, unresolved=None, *, name="input.md", client=None, **options):
        source = self.source(text, name=name)
        fake = client if client is not None else FakeClient({"candidates": candidates or [], "unresolved": unresolved or []})
        result = Step0Preprocessor(fake).process(source, **options)
        self.assertEqual(result["original_text"], text)
        self.assertEqual(Path(source.path).read_bytes(), text.encode("utf-8"))
        validate_result(result)
        return result, fake

    def assert_rejected(self, result, code, count=1):
        self.assertEqual(result["processed_text"], result["original_text"])
        self.assertEqual(result["replacements"], [])
        self.assertEqual(result["status"]["rejected_count"], count)
        self.assertEqual(result["status"]["candidate_count"], count)
        self.assertIn(code, [item["reason_code"] for item in result["unresolved"]])
        self.assertIn(code, [item["code"] for item in result["warnings"]])

    def test_clear_reference_is_restored_and_prompt_has_full_source(self):
        result, fake = self.process(candidates=[candidate(BASE)], timeout=45, max_retries=2)
        self.assertEqual(result["processed_text"], "北區控制器負責調節。\n北區控制器接收資料。\n")
        self.assertEqual(result["status"]["applied_count"], 1)
        self.assertEqual(result["status"]["execution_state"], "completed")
        self.assertEqual(result["status"]["prompt_id"], "prompts/step0.md")
        self.assertEqual(fake.calls[0][2], {"timeout": 45, "max_retries": 2})
        data = json.loads(fake.calls[0][0].split("\n\nSOURCE_DATA_JSON\n", 1)[1])
        self.assertEqual(data["source_text"], BASE)
        self.assertEqual(data["line_ranges"][-1]["end"], len(BASE))
        self.assertEqual(result["status"]["llm_call_count"], 1)
        self.assertEqual(result["status"]["llm_retry_count"], 0)
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["warnings"], [])

    def test_pronoun_and_demonstrative_references(self):
        for original, category in (("它", "pronoun"), ("該控制器", "demonstrative")):
            with self.subTest(original=original):
                text = f"北區控制器負責調節。\n{original}接收資料。"
                result, _ = self.process(text, [candidate(text, original, reference_type=category)])
                self.assertEqual(result["status"]["applied_count"], 1)

    def test_english_pronoun(self):
        text = "Northern Controller is ready.\nIt receives data."
        result, _ = self.process(text, [candidate(text, "It", "Northern Controller", reference_type="pronoun")])
        self.assertTrue(result["processed_text"].endswith("Northern Controller receives data."))

    def test_ambiguous_reference_is_preserved_with_llm_origin(self):
        text = "北區控制器與南區服務同時運作。\n系統接收資料。"
        item = {"original_text": "系統", "source_range": span(text, "系統"), "reason": "兩個元件都可能是系統，不能唯一辨識。"}
        result, _ = self.process(text, unresolved=[item])
        self.assertEqual(result["processed_text"], text)
        self.assertEqual(result["unresolved"][0]["origin"], "llm")
        self.assertEqual(result["unresolved"][0]["source_range"], item["source_range"])
        self.assertEqual(result["status"]["unresolved_count"], 1)

    def test_non_unique_candidate_is_rejected(self):
        result, _ = self.process(candidates=[candidate(BASE, is_unique=False)])
        self.assert_rejected(result, "ambiguous_reference")

    def test_generic_definition_is_protected_even_if_model_claims_specific(self):
        text = "北區控制器負責調節。\n延遲是指資料進入控制器到命令輸出的時間差。"
        result, _ = self.process(text, [candidate(text)])
        self.assert_rejected(result, "generic_definition")

    def test_definition_section_is_protected_and_ends_at_next_heading(self):
        text = "北區控制器負責調節。\n## 定義\n控制器為通用概念。\n## 運作\n控制器接收資料。"
        first = text.index("控制器為")
        result, _ = self.process(text, [candidate(text, start=first), candidate(text)])
        self.assertEqual(result["status"]["applied_count"], 1)
        self.assertEqual(result["status"]["rejected_count"], 1)
        self.assertIn("控制器為通用概念。", result["processed_text"])
        self.assertTrue(result["processed_text"].endswith("北區控制器接收資料。"))

    def test_model_marked_generic_context_is_rejected(self):
        result, _ = self.process(candidates=[candidate(BASE, context="generic_definition")])
        self.assert_rejected(result, "generic_definition")

    def test_document_self_reference_is_protected(self):
        text = "維護案例記錄調節程序。\n此測試文件描述控制器。"
        item = candidate(text, "此測試文件", "維護案例", reference_type="demonstrative")
        result, _ = self.process(text, [item])
        self.assert_rejected(result, "document_self_reference")

    def test_wrong_offset_is_rejected_without_search_or_repair(self):
        item = candidate(BASE)
        item["source_range"] = {key: value - 1 for key, value in item["source_range"].items()}
        result, _ = self.process(candidates=[item])
        self.assert_rejected(result, "source_text_mismatch")
        self.assertIsNone(result["unresolved"][0]["source_range"])
        self.assertEqual(result["unresolved"][0]["original_text"], "")
        self.assertEqual(result["unresolved"][0]["reported_text"], "控制器")
        self.assertEqual(result["unresolved"][0]["reported_range"], item["source_range"])

    def test_out_of_bounds_reversed_empty_and_negative_ranges(self):
        for reported in ({"start": -1, "end": 2}, {"start": 0, "end": 1000},
                         {"start": 8, "end": 3}, {"start": 5, "end": 5}):
            with self.subTest(reported=reported):
                result, _ = self.process(candidates=[candidate(BASE, source_range=reported)])
                self.assert_rejected(result, "invalid_source_range")

    def test_wrong_evidence_range_and_text_are_rejected(self):
        variants = (
            {"evidence_range": {"start": -1, "end": 2}},
            {"evidence_text": "模型虛構的證據"},
        )
        for changes, code in zip(variants, ("invalid_evidence_range", "evidence_text_mismatch")):
            with self.subTest(changes=changes):
                result, _ = self.process(candidates=[candidate(BASE, **changes)])
                self.assert_rejected(result, code)

    def test_name_must_exist_verbatim_in_evidence(self):
        result, _ = self.process(candidates=[candidate(BASE, replacement="南區控制器")])
        self.assert_rejected(result, "unsupported_replacement")

    def test_target_cannot_be_its_only_evidence(self):
        text = "北區控制器記錄資料。\n該控制器接收資料。"
        item = candidate(text, "該控制器", "控制器", evidence="該控制器接收資料。", reference_type="demonstrative")
        result, _ = self.process(text, [item])
        self.assert_rejected(result, "unsupported_replacement")

    def test_all_overlapping_candidates_are_rejected(self):
        text = "北區控制器負責調節。\n該控制器接收資料。"
        items = [candidate(text, "該控制器", reference_type="demonstrative"), candidate(text)]
        result, _ = self.process(text, items)
        self.assert_rejected(result, "overlapping_candidates", 2)

    def test_duplicate_candidates_are_both_rejected(self):
        item = candidate(BASE)
        result, _ = self.process(candidates=[item, deepcopy(item)])
        self.assert_rejected(result, "overlapping_candidates", 2)

    def test_conflicting_unresolved_and_candidate_is_not_applied(self):
        item = {"original_text": "控制器", "source_range": span(BASE, "控制器"), "reason": "存在歧義。"}
        result, _ = self.process(candidates=[candidate(BASE)], unresolved=[item])
        self.assert_rejected(result, "conflicting_llm_decisions")
        self.assertEqual(result["status"]["unresolved_count"], 2)

    def test_new_unsafe_chinese_english_boundary_is_rejected(self):
        text = "Alpha 控制器負責調節。\n再由控制器接收資料。"
        result, _ = self.process(text, [candidate(text, replacement="Alpha 控制器")])
        self.assert_rejected(result, "unsafe_boundary")
        self.assertNotIn("再由 Alpha", result["processed_text"])

    def test_unsafe_english_word_fragment_is_rejected(self):
        text = "Device 接收資料。\ncritical data"
        item = candidate(text, "it", "Device", start=text.index("it", text.index("critical")), reference_type="pronoun")
        result, _ = self.process(text, [item])
        self.assert_rejected(result, "unsafe_boundary")

    def test_markdown_headings_lists_tables_and_crlf_are_preserved(self):
        text = "北區控制器負責調節。\r\n\r\n# 控制器摘要\r\n\r\n- 控制器接收資料。\r\n\r\n| 元件 | 說明 |\r\n| --- | --- |\r\n| 控制器 | 100 ms 與 90% |\r\n"
        heading = text.index("控制器摘要")
        list_pos = text.index("控制器接收")
        table_pos = text.index("控制器 |")
        items = [candidate(text, start=heading), candidate(text, start=list_pos), candidate(text, start=table_pos)]
        result, _ = self.process(text, items)
        expected = text[:table_pos] + "北區控制器" + text[table_pos + 3:]
        expected = expected[:list_pos] + "北區控制器" + expected[list_pos + 3:]
        self.assertEqual(result["processed_text"], expected)
        self.assertEqual(result["status"]["applied_count"], 2)
        self.assertEqual(result["status"]["rejected_count"], 1)
        self.assertIn("# 控制器摘要\r\n", result["processed_text"])

    def test_fenced_inline_indented_code_url_paths_links_html_and_math(self):
        fragments = (
            "```text\n控制器\n```\n", "~~~text\n控制器\n~~~\n", "`控制器`", "    控制器\n",
            "https://example.test/控制器", "C:\\data\\控制器.txt", "資料/控制器.md",
            "[控制器](help.md)", "<span>控制器</span>", "$控制器$",
            "[控制器]", "[^控制器]", "控制器_id", "<script>\n控制器\n",
            "```text\nnot a closing ``` marker\n控制器\n", "```\n控制器\n",
        )
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                text = "北區控制器負責調節。\n" + fragment
                result, _ = self.process(text, [candidate(text)])
                self.assert_rejected(result, "protected_content")

    def test_front_matter_and_bom_heading_are_preserved(self):
        for text in ("---\nname: 控制器\n---\n北區控制器負責調節。",
                     "\ufeff# 控制器\n北區控制器負責調節。"):
            with self.subTest(text=text):
                item = candidate(text, start=text.index("控制器"), evidence="北區控制器負責調節。")
                result, _ = self.process(text, [item])
                self.assertEqual(result["status"]["applied_count"], 0)

    def test_numbers_units_terms_actions_and_punctuation_are_not_rewritten(self):
        variants = (
            ("99% 與 12%", "12%", "99%", "numeric_content"),
            ("seconds 與 ms", "ms", "seconds", "term_changed"),
            ("LQR 與 PID", "PID", "LQR", "term_changed"),
            ("不會停止。\n會停止。", "會停止", "不會停止", "non_reference_edit"),
            ("北區控制器，負責調節。\n控制器，", "控制器，", "北區控制器，", "non_reference_edit"),
        )
        for text, original, replacement, code in variants:
            with self.subTest(original=original):
                result, _ = self.process(text, [candidate(text, original, replacement)])
                self.assert_rejected(result, code)

    def test_multiple_unsorted_candidates_have_python_computed_offsets(self):
        text = "北區控制器與南區服務協同運作。\n控制器接收資料，服務保存結果。"
        first = candidate(text)
        second = candidate(text, "服務", "南區服務")
        result, _ = self.process(text, [second, first])
        self.assertEqual(result["processed_text"], "北區控制器與南區服務協同運作。\n北區控制器接收資料，南區服務保存結果。")
        self.assertEqual(result["status"]["applied_count"], 2)
        for replacement in result["replacements"]:
            processed_range = replacement["processed_range"]
            self.assertEqual(result["processed_text"][processed_range["start"]:processed_range["end"]], replacement["replacement_text"])
        later = result["replacements"][1]
        self.assertEqual(later["processed_range"]["start"], later["source_range"]["start"] + 2)

    def test_unicode_offsets_are_codepoints_and_utf16_guess_is_rejected(self):
        text = "😀北區控制器負責調節。\n控制器接收資料。"
        good = candidate(text)
        result, _ = self.process(text, [good])
        self.assertEqual(result["status"]["applied_count"], 1)
        wrong = deepcopy(good)
        wrong["source_range"] = {key: value + 1 for key, value in wrong["source_range"].items()}
        result, _ = self.process(text, [wrong])
        self.assert_rejected(result, "source_text_mismatch")

    def test_loader_hash_size_bom_crlf_and_character_count(self):
        raw = b"\xef\xbb\xbf" + "資料\r\n第二行\n".encode("utf-8")
        source = self.source(raw=raw)
        self.assertEqual(source.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(source.size_bytes, len(raw))
        self.assertEqual(source.original_text.encode("utf-8"), raw)
        self.assertEqual(source.metadata()["char_count"], len(raw.decode("utf-8")))

    def test_txt_input_is_supported_without_normalization(self):
        text = BASE.replace("\n", "\r\n")
        result, _ = self.process(text, [candidate(text)], name="input.TXT")
        self.assertEqual(result["source"]["document_type"], "txt")
        self.assertEqual(result["processed_text"].count("\r\n"), 2)

    def test_loader_rejects_missing_directory_unsupported_and_non_utf8(self):
        with self.assertRaises(FileNotFoundError):
            load_source(self.root / "missing.md")
        with self.assertRaises(SourceLoadError):
            load_source(self.root)
        for name, raw in (("bad.pdf", b"data"), ("bad.md", b"\xff\xfe")):
            path = self.root / name
            path.write_bytes(raw)
            with self.subTest(name=name), self.assertRaises(SourceLoadError):
                load_source(path)

    def test_reconstruction_detects_unrecorded_edits_and_offset_tampering(self):
        result, _ = self.process(candidates=[candidate(BASE)])
        self.assertEqual(reconstruct_processed_text(BASE, result["replacements"]), result["processed_text"])
        for field in ("processed_text", "processed_range", "evidence_text", "source_hash", "counts"):
            broken = deepcopy(result)
            if field == "processed_text": broken[field] += " 未授權改寫"
            elif field == "processed_range": broken["replacements"][0][field]["end"] += 1
            elif field == "evidence_text": broken["replacements"][0][field] = "虛構"
            elif field == "source_hash": broken["source"]["sha256"] = "0" * 64
            else: broken["status"]["applied_count"] += 1
            with self.subTest(field=field), self.assertRaises(ResultIntegrityError):
                validate_result(broken)

    def test_llm_response_schema_is_enforced_before_any_edit(self):
        for data in ({}, {"candidates": [], "unresolved": [], "processed_text": "改寫"},
                     {"candidates": [{}], "unresolved": []},
                     {"candidates": [candidate(BASE, source_range={"start": True, "end": 4})], "unresolved": []}):
            with self.subTest(data=data):
                result, _ = self.process(client=FakeClient(data))
                self.assertEqual(result["status"]["execution_state"], "llm_failed")
                self.assertEqual(result["status"]["llm_error_kind"], "invalid_llm_response")
                self.assertEqual(result["processed_text"], BASE)

    def test_result_schema_rejects_missing_fields_and_extra_knowledge_ids(self):
        result, _ = self.process()
        schema = pipeline._schema("step0_result.schema.json")
        Draft202012Validator(schema).validate(result)
        for change in ("missing_original", "knowledge_item_id", "bad_range"):
            broken = deepcopy(result)
            if change == "missing_original": del broken["original_text"]
            elif change == "knowledge_item_id": broken["knowledge_item_id"] = "not-allowed"
            else: broken["status"]["llm_call_count"] = -1
            with self.subTest(change=change), self.assertRaises(ValidationError):
                Draft202012Validator(schema).validate(broken)

    def test_invalid_unresolved_offsets_are_kept_as_unverified_reports(self):
        item = {"original_text": "系統", "source_range": {"start": -4, "end": 999}, "reason": "不能判定。"}
        result, _ = self.process(unresolved=[item])
        unresolved = result["unresolved"][0]
        self.assertEqual(unresolved["origin"], "python_validation")
        self.assertIsNone(unresolved["source_range"])
        self.assertEqual(unresolved["reported_range"], item["source_range"])
        self.assertEqual(unresolved["reported_text"], "系統")
        self.assertEqual(result["warnings"][0]["code"], "invalid_unresolved_range")

    def test_llm_failures_and_timeout_preserve_full_source_and_counts(self):
        for kind, attempts in ((ErrorKind.TIMEOUT, 3), (ErrorKind.CLI_EXECUTION, 1), (ErrorKind.CLI_NOT_FOUND, 0)):
            with self.subTest(kind=kind):
                fake = FakeClient(error=GenerationError(kind, "SECRET_RAW_DIAGNOSTICS"), attempt_count=attempts)
                result, _ = self.process(client=fake, max_retries=2)
                self.assertEqual(result["status"]["execution_state"], "llm_failed")
                self.assertEqual(result["processed_text"], BASE)
                self.assertEqual(result["status"]["llm_call_count"], attempts)
                self.assertEqual(result["status"]["llm_retry_count"], max(0, attempts - 1))
                self.assertEqual(result["unresolved"][0]["origin"], "llm_failure")
                self.assertNotIn("SECRET_RAW_DIAGNOSTICS", json.dumps(result))
                self.assertEqual(len(fake.calls), 1)

    def test_empty_or_whitespace_source_skips_llm(self):
        for text in ("", "\ufeff", " \t\r\n"):
            with self.subTest(text=text):
                result, fake = self.process(text)
                self.assertEqual(result["processed_text"], text)
                self.assertEqual(result["status"]["llm_call_count"], 0)
                self.assertEqual(fake.calls, [])

    def test_success_after_client_retry_records_actual_attempts(self):
        fake = FakeClient({"candidates": [candidate(BASE)], "unresolved": []}, attempt_count=2)
        result, _ = self.process(client=fake, max_retries=1)
        self.assertEqual(result["status"]["llm_call_count"], 2)
        self.assertEqual(result["status"]["llm_retry_count"], 1)
        self.assertEqual(len(fake.calls), 1)

    def test_structure_backstop_rolls_back_all_accepted_edits(self):
        signature = pipeline.structure_signature

        def simulate_structure_change(text, document_type):
            return signature(text, document_type) if text == BASE else ("changed",)

        with patch.object(pipeline, "structure_signature", side_effect=simulate_structure_change):
            result, _ = self.process(candidates=[candidate(BASE)])
        self.assert_rejected(result, "structure_changed")

    def test_real_sample_is_retained_in_full_with_fake_client(self):
        path = Path(__file__).resolve().parents[1] / "source" / "sample_technical_incident.md"
        before = path.read_bytes()
        result = Step0Preprocessor(FakeClient()).process(load_source(path))
        self.assertEqual(result["original_text"].encode("utf-8"), before)
        self.assertEqual(result["processed_text"], result["original_text"])
        self.assertEqual(result["source"]["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(path.read_bytes(), before)
        validate_result(result)

    def test_invalid_options_do_not_call_client(self):
        source = self.source()
        fake = FakeClient()
        processor = Step0Preprocessor(fake)
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": True},
                        {"max_retries": -1}, {"max_retries": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                processor.process(source, **options)
        self.assertEqual(fake.calls, [])

    def test_save_creates_complete_json_and_never_overwrites(self):
        source = self.source()
        before = Path(source.path).read_bytes()
        out = self.root / "results" / "nested"
        fake = FakeClient({"candidates": [candidate(BASE)], "unresolved": []})
        result, path = run_step0(source.path, out, client=fake)
        self.assertEqual(path.name, "input.step0.json")
        self.assertEqual(json.loads(path.read_bytes().decode("utf-8")), result)
        self.assertEqual(list(out.iterdir()), [path])
        original_output = path.read_bytes()
        second_fake = FakeClient()
        with self.assertRaises(FileExistsError):
            run_step0(source.path, out, client=second_fake)
        self.assertEqual(second_fake.calls, [])
        self.assertEqual(path.read_bytes(), original_output)
        self.assertEqual(Path(source.path).read_bytes(), before)

    def test_save_result_refuses_source_path_and_invalid_results(self):
        result, _ = self.process()
        with self.assertRaises(ValueError):
            save_result(result, result["source"]["path"])
        result["processed_text"] = "擅自改寫"
        destination = self.root / "invalid.json"
        with self.assertRaises(ResultIntegrityError):
            save_result(result, destination)
        self.assertFalse(destination.exists())

    def test_atomic_publish_race_does_not_overwrite_or_leave_temp_files(self):
        result, _ = self.process()
        path = self.root / "result.json"

        def racing_publish(temporary, destination):
            Path(destination).write_bytes(b"EXISTING_RESULT")
            raise FileExistsError("another writer won")

        with patch.object(pipeline.os, "link", side_effect=racing_publish):
            with self.assertRaises(FileExistsError):
                save_result(result, path)
        self.assertEqual(path.read_bytes(), b"EXISTING_RESULT")
        self.assertEqual(list(self.root.glob(".step0-*.tmp")), [])

    def test_publish_io_failure_cleans_temporary_file(self):
        result, _ = self.process()
        with patch.object(pipeline.os, "link", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                save_result(result, self.root / "result.json")
        self.assertEqual(list(self.root.glob(".step0-*.tmp")), [])

    def test_cli_success_uses_fake_and_writes_independent_output(self):
        source = self.source()
        out = self.root / "cli-output"
        fake = FakeClient({"candidates": [candidate(BASE)], "unresolved": []})
        with redirect_stdout(io.StringIO()) as stdout:
            code = main(["--input", source.path, "--output-dir", str(out), "--timeout", "30", "--max-retries", "0"], client=fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"]["applied_count"], 1)
        self.assertTrue((out / "input.step0.json").is_file())
        self.assertEqual(fake.calls[0][2], {"timeout": 30.0, "max_retries": 0})

    def test_cli_invalid_input_and_output_directory_do_not_call_llm(self):
        source = self.source()
        blocked = self.root / "blocked"
        blocked.write_bytes(b"a file, not directory")
        for input_path, out in ((self.root / "missing.md", self.root / "out"), (source.path, blocked)):
            fake = FakeClient()
            with self.subTest(input_path=input_path), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--input", str(input_path), "--output-dir", str(out)], client=fake), 2)
            self.assertEqual(fake.calls, [])

    def test_cli_argument_errors_are_rejected(self):
        for option, value in (("--timeout", "0"), ("--timeout", "nan"), ("--timeout", "inf"),
                              ("--max-retries", "-1"), ("--max-retries", "1.5")):
            with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(["--input", "x.md", "--output-dir", "out", option, value], client=FakeClient())
                self.assertEqual(caught.exception.code, 2)

    def test_output_permission_check_fails_before_llm_call(self):
        source = self.source()
        fake = FakeClient()
        with patch.object(pipeline.tempfile, "TemporaryFile", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                run_step0(source.path, self.root / "blocked-output", client=fake)
        self.assertEqual(fake.calls, [])

    def test_cli_llm_failure_still_saves_auditable_unchanged_json(self):
        source = self.source()
        out = self.root / "failed"
        fake = FakeClient(error=GenerationError(ErrorKind.TIMEOUT, "timeout"))
        with redirect_stdout(io.StringIO()):
            code = main(["--input", source.path, "--output-dir", str(out)], client=fake)
        self.assertEqual(code, 1)
        result = json.loads((out / "input.step0.json").read_bytes().decode("utf-8"))
        self.assertEqual(result["processed_text"], BASE)
        self.assertEqual(result["status"]["llm_error_kind"], "timeout")


if __name__ == "__main__":
    unittest.main()
