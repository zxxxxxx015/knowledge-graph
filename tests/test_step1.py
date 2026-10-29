"""Offline contracts: fake boundaries are annotations, not a live semantic eval."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from scripts.run_step1 import main
from src.ingestion import load_source
from src.llm import AttemptRecord, ErrorKind, GenerationError, GenerationResult
from src.pipeline.step0_preprocess import Step0Preprocessor
from src.pipeline.step0_safety import ResultIntegrityError, line_ranges
from src.pipeline import step1_split as pipeline
from src.pipeline.step1_split import (
    Step1Error, Step1Splitter, analyze_structure, load_step0, original_mapping,
    run_step1, save_result, schema, statement_id, validate_result,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, data=None, *, error=None, attempts=1):
        self.data = data if data is not None else {"statements": [], "unhandled": []}
        self.error, self.attempts, self.calls = error, attempts, []

    def generate_json(self, prompt, schema, **options):
        self.calls.append((prompt, deepcopy(schema), options))
        records = tuple(AttemptRecord(i + 1, 0.01, 0, self.error) for i in range(self.attempts))
        return GenerationResult(self.error is None, deepcopy(self.data), self.error, records)


def decision(text, start=0, end=None, *, kind="prose", reason="complete_sentence"):
    end = len(text) if end is None else end
    return {"start": start, "end": end, "text": text[start:end], "kind": kind,
            "boundary_reason": reason, "reason": "離線人工標註：依完整陳述或明確相鄰語意依賴決定邊界。"}


class Step1Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="step1-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.enterContext(patch("src.llm.codex_client.subprocess.Popen", side_effect=AssertionError("LIVE LLM FORBIDDEN")))
        self.enterContext(patch("src.llm.codex_client.subprocess.run", side_effect=AssertionError("LIVE PROCESS FORBIDDEN")))

    def input(self, text="設備接收資料。", *, source_name="source.md", step0_data=None):
        source = self.root / source_name
        source.write_bytes(text.encode("utf-8"))
        if step0_data is None:
            step0_data = Step0Preprocessor(FakeClient({"candidates": [], "unresolved": []})).process(load_source(source))
        path = self.root / "fixture.step0.json"
        path.write_bytes((json.dumps(step0_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return load_step0(path)

    def process(self, text, items=None, *, unhandled=None, snapshot=None, fake=None, **options):
        snapshot = snapshot or self.input(text)
        fake = fake or FakeClient({"statements": items if items is not None else [decision(text)], "unhandled": unhandled or []})
        result = Step1Splitter(fake).process(snapshot, **options)
        validate_result(result, snapshot)
        self.assertEqual(result["processed_text"], snapshot.data["processed_text"])
        return result, fake

    def test_complete_single_sentence(self):
        text = "設備接收資料。"
        result, _ = self.process(text)
        self.assertEqual([s["text"] for s in result["knowledge_statements"]], [text])

    def test_single_sentence_with_two_actions_stays_whole(self):
        text = "設備每 100 ms 接收資料，並依照控制邏輯更新輸出。"
        result, _ = self.process(text)
        self.assertEqual(len(result["knowledge_statements"]), 1)

    def test_two_independent_sentences(self):
        text = "設備接收資料。設備更新輸出。"
        cut = text.index("。") + 1
        result, _ = self.process(text, [decision(text, 0, cut, reason="independent_sentence"), decision(text, cut, reason="independent_sentence")])
        self.assertEqual(len(result["knowledge_statements"]), 2)

    def test_lead_sentence_and_incomplete_list_merge(self):
        text = "模式啟動後，操作員應檢查：\n- 負載\n- 佇列\n- 封包"
        result, _ = self.process(text, [decision(text, kind="list", reason="lead_list_dependency")])
        self.assertEqual(result["knowledge_statements"][0]["text"], text)

    def test_independent_complete_list_items_are_separate(self):
        text = "- 設備接收資料。\n- 服務保存紀錄。\n"
        items = [decision(text, **s, kind="list", reason="independent_sentence") for s in line_ranges(text)]
        result, _ = self.process(text, items)
        self.assertEqual(len(result["knowledge_statements"]), 2)
        self.assertTrue(all(s["text"].startswith("- ") for s in result["knowledge_statements"]))

    def test_two_causal_sentences_merge(self):
        text = "負載增加造成服務延遲。因此，資料無法及時更新。"
        result, _ = self.process(text, [decision(text, reason="causal_dependency")])
        self.assertEqual(result["knowledge_statements"][0]["text"], text)

    def test_three_or_more_causal_sentences_preserve_lines(self):
        lines = ["- 延遲造成輸出變慢。", "- 輸出變慢導致調節時間拉長。", "- 調節時間拉長造成流量波動。", "- 流量波動造成產品不穩。"]
        text = "\n".join(lines)
        result, _ = self.process(text, [decision(text, kind="list", reason="causal_chain")])
        self.assertEqual(len(result["knowledge_statements"]), 1)
        self.assertEqual(result["knowledge_statements"][0]["text"].splitlines(), lines)
        self.assertNotIn("→", result["knowledge_statements"][0]["text"])

    def test_same_topic_does_not_require_merge(self):
        text = "設備接收資料。設備外殼為金屬。設備具有兩個接頭。"
        items = [decision(text, m.start(), m.end(), reason="independent_sentence") for m in re.finditer(r"[^。]+。", text)]
        result, _ = self.process(text, items)
        self.assertEqual(len(result["knowledge_statements"]), 3)

    def test_whitespace_crlf_emoji_punctuation_units_preserved(self):
        text = "  😀設備以 12% 變化率更新；間隔 100 ms。\r\n第二行  保留。\r\n"
        result, _ = self.process(text)
        self.assertEqual(result["knowledge_statements"][0]["text"].encode("utf-8"), text.encode("utf-8"))
        self.assertEqual(result["knowledge_statements"][0]["original_mapping"]["text"], text)

    def test_heading_parent_is_ancestor_not_previous_sibling(self):
        text = "# 文件\n\n## 背景\n\n設備運作。\n\n## 指標\n\n資料更新。"
        items = [decision(text, text.index(s), text.index(s) + len(s)) for s in ("設備運作。", "資料更新。")]
        result, _ = self.process(text, items)
        headings = result["structure"]["headings"]
        self.assertEqual(headings[2]["parent_id"], headings[0]["heading_id"])
        self.assertEqual([h["title"] for h in result["knowledge_statements"][1]["heading_context"]], ["文件", "指標"])

    def test_setext_and_skipped_heading_levels(self):
        text = "文件\n====\n\n### 子節\n資料更新。\n\n## 同層分支\n設備運作。"
        items = [decision(text, text.index(s), text.index(s) + len(s)) for s in ("資料更新。", "設備運作。")]
        result, _ = self.process(text, items)
        headings = result["structure"]["headings"]
        self.assertEqual([h["level"] for h in headings], [1, 3, 2])
        self.assertEqual(headings[2]["parent_id"], headings[0]["heading_id"])

    def test_unhandled_heading_context_contains_only_parent_id_ancestors(self):
        examples = [
            ("# 文件\n## 背景\n### 細節\n## 指標\n### 正常\n#### 深入\n### 異常\n# 第二份\n## 概況\n",
             [[], ["文件"], ["文件", "背景"], ["文件"], ["文件", "指標"],
              ["文件", "指標", "正常"], ["文件", "指標"], [], ["第二份"]]),
            ("文件\n====\n\n### 子節\n\n## 同層分支\n",
             [[], ["文件"], ["文件"]]),
        ]
        for text, expected in examples:
            with self.subTest(text=text):
                result, fake = self.process(text, [])
                regions = [r for r in result["unhandled_regions"] if r["reason_code"] == "heading_context"]
                self.assertEqual([[h["title"] for h in r["heading_context"]] for r in regions], expected)
                self.assertEqual(fake.calls, [])

    def test_unhandled_heading_context_rejects_previous_sibling_in_validation(self):
        text = "# 文件\n## 背景\n## 指標\n"
        result, _ = self.process(text, [])
        broken = deepcopy(result)
        broken["unhandled_regions"][-1]["heading_context"] = deepcopy(result["structure"]["headings"][:2])
        with self.assertRaisesRegex(Step1Error, "未處理區域"):
            validate_result(broken)

    def test_unhandled_heading_context_accepted_sample_preserves_all_29_statements(self):
        # Replay the already accepted boundaries through a Fake Client only.
        # Read historical JSON as evidence; never rewrite or validate it as new output.
        path = ROOT / "outputs/step1/20261029-150010/sample_technical_incident.step1.json"
        before = path.read_bytes()
        accepted = json.loads(before.decode("utf-8"))
        snapshot = load_step0(accepted["input"]["step0_json_path"])
        items = [{**s["processed_range"], "text": s["text"], "kind": s["kind"],
                  "boundary_reason": s["boundary_reason"], "reason": s["boundary_detail"]}
                 for s in accepted["knowledge_statements"]]
        unhandled = [{**r["processed_range"], "text": r["text"], "reason_code": r["reason_code"], "reason": r["reason"]}
                     for r in accepted["unhandled_regions"] if r["reason_code"] not in pipeline.STRUCTURAL_REASONS]
        result, _ = self.process(snapshot.data["processed_text"], items, unhandled=unhandled, snapshot=snapshot)
        self.assertEqual(len(result["knowledge_statements"]), 29)
        self.assertEqual(result["knowledge_statements"], accepted["knowledge_statements"])
        headings = {h["heading_id"]: h for h in result["structure"]["headings"]}
        changed = 0
        heading_count = 0
        for old, new in zip(accepted["unhandled_regions"], result["unhandled_regions"], strict=True):
            self.assertEqual({k: v for k, v in old.items() if k != "heading_context"},
                             {k: v for k, v in new.items() if k != "heading_context"})
            if new["reason_code"] != "heading_context":
                self.assertEqual(old, new)
                continue
            heading_count += 1
            heading = next(h for h in headings.values() if h["processed_range"] == new["processed_range"])
            ancestors = []
            parent_id = heading["parent_id"]
            while parent_id is not None:
                parent = headings[parent_id]
                ancestors.insert(0, parent)
                parent_id = parent["parent_id"]
            self.assertEqual(new["heading_context"], ancestors)
            changed += old["heading_context"] != new["heading_context"]
        self.assertEqual(heading_count, 10)
        self.assertEqual(changed, 8)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(Path(snapshot.path).read_bytes(), snapshot.raw)

    def table(self):
        return "# 文件\n\n## 測試\n\n| 項目 | 條件 | 結果 |\n| --- | :--- | ---: |\n| 一 | 負載過高 | 發出警告 |\n| 二 | 資料逾時 | 標記過期 |\n| 三 | 佇列過長 | 延後更新 |\n"

    def table_items(self, text, snapshot):
        table = analyze_structure(snapshot.data)["tables"][0]
        return [decision(text, **row["processed_range"], kind="table", reason="table_row") for row in table["rows"]]

    def test_table_three_independent_rows(self):
        text = self.table()
        snapshot = self.input(text)
        result, _ = self.process(text, self.table_items(text, snapshot), snapshot=snapshot)
        self.assertEqual(len(result["knowledge_statements"]), 3)
        self.assertEqual([r["reason_code"] for r in result["unhandled_regions"]], ["heading_context", "heading_context", "table_structure"])

    def test_table_header_cell_positions_and_original_mapping(self):
        text = self.table()
        snapshot = self.input(text)
        result, _ = self.process(text, self.table_items(text, snapshot), snapshot=snapshot)
        for statement in result["knowledge_statements"]:
            table = statement["table_context"]
            self.assertEqual(table["text"], text[table["processed_range"]["start"]:])
            self.assertEqual([h["title"] for h in statement["heading_context"]], ["文件", "測試"])
            for cell in table["rows"][0]["cells"]:
                header = table["header"]["cells"][cell["column_index"]]
                self.assertEqual(cell["header_text"], header["text"])
                self.assertEqual(cell["header_range"], header["processed_range"])
                self.assertEqual(cell["text"], pipeline._slice(text, cell["processed_range"]))
                self.assertEqual(cell["original_mapping"]["text"], cell["text"])

    def test_dependent_adjacent_table_rows_can_merge(self):
        text = "| 條件 | 結果 |\n| --- | --- |\n| 高負載 | 延遲增加 |\n| 延遲增加 | 輸出變慢 |\n"
        snapshot = self.input(text)
        table = analyze_structure(snapshot.data)["tables"][0]
        item = decision(text, table["rows"][0]["processed_range"]["start"], table["rows"][-1]["processed_range"]["end"], kind="table", reason="table_dependency")
        result, _ = self.process(text, [item], snapshot=snapshot)
        self.assertEqual(len(result["knowledge_statements"]), 1)
        self.assertEqual(len(result["knowledge_statements"][0]["table_context"]["rows"]), 2)

    def test_complex_tables_remain_pending_without_model_call(self):
        examples = [
            "| 甲 | 乙 |\n| 內容 | 資料 |\n",  # Missing separator.
            "| 甲 | 乙 |\n| --- | --- |\n| 一 | 二 | 三 |\n",
            "| 甲 | 乙 |\n| --- | --- |\n| 一 | 二 |\n多行儲存格續行\n",
            "| 甲 | 乙 |\n| --- | --- |\n| 一 | |\n",
            "| 甲 | 乙 |\n| --- | --- |\n| 一 | ^ |\n",
            "| 甲 | 乙 |\n| --- | --- |\n| 一<br>二 | 三 |\n",
            "| 甲 | 甲 |\n| --- | --- |\n| 一 | 二 |\n",
            "| 甲 | 乙 |\n| --- | --- |\n| `a|b` | 三 |\n",
            "<table>\n<tr><td>資料</td></tr>\n\n</table>\n",
        ]
        for text in examples:
            with self.subTest(text=text):
                result, fake = self.process(text, [])
                self.assertEqual(result["knowledge_statements"], [])
                self.assertEqual(result["unhandled_regions"][0]["reason_code"], "table_pending")
                self.assertEqual(result["unhandled_regions"][0]["text"], text)
                self.assertEqual(fake.calls, [])

    def test_escaped_pipe_cells_have_exact_spans(self):
        text = "甲 | 乙\r\n--- | ---\r\n一\\|二 | 三\r\n"
        snapshot = self.input(text)
        result, _ = self.process(text, self.table_items(text, snapshot), snapshot=snapshot)
        cell = result["knowledge_statements"][0]["table_context"]["rows"][0]["cells"][0]
        self.assertEqual(cell["text"], "一\\|二 ")

    def replaced_input(self):
        text = "北區設備負責調節。\n設備接收資料。\n設備更新輸出。"
        evidence = text.splitlines()[0]
        candidates = []
        for match in list(re.finditer("設備", text))[1:]:
            candidates.append({"original_text": "設備", "replacement_text": "北區設備", "source_range": {"start": match.start(), "end": match.end()},
                               "evidence_text": evidence, "evidence_range": {"start": 0, "end": len(evidence)}, "reference_type": "elliptical_name",
                               "context": "specific_reference", "is_unique": True, "reason": "前文明確指定設備名稱。"})
        source = self.root / "source.md"
        source.write_bytes(text.encode("utf-8"))
        data = Step0Preprocessor(FakeClient({"candidates": candidates, "unresolved": []})).process(load_source(source))
        self.assertEqual(len(data["replacements"]), 2)
        return self.input(text, step0_data=data)

    def test_step0_replacements_map_multiple_length_changes(self):
        snapshot = self.replaced_input()
        text = snapshot.data["processed_text"]
        items = [decision(text, **s) for s in line_ranges(text)]
        result, _ = self.process(text, items, snapshot=snapshot)
        self.assertEqual([s["original_mapping"]["text"] for s in result["knowledge_statements"]], snapshot.data["original_text"].splitlines(keepends=True))
        self.assertEqual(result["knowledge_statements"][-1]["step0_trace"]["replacement_indexes"], [1])

    def test_boundary_inside_replacement_is_unavailable_not_guessed(self):
        snapshot = self.replaced_input()
        text = snapshot.data["processed_text"]
        cut = snapshot.data["replacements"][0]["processed_range"]["start"] + 1
        # Deliberately unrealistic semantic annotation exercises mapping guard.
        result, _ = self.process(text, [decision(text, 0, cut), decision(text, cut)], snapshot=snapshot)
        for item in result["knowledge_statements"]:
            self.assertEqual(item["original_mapping"]["status"], "unavailable")
            self.assertIsNone(item["original_mapping"]["source_range"])
        self.assertIn("mapping_unavailable", [w["code"] for w in result["warnings"]])

    def test_mapping_at_both_replacement_edges_is_exact(self):
        snapshot = self.replaced_input()
        data = snapshot.data
        for replacement in data["replacements"]:
            mapping = original_mapping(data, replacement["processed_range"])
            self.assertEqual(mapping["source_range"], replacement["source_range"])
            self.assertEqual(mapping["text"], replacement["original_text"])

    def unresolved_input(self):
        snapshot = self.replaced_input()
        data = snapshot.data
        pos = data["original_text"].index("接收")
        source_range = {"start": pos, "end": pos + 2}
        data["unresolved"] = [{"original_text": "接收", "source_range": source_range, "reported_text": "接收", "reported_range": source_range,
                                "proposed_replacement": None, "reason_code": "ambiguous_reference", "reason": "離線追溯測試紀錄。", "origin": "python_validation", "candidate_index": None}]
        data["warnings"] = [{"code": "preserved", "message": "離線追溯測試紀錄。", "source_range": source_range, "candidate_index": None},
                            {"code": "unlocated", "message": "位置不能驗證。", "source_range": None, "candidate_index": None}]
        data["unresolved"].append({"original_text": "", "source_range": None, "reported_text": "未知", "reported_range": {"start": -1, "end": 999},
                                   "proposed_replacement": None, "reason_code": "invalid_unresolved_range", "reason": "位置無效。", "origin": "python_validation", "candidate_index": None})
        data["status"].update(execution_state="completed_with_warnings", unresolved_count=2, warning_count=2)
        return self.input(data["original_text"], step0_data=data)

    def test_new_step0_schema_completed_with_warnings_and_trace(self):
        snapshot = self.unresolved_input()
        text = snapshot.data["processed_text"]
        result, _ = self.process(text, [decision(text, **s) for s in line_ranges(text)], snapshot=snapshot)
        self.assertIn("document_type", result["source"])
        self.assertNotIn("type", result["source"])
        self.assertEqual(result["step0_provenance"], {k: snapshot.data[k] for k in ("replacements", "unresolved", "warnings", "status")})
        second = result["knowledge_statements"][1]["step0_trace"]
        self.assertEqual(second["unresolved_indexes"], [0])
        self.assertEqual(second["warning_indexes"], [0])
        self.assertEqual(second["unlocated_unresolved_indexes"], [1])
        self.assertEqual(second["unlocated_warning_indexes"], [1])
        self.assertEqual(result["knowledge_statements"][0]["step0_trace"]["unresolved_indexes"], [])
        self.assertEqual(result["step0_provenance"]["unresolved"][0]["origin"], "python_validation")

    def test_step0_failed_state_rejected_before_client(self):
        source = self.root / "failed.md"
        source.write_text("設備更新。", encoding="utf-8")
        error = GenerationError(ErrorKind.TIMEOUT, "secret")
        data = Step0Preprocessor(FakeClient(error=error)).process(load_source(source))
        path = self.root / "failed.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(Step1Error):
            load_step0(path)

    def test_wrong_offset_or_text_is_rejected_without_repair(self):
        text = "😀設備更新。"
        for change in ({"start": 1}, {"end": len(text) - 1}, {"text": "設備已更新。"}, {"start": True}):
            with self.subTest(change=change), self.assertRaises(Step1Error):
                self.process(text, [decision(text) | change])

    def test_overlap_duplicate_out_of_bounds_empty_negative_rejected(self):
        text = "設備接收資料。"
        cases = [[decision(text), decision(text)], [decision(text, 0, 4), decision(text, 3)],
                 [decision(text) | {"end": 999}], [decision(text) | {"start": -1}], [decision(text, 1, 1)], [decision(text, 4, 2)]]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(Step1Error):
                self.process(text, items)

    def test_omitted_nonwhitespace_prose_rejected(self):
        text = "設備更新。\n重要紀錄。"
        with self.assertRaisesRegex(Step1Error, "未記錄"):
            self.process(text, [decision(text, 0, 5)])

    def test_whitespace_gaps_allowed(self):
        text = " \t設備更新。\n\n"
        result, _ = self.process(text, [decision(text, 2, 7)])
        self.assertEqual(result["processed_text"], text)

    def test_explicit_semantic_pending_is_preserved_with_warning(self):
        text = "可能尚未完整的片段"
        region = {"start": 0, "end": len(text), "text": text, "reason_code": "incomplete_statement", "reason": "無法安全判定為完整陳述。"}
        result, _ = self.process(text, [], unhandled=[region])
        self.assertEqual(result["unhandled_regions"][0]["text"], text)
        self.assertEqual(result["status"]["execution_state"], "completed_with_warnings")

    def test_statement_and_unhandled_overlap_rejected(self):
        text = "設備更新。"
        region = {"start": 0, "end": len(text), "text": text, "reason_code": "semantic_pending", "reason": "未確認。"}
        with self.assertRaises(Step1Error):
            self.process(text, unhandled=[region])

    def test_heading_or_code_cannot_become_prose(self):
        for text in ("# 標題\n設備更新。", "```py\nprint('text。')\n```\n設備更新。"):
            with self.subTest(text=text), self.assertRaises(Step1Error):
                self.process(text)

    def test_code_blocks_never_split_as_sentences(self):
        examples = ["```py\nprint('text。')\n```\n", "~~~text\n資料。\n~~~\n", "```\n未閉合。\n", "    code。\n\n    second。\n", "- ```py\n  code。\n  ```\n", "<pre>\ncode。\n\nsecond。\n</pre>\n"]
        for text in examples:
            with self.subTest(text=text):
                result, fake = self.process(text, [])
                self.assertEqual(result["unhandled_regions"][0]["reason_code"], "code_block")
                self.assertEqual(fake.calls, [])

    def test_inline_code_cannot_be_cut(self):
        text = "設備執行 `x.y()` 並更新。"
        with self.assertRaisesRegex(Step1Error, "inline code"):
            self.process(text, [decision(text, 0, 8), decision(text, 8)])
        self.process(text)

    def test_table_partial_row_wrong_kind_and_unjustified_merge_rejected(self):
        text = self.table()
        snapshot = self.input(text)
        items = self.table_items(text, snapshot)
        partial = deepcopy(items)
        partial[0]["start"] += 1
        partial[0]["text"] = partial[0]["text"][1:]
        wrong_kind = deepcopy(items)
        wrong_kind[0]["kind"] = "prose"
        unjustified = [decision(text, items[0]["start"], items[-1]["end"], kind="table", reason="table_row")]
        for decisions in (partial, wrong_kind, unjustified):
            with self.subTest(items=decisions), self.assertRaises(Step1Error):
                self.process(text, decisions, snapshot=snapshot)

    def test_stable_sha256_ids_are_reproducible_and_source_sensitive(self):
        text = "設備更新。"
        first, _ = self.process(text)
        second, _ = self.process(text)
        item = first["knowledge_statements"][0]
        self.assertEqual(item["statement_id"], second["knowledge_statements"][0]["statement_id"])
        self.assertRegex(item["statement_id"], r"^ks_[0-9a-f]{64}$")
        raw = json.dumps([first["source"]["sha256"], 0, len(text), text], ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(item["statement_id"], "ks_" + hashlib.sha256(raw.encode("utf-8")).hexdigest())
        self.assertNotEqual(item["statement_id"], statement_id("0" * 64, item["processed_range"], text))

    def test_result_schema_rejects_missing_extra_and_wrong_types(self):
        result, _ = self.process("設備更新。")
        validator = Draft202012Validator(schema("step1_result.schema.json"))
        validator.validate(result)
        for change in ("missing", "extra", "bad_id", "bad_trace", "table_context"):
            broken = deepcopy(result)
            if change == "missing": del broken["step0_provenance"]
            elif change == "extra": broken["knowledge_items"] = []
            elif change == "bad_id": broken["knowledge_statements"][0]["statement_id"] = "random"
            elif change == "bad_trace": broken["knowledge_statements"][0]["step0_trace"]["warning_indexes"] = [-1]
            else: broken["knowledge_statements"][0]["kind"] = "table"
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validator.validate(broken)

    def test_integrity_validation_detects_semantic_metadata_tampering(self):
        text = self.table()
        snapshot = self.input(text)
        result, _ = self.process(text, self.table_items(text, snapshot), snapshot=snapshot)
        for change in ("text", "mapping", "trace", "header", "structure", "count", "coverage", "id", "warning"):
            broken = deepcopy(result)
            item = broken["knowledge_statements"][0]
            if change == "text": item["text"] += "改寫"
            elif change == "mapping": item["original_mapping"]["text"] = "改寫"
            elif change == "trace": item["step0_trace"]["unresolved_indexes"] = [999]
            elif change == "header": item["table_context"]["header"]["cells"][0]["text"] = "猜測"
            elif change == "structure": broken["structure"]["tables"][0]["rows"][0]["cells"][0]["text"] = "猜測"
            elif change == "count": broken["status"]["statement_count"] += 1
            elif change == "coverage": broken["knowledge_statements"].pop()
            elif change == "id": item["statement_id"] = "ks_" + "0" * 64
            else: broken["warnings"] = [{"code": "fake", "message": "fake"}]
            with self.subTest(change=change), self.assertRaises((Step1Error, ValidationError)):
                validate_result(broken, snapshot)

    def test_llm_schema_rejects_rewritten_document_or_invalid_shape(self):
        for data in ({}, {"statements": [], "unhandled": [], "rewritten_text": "改寫"}, {"statements": [{}], "unhandled": []}):
            with self.subTest(data=data), self.assertRaises(Step1Error):
                self.process("設備更新。", fake=FakeClient(data))

    def test_invalid_input_hash_offsets_and_new_schema_rejected(self):
        snapshot = self.input()
        for field in ("hash", "text", "old_name", "count"):
            data = snapshot.data
            if field == "hash": data["source"]["sha256"] = "0" * 64
            elif field == "text": data["processed_text"] += "未授權修改"
            elif field == "old_name": data["source"]["type"] = data["source"].pop("document_type")
            else: data["status"]["warning_count"] = 999
            path = self.root / "invalid.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises((Step1Error, ResultIntegrityError, ValidationError)):
                load_step0(path)

    def test_only_json_input_and_strict_json_parsing(self):
        examples = [("x.md", b"text"), ("x.txt", b"text"), ("x.json", b'{"x":1,"x":2}'),
                    ("x.json", b'{"x":NaN}'), ("x.json", b'not json'), ("x.json", b'\xff')]
        for name, raw in examples:
            path = self.root / name
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                load_step0(path)

    def test_empty_whitespace_and_heading_only_skip_llm(self):
        for text in ("", " \t\r\n", "# 文件\n\n## 章節\n"):
            with self.subTest(text=text):
                result, fake = self.process(text, [])
                self.assertEqual(fake.calls, [])
                self.assertEqual(result["status"]["llm_call_count"], 0)

    def test_txt_source_does_not_interpret_markdown(self):
        text = "# 這是原文。\n| 這是文字。"
        snapshot = self.input(text, source_name="source.txt")
        result, _ = self.process(text, snapshot=snapshot)
        self.assertEqual(result["structure"], {"headings": [], "tables": [], "protected_regions": []})

    def test_prompt_contains_only_step0_processed_text_as_source(self):
        snapshot = self.replaced_input()
        text = snapshot.data["processed_text"]
        _, fake = self.process(text, snapshot=snapshot, timeout=31, max_retries=2)
        payload = json.loads(fake.calls[0][0].split("\n\nSTEP1_DATA_JSON\n")[1])
        self.assertEqual(payload["processed_text"], text)
        self.assertNotIn("original_text", payload)
        self.assertEqual(fake.calls[0][2], {"timeout": 31, "max_retries": 2})
        self.assertEqual(payload["line_ranges"][-1]["end"], len(text))

    def test_table_prompt_excludes_nested_original_text_mappings(self):
        text = self.table()
        snapshot = self.input(text)
        _, fake = self.process(text, self.table_items(text, snapshot), snapshot=snapshot)
        payload = json.loads(fake.calls[0][0].split("\n\nSTEP1_DATA_JSON\n")[1])
        def check(value):
            if isinstance(value, dict):
                self.assertNotIn("original_mapping", value)
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(payload)

    def test_header_unresolved_is_associated_with_each_table_statement(self):
        text = self.table()
        data = self.input(text).data
        start = text.index("條件")
        span = {"start": start, "end": start + 2}
        data["unresolved"] = [{"original_text": "條件", "source_range": span, "reported_text": "條件", "reported_range": span,
                               "proposed_replacement": None, "reason_code": "ambiguous_reference", "reason": "離線表頭追溯標註。", "origin": "llm", "candidate_index": None}]
        data["status"].update(execution_state="completed_with_warnings", unresolved_count=1)
        snapshot = self.input(text, step0_data=data)
        result, _ = self.process(text, self.table_items(text, snapshot), snapshot=snapshot)
        self.assertTrue(all(s["step0_trace"]["unresolved_indexes"] == [0] for s in result["knowledge_statements"]))

    def test_save_rejects_invalid_result_shape_before_file_access(self):
        with self.assertRaises(ValidationError):
            save_result({}, self.root / "invalid.json")
        self.assertFalse((self.root / "invalid.json").exists())

    def test_runtime_does_not_read_source_even_when_deleted(self):
        snapshot = self.replaced_input()
        Path(snapshot.data["source"]["path"]).unlink()
        text = snapshot.data["processed_text"]
        result, _ = self.process(text, snapshot=snapshot)
        self.assertEqual(result["knowledge_statements"][0]["original_mapping"]["text"], snapshot.data["original_text"])

    def test_retry_metadata_records_client_attempts(self):
        text = "設備更新。"
        result, fake = self.process(text, fake=FakeClient({"statements": [decision(text)], "unhandled": []}, attempts=2), max_retries=1)
        self.assertEqual(result["status"]["llm_call_count"], 2)
        self.assertEqual(result["status"]["llm_retry_count"], 1)
        self.assertEqual(len(fake.calls), 1)

    def test_llm_failure_is_sanitized_and_does_not_publish(self):
        snapshot = self.input()
        out = self.root / "failure"
        fake = FakeClient(error=GenerationError(ErrorKind.TIMEOUT, "SECRET_DIAGNOSTICS"))
        with self.assertRaisesRegex(Step1Error, "timeout") as caught:
            run_step1(snapshot.path, out, client=fake)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(list(out.glob("*.json")), [])

    def test_invalid_options_do_not_call_client(self):
        snapshot = self.input()
        fake = FakeClient()
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": True}, {"max_retries": -1}, {"max_retries": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Step1Splitter(fake).process(snapshot, **options)
        self.assertEqual(fake.calls, [])

    def test_cli_success_and_protected_input_files_unchanged(self):
        text = "設備更新。"
        snapshot = self.input(text)
        source = Path(snapshot.data["source"]["path"])
        before = source.read_bytes()
        out = self.root / "nested" / "out"
        fake = FakeClient({"statements": [decision(text)], "unhandled": []})
        with redirect_stdout(io.StringIO()) as stdout:
            code = main(["--input", snapshot.path, "--output-dir", str(out), "--timeout", "30", "--max-retries", "0"], client=fake)
        self.assertEqual(code, 0)
        path = out / "fixture.step1.json"
        result = json.loads(path.read_bytes().decode("utf-8"))
        self.assertEqual(json.loads(stdout.getvalue())["output_path"], str(path))
        self.assertEqual(fake.calls[0][2], {"timeout": 30.0, "max_retries": 0})
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(Path(snapshot.path).read_bytes(), snapshot.raw)
        self.assertEqual(result["input"]["sha256"], hashlib.sha256(snapshot.raw).hexdigest())

    def test_existing_output_rejected_before_llm(self):
        text = "設備更新。"
        snapshot = self.input(text)
        out = self.root / "history"
        fake = FakeClient({"statements": [decision(text)], "unhandled": []})
        _, path = run_step1(snapshot.path, out, client=fake)
        raw = path.read_bytes()
        second = FakeClient()
        with self.assertRaises(FileExistsError):
            run_step1(snapshot.path, out, client=second)
        self.assertEqual(second.calls, [])
        self.assertEqual(path.read_bytes(), raw)

    def test_atomic_race_and_io_failure_do_not_overwrite_or_leave_temps(self):
        result, _ = self.process("設備更新。")
        path = self.root / "race.json"
        def race(temporary, destination):
            Path(destination).write_bytes(b"HISTORY")
            raise FileExistsError("racing writer")
        with patch.object(pipeline.os, "link", side_effect=race), self.assertRaises(FileExistsError):
            save_result(result, path)
        self.assertEqual(path.read_bytes(), b"HISTORY")
        self.assertEqual(list(self.root.glob(".step1-*.tmp")), [])
        with patch.object(pipeline.os, "link", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
            save_result(result, self.root / "denied.json")
        self.assertEqual(list(self.root.glob(".step1-*.tmp")), [])

    def test_output_preflight_permission_error_occurs_before_llm(self):
        snapshot = self.input()
        fake = FakeClient()
        with patch.object(pipeline.tempfile, "TemporaryFile", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
            run_step1(snapshot.path, self.root / "out", client=fake)
        self.assertEqual(fake.calls, [])

    def test_save_rejects_source_input_and_input_mutation(self):
        result, _ = self.process("設備更新。")
        for path in (result["source"]["path"], result["input"]["step0_json_path"]):
            with self.subTest(path=path), self.assertRaises(Step1Error):
                save_result(result, path)
        # Even formatting changes alter the exact input-file SHA-256.
        path = Path(result["input"]["step0_json_path"])
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(Step1Error, "快照"):
            save_result(result, self.root / "changed-input.json")

    def test_cli_invalid_inputs_blocked_output_and_llm_failure(self):
        snapshot = self.input()
        blocked = self.root / "blocked"
        blocked.write_text("file", encoding="utf-8")
        for input_path, out in ((self.root / "missing.json", self.root / "out"), (Path(snapshot.data["source"]["path"]), self.root / "out"), (Path(snapshot.path), blocked)):
            fake = FakeClient()
            with self.subTest(path=input_path), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--input", str(input_path), "--output-dir", str(out)], client=fake), 2)
            self.assertEqual(fake.calls, [])
        fake = FakeClient(error=GenerationError(ErrorKind.TIMEOUT, "secret"))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--input", snapshot.path, "--output-dir", str(self.root / "failed")], client=fake), 1)

    def test_cli_argument_errors(self):
        for option, value in (("--timeout", "0"), ("--timeout", "nan"), ("--timeout", "inf"), ("--max-retries", "-1"), ("--max-retries", "1.5")):
            with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["--input", "x.json", "--output-dir", "out", option, value], client=FakeClient())
            self.assertEqual(caught.exception.code, 2)

    def test_latest_real_step0_sample_offline_reference_26_plus_3(self):
        # This is an explicit offline annotation for this sample, not a general
        # splitter, not an LLM eval, and never used by the production pipeline.
        path = ROOT / "outputs/step0/20261029-134323/sample_technical_incident.step0.json"
        source_path = ROOT / "source/sample_technical_incident.md"
        input_before, source_before = path.read_bytes(), source_path.read_bytes()
        snapshot = load_step0(path)
        data = snapshot.data
        text = data["processed_text"]
        structure = analyze_structure(data)
        table = structure["tables"][0]
        items = []
        effect_heading = structure["headings"][5]
        effect_lines = [s for s in line_ranges(text) if effect_heading["processed_range"]["end"] <= s["start"] < structure["headings"][6]["processed_range"]["start"] and pipeline._slice(text, s).startswith("- ")]
        chain_start, chain_end = effect_lines[0]["start"], effect_lines[2]["end"]
        protected = [r["processed_range"] for r in structure["protected_regions"]]
        for span in line_ranges(text):
            if not pipeline._slice(text, span).strip() or any(pipeline.overlaps(span, p) for p in protected) or pipeline.overlaps(span, table["processed_range"]):
                continue
            if chain_start <= span["start"] < chain_end:
                if span["start"] == chain_start:
                    items.append(decision(text, chain_start, chain_end, kind="list", reason="causal_chain"))
                continue
            line = pipeline._slice(text, span)
            if line.startswith("- "):
                items.append(decision(text, **span, kind="list"))
            else:
                for match in re.finditer(r"[^。]+。", line):
                    items.append(decision(text, span["start"] + match.start(), span["start"] + match.end(), reason="independent_sentence"))
        items.extend(self.table_items(text, snapshot))
        result, _ = self.process(text, items, snapshot=snapshot)
        self.assertEqual(sum(s["kind"] != "table" for s in result["knowledge_statements"]), 26)
        self.assertEqual(sum(s["kind"] == "table" for s in result["knowledge_statements"]), 3)
        chains = [s for s in result["knowledge_statements"] if s["boundary_reason"] == "causal_chain"]
        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0]["text"], text[chain_start:chain_end])
        self.assertTrue(any(s["step0_trace"]["warning_indexes"] for s in result["knowledge_statements"]))
        self.assertTrue(any(s["step0_trace"]["replacement_indexes"] for s in result["knowledge_statements"] if s["kind"] == "table"))
        self.assertEqual(result["step0_provenance"]["status"]["execution_state"], "completed_with_warnings")
        self.assertEqual(path.read_bytes(), input_before)
        self.assertEqual(source_path.read_bytes(), source_before)


if __name__ == "__main__":
    unittest.main()
