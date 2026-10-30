"""Offline annotations exercise contracts; they do not measure LLM semantics."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from scripts.run_step2 import main
from scripts import run_pipeline
from src.ingestion import load_source
from src.llm import AttemptRecord, ErrorKind, GenerationError, GenerationResult
from src.pipeline.step0_preprocess import Step0Preprocessor, run_step0
from src.pipeline.step1_split import Step1Splitter, load_step0, run_step1
from src.pipeline import step2_extract as pipeline
from src.pipeline.step2_extract import (
    Step1Input, Step2Error, Step2EvidenceError, Step2Extractor, Step2LLMError, load_step1,
    run_step2, save_result, validate_result,
)
from tests.test_step0 import candidate as replacement
from tests.test_step1 import FakeClient, decision


class WireValidatingClient(FakeClient):
    """Like CodexClient, validate only against the schema passed to the client."""

    def generate_json(self, prompt, schema, **options):
        Draft202012Validator(schema).validate(self.data)
        return super().generate_json(prompt, schema, **options)


def annotations(snapshot, labels):
    """Explicit fixture grouping by labels; never used in production."""
    data = snapshot.data
    reviews, groups = [], {}
    for statement, surfaces in zip(data["knowledge_statements"], labels):
        candidates = []
        for entry in surfaces:
            surface, quote = entry if isinstance(entry, tuple) else (entry, None)
            cid = f"c{sum(len(r['candidates']) for r in reviews) + len(candidates)}"
            candidates.append({"candidate_id": cid, "surface_text": surface, "evidence_quote": quote,
                               "unit_kind": "concept", "resolution": "confirmed", "reason": "離線標註：完整核心技術概念。"})
            group = groups.setdefault(surface, {"candidate_ids": [], "canonical_label": surface,
                "disambiguation": "條文中此技術概念的明確身分。", "reason": "離線標註：本文確認指向同一概念。", "identity_evidence": []})
            group["candidate_ids"].append(cid)
            if not any(e["statement_id"] == statement["statement_id"] for e in group["identity_evidence"]):
                evidence = statement["text"]
                if statement["kind"] == "table":
                    evidence = next(cell["text"] for row in statement["table_context"]["rows"]
                                    for cell in row["cells"] if surface in cell["text"])
                group["identity_evidence"].append({"statement_id": statement["statement_id"], "text": evidence,
                                                   "evidence_quote": None})
        reviews.append({"statement_id": statement["statement_id"], "decision": "kept", "basis": "knowledge_bearing",
                        "reason": "含有明確技術概念。", "candidates": candidates})
    return {"statement_reviews": reviews, "concept_groups": list(groups.values())}


class Step2Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="step2-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch("src.llm.codex_client.subprocess.Popen", side_effect=AssertionError("LIVE LLM FORBIDDEN")))
        self.enterContext(patch("src.llm.codex_client.subprocess.run", side_effect=AssertionError("LIVE PROCESS FORBIDDEN")))

    def input(self, text="APC 控制器接收感測器資料。", *, spans=None, replacements=None):
        source = self.root / "source.md"
        source.write_bytes(text.encode("utf-8"))
        result0 = Step0Preprocessor(FakeClient({"candidates": replacements or [], "unresolved": []})).process(load_source(source))
        path0 = self.root / "fixture.step0.json"
        path0.write_text(json.dumps(result0, ensure_ascii=False), encoding="utf-8")
        text1 = result0["processed_text"]
        spans = spans(text1) if callable(spans) else spans
        result1 = Step1Splitter(FakeClient({"statements": spans if spans is not None else [decision(text1)], "unhandled": []})).process(load_step0(path0))
        path1 = self.root / "fixture.step1.json"
        path1.write_text(json.dumps(result1, ensure_ascii=False), encoding="utf-8")
        return load_step1(path1)

    def process(self, snapshot=None, plan=None, labels=None, **options):
        snapshot = snapshot or self.input()
        plan = plan if plan is not None else annotations(snapshot, labels or [["APC 控制器", "感測器資料"]])
        fake = FakeClient(plan)
        result = Step2Extractor(fake).process(snapshot, **options)
        validate_result(result, snapshot)
        return result, fake

    def test_multiple_atomic_concepts_and_complete_snapshot(self):
        text = "APC 控制器每 100 ms 接收一次感測器資料，並依照 PID 控制邏輯更新閥門開度。"
        snapshot = self.input(text)
        before = Path(snapshot.path).read_bytes()
        result, fake = self.process(snapshot, labels=[["APC 控制器", "感測器資料", "PID 控制邏輯", "閥門開度"]], timeout=420, max_retries=2)
        self.assertEqual(result["status"]["knowledge_item_count"], 4)
        self.assertEqual(result["step1_snapshot"], snapshot.data)
        self.assertEqual(result["input"]["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(Path(snapshot.path).read_bytes(), before)
        self.assertEqual(fake.calls[0][2], {"timeout": 420, "max_retries": 2})
        self.assertIn("不可信資料", fake.calls[0][0])
        self.assertIn("table_context", Step2Extractor(fake).prompt)
        self.assertIn("不拆成", Step2Extractor(fake).prompt)

    def test_compound_technical_term_is_not_split(self):
        result, _ = self.process(self.input("控制迴圈延遲增加。"), labels=[["控制迴圈延遲"]])
        self.assertEqual([i["canonical_label"] for i in result["knowledge_items"]], ["控制迴圈延遲"])

    def test_wire_schema_removes_only_target_keyword_and_is_deep_copy(self):
        snapshot = self.input()
        path = pipeline.ROOT / "schemas/step2_llm.schema.json"
        before = path.read_bytes()
        original = json.loads(before)
        expected = deepcopy(original)
        del expected["properties"]["concept_groups"]["items"]["properties"]["candidate_ids"]["uniqueItems"]
        fake = WireValidatingClient(annotations(snapshot, [["APC 控制器"]]))
        extractor = Step2Extractor(fake)
        with patch.object(fake, "generate_json", wraps=fake.generate_json) as generate:
            extractor.process(snapshot)
        request = generate.call_args.args[1]
        self.assertEqual(request, expected)
        self.assertIsNot(request, extractor.llm_schema)
        self.assertIsNot(request["$defs"], extractor.llm_schema["$defs"])
        request["$defs"]["candidate"]["properties"]["reason"]["minLength"] = 99
        self.assertEqual(extractor.llm_schema, original)
        self.assertEqual(path.read_bytes(), before)

    def test_wire_accepts_duplicate_ids_but_full_local_schema_rejects(self):
        snapshot = self.input()
        plan = annotations(snapshot, [["APC 控制器"]])
        plan["concept_groups"][0]["candidate_ids"] *= 2
        fake = WireValidatingClient(plan)
        with self.assertRaisesRegex(Step2Error, "Schema"):
            run_step2(snapshot.path, self.root / "duplicate", client=fake)
        self.assertEqual(len(fake.calls), 1)  # Client-side wire validation passed.
        self.assertEqual(list((self.root / "duplicate").glob("*.json")), [])

    def test_wire_accepts_cross_group_reuse_but_python_rejects_conflict(self):
        snapshot = self.input()
        plan = annotations(snapshot, [["APC 控制器", "感測器資料"]])
        plan["concept_groups"][1]["candidate_ids"] = plan["concept_groups"][0]["candidate_ids"][:]
        fake = WireValidatingClient(plan)
        with self.assertRaisesRegex(Step2Error, "群組"):
            run_step2(snapshot.path, self.root / "conflict", client=fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(list((self.root / "conflict").glob("*.json")), [])

    def test_text_contract_locates_both_evidence_types_and_keeps_result_schema(self):
        text = "\ufeff😀APC 控制器\r\n使用 `sensor_data` 與 **閥門開度**。"
        snapshot = self.input(text)
        plan = annotations(snapshot, [["APC 控制器", "sensor_data", "閥門開度"]])
        before = deepcopy(plan)
        result, fake = self.process(snapshot, plan)
        self.assertEqual(fake.data, before)
        self.assertEqual(plan, before)
        for definition in ("candidate", "evidence"):
            model_definition = fake.calls[0][1]["$defs"][definition]
            self.assertNotIn("start", model_definition["properties"])
            self.assertNotIn("end", model_definition["properties"])
        self.assertIn("step2-text-evidence-v3", fake.calls[0][0])
        for occurrence in result["occurrences"]:
            start = text.index(occurrence["surface_text"])
            self.assertEqual(occurrence["statement_local_range"], {"start": start, "end": start + len(occurrence["surface_text"])})
            self.assertEqual(occurrence["processed_range"], occurrence["statement_local_range"])
        self.assertEqual(result["occurrences"][0]["processed_range"]["start"], 2)
        stored = result["extraction_decisions"]
        for candidate in stored["statement_reviews"][0]["candidates"]:
            self.assertEqual(text[candidate["start"]:candidate["end"]], candidate["surface_text"])
        for group in stored["concept_groups"]:
            for evidence in group["identity_evidence"]:
                self.assertEqual(evidence["start"], 0)
                self.assertEqual(evidence["end"], len(text))
        restored = deepcopy(stored)
        for review in restored["statement_reviews"]:
            for candidate in review["candidates"]:
                del candidate["start"], candidate["end"]
        for group in restored["concept_groups"]:
            for evidence in group["identity_evidence"]:
                del evidence["start"], evidence["end"]
        self.assertEqual(restored, before)  # No model decisions or evidence lost.
        groups = {g["canonical_label"]: g for g in stored["concept_groups"]}
        for item in result["knowledge_items"]:
            self.assertEqual(item["identity_evidence"], groups[item["canonical_label"]]["identity_evidence"])
        Draft202012Validator(pipeline.schema("step2_result.schema.json")).validate(result)

    def test_zero_and_approximate_matches_rejected_without_publication(self):
        cases = (("CPU 使用率升高。", "CPU使用率"), ("PID 控制器運作。", "pid"),
                 ("Cafe\u0301 控制器運作。", "Caf\u00e9"), ("感測器 資料更新。", "感測器資料"),
                 ("控制器\r\n資料更新。", "控制器\n資料"))
        for text, surface in cases:
            snapshot = self.input(text)
            fake = WireValidatingClient(annotations(snapshot, [[surface]]))
            with self.subTest(surface=surface), self.assertRaises(Step2EvidenceError) as caught:
                run_step2(snapshot.path, self.root / "no-match", client=fake)
            self.assertEqual(caught.exception.code, "evidence_not_found")
            self.assertEqual(list((self.root / "no-match").glob("*.json")), [])

    def test_overlapping_and_repeated_matches_are_rejected(self):
        for text, surface in (("aaaa。", "aa"), ("控制器連接控制器。", "控制器")):
            snapshot = self.input(text)
            with self.subTest(text=text), self.assertRaises(Step2EvidenceError) as caught:
                self.process(snapshot, labels=[[surface]])
            self.assertEqual(caught.exception.code, "evidence_ambiguous")

    def test_candidate_cannot_borrow_text_from_another_statement(self):
        snapshot = self.two_statements("設備接收資料。CPU 使用率上升。")
        plan = annotations(snapshot, [["CPU 使用率"], ["CPU 使用率"]])
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, plan)
        self.assertEqual(caught.exception.code, "evidence_not_found")

    def test_identity_evidence_requires_unique_exact_match(self):
        snapshot = self.input("APC 控制器接收資料。資料會更新。")
        for fragment, code in (("資料", "evidence_ambiguous"), ("不存在證據", "evidence_not_found")):
            plan = annotations(snapshot, [["APC 控制器"]])
            plan["concept_groups"][0]["identity_evidence"][0]["text"] = fragment
            with self.subTest(code=code), self.assertRaises(Step2EvidenceError) as caught:
                self.process(snapshot, plan)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.role, "identity_evidence")

    def test_model_supplied_offsets_are_rejected_in_both_contracts(self):
        snapshot = self.input()
        for role in ("candidate", "identity_evidence"):
            plan = annotations(snapshot, [["APC 控制器"]])
            target = plan["statement_reviews"][0]["candidates"][0] if role == "candidate" else plan["concept_groups"][0]["identity_evidence"][0]
            target.update(start=0, end=7)
            with self.subTest(role=role), self.assertRaises(Step2EvidenceError) as caught:
                self.process(snapshot, plan)
            self.assertEqual(caught.exception.code, "llm_schema_invalid")

    def test_empty_and_whitespace_fragments_still_rejected(self):
        snapshot = self.input()
        for role in ("candidate", "identity_evidence"):
            for fragment in ("", " \r\n"):
                plan = annotations(snapshot, [["APC 控制器"]])
                if role == "candidate": plan["statement_reviews"][0]["candidates"][0]["surface_text"] = fragment
                else: plan["concept_groups"][0]["identity_evidence"][0]["text"] = fragment
                with self.subTest(role=role, fragment=fragment), self.assertRaises(Step2EvidenceError) as caught:
                    self.process(snapshot, plan)
                self.assertEqual(caught.exception.code, "llm_schema_invalid")

    def test_stored_python_ranges_are_recomputed_on_validation(self):
        result, _ = self.process()
        for role in ("candidate", "identity_evidence"):
            changed = deepcopy(result)
            decisions = changed["extraction_decisions"]
            target = decisions["statement_reviews"][0]["candidates"][0] if role == "candidate" else decisions["concept_groups"][0]["identity_evidence"][0]
            target["start"] += 1
            with self.subTest(role=role), self.assertRaises(Step2EvidenceError) as caught:
                validate_result(changed)
            self.assertEqual(caught.exception.code, "derived_range_mismatch")

    def test_pending_ambiguity_is_not_silently_accepted(self):
        snapshot = self.input("APC 控制器與控制器共同運作。")
        plan = annotations(snapshot, [["APC 控制器", "控制器"]])
        plan["statement_reviews"][0]["candidates"][1]["resolution"] = "pending"
        plan["concept_groups"].pop()
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, plan)
        self.assertEqual(caught.exception.code, "evidence_ambiguous")

    def test_evidence_failure_messages_do_not_echo_model_text_or_ids(self):
        snapshot = self.input()
        plan = annotations(snapshot, [["APC 控制器"]])
        plan["statement_reviews"][0]["candidates"][0].update(surface_text="SECRET_SURFACE", candidate_id="SECRET_CANDIDATE")
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, plan)
        self.assertEqual(caught.exception.code, "evidence_not_found")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn(snapshot.data["processed_text"], str(caught.exception))

    def test_table_identity_evidence_must_be_inside_one_cell(self):
        text = "| 觸發條件 | 動作 |\n| --- | --- |\n| CPU 使用率高於 90% | 重啟控制器 |\n"
        snapshot = self.input(text, spans=[decision(text, text.index("| CPU"), kind="table", reason="table_row")])
        plan = annotations(snapshot, [["CPU 使用率"]])
        plan["concept_groups"][0]["identity_evidence"][0]["text"] = snapshot.data["knowledge_statements"][0]["text"]
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, plan)
        self.assertEqual(caught.exception.code, "table_cell_mismatch")
        self.assertEqual(caught.exception.role, "identity_evidence")

    def test_table_repeated_cell_values_are_ambiguous(self):
        text = "| 一 | 二 |\n| --- | --- |\n| CPU | CPU |\n"
        snapshot = self.input(text, spans=[decision(text, text.index("| CPU"), kind="table", reason="table_row")])
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, labels=[["CPU"]])
        self.assertEqual(caught.exception.code, "evidence_ambiguous")

    def test_conditions_actions_and_changes_remain_in_statement(self):
        text = "CPU 使用率高於 90% 持續 30 秒，控制迴圈延遲增加，造成產線流量波動。"
        result, _ = self.process(self.input(text), labels=[["CPU 使用率", "控制迴圈延遲", "產線流量"]])
        self.assertEqual({i["canonical_label"] for i in result["knowledge_items"]}, {"CPU 使用率", "控制迴圈延遲", "產線流量"})
        self.assertEqual(result["step1_snapshot"]["knowledge_statements"][0]["text"], text)

    def test_confirmed_nonconcept_kinds_rejected(self):
        for text, kind in [("每 100 ms 接收一次", "behavior"), ("CPU 使用率高於 90% 持續 30 秒", "condition"),
                           ("控制迴圈延遲增加", "state_change"), ("造成產線流量波動", "causal_statement")]:
            with self.subTest(kind=kind):
                snapshot = self.input(text)
                plan = annotations(snapshot, [[text]])
                plan["statement_reviews"][0]["candidates"][0]["unit_kind"] = kind
                with self.assertRaises(Step2Error):
                    self.process(snapshot, plan)

    def two_statements(self, text="控制器接收資料。控制器更新輸出。"):
        return self.input(text, spans=lambda t: [decision(t, 0, t.index("。") + 1), decision(t, t.index("。") + 1)])

    def test_same_concept_across_statements_and_repeated_text_is_ambiguous(self):
        snapshot = self.two_statements()
        result, _ = self.process(snapshot, labels=[["控制器"], ["控制器"]])
        self.assertEqual(len(result["knowledge_items"]), 1)
        self.assertEqual(len(result["occurrences"]), 2)
        self.assertEqual(len({o["knowledge_item_id"] for o in result["occurrences"]}), 1)
        self.assertEqual(len({o["occurrence_id"] for o in result["occurrences"]}), 2)
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(self.input("控制器連接控制器。"), labels=[["控制器"]])
        self.assertEqual(caught.exception.code, "evidence_ambiguous")

    def test_same_name_different_concepts_are_distinct(self):
        snapshot = self.two_statements("銀行提供服務。河邊銀行是地名。")
        plan = annotations(snapshot, [["銀行"], ["銀行"]])
        group = plan["concept_groups"].pop()
        for index, meaning in enumerate(("金融機構", "本文河邊的地名")):
            plan["concept_groups"].append({**deepcopy(group), "candidate_ids": [group["candidate_ids"][index]],
                "disambiguation": meaning, "identity_evidence": [group["identity_evidence"][index]]})
        result, _ = self.process(snapshot, plan)
        self.assertEqual(len({i["knowledge_item_id"] for i in result["knowledge_items"]}), 2)

    def test_explicit_context_supported_synonym_merge(self):
        snapshot = self.two_statements("APC 控制器在本文簡稱主控。主控接收資料。")
        plan = annotations(snapshot, [["APC 控制器"], ["主控"]])
        first, second = plan["concept_groups"]
        first["candidate_ids"].extend(second["candidate_ids"])
        first["identity_evidence"].extend(second["identity_evidence"])
        first["reason"] = "第一條文明確宣告 APC 控制器簡稱主控。"
        plan["concept_groups"] = [first]
        result, _ = self.process(snapshot, plan)
        self.assertEqual(len(result["knowledge_items"]), 1)

    def test_uncertain_synonyms_are_pending_without_official_items(self):
        snapshot = self.two_statements("控制器接收資料。主控更新輸出。")
        plan = annotations(snapshot, [["控制器"], ["主控"]])
        plan["concept_groups"] = []
        for r in plan["statement_reviews"]:
            r.update(decision="needs_review", basis="uncertain", reason="無法確認兩個名稱是否同義。")
            r["candidates"][0].update(resolution="pending", reason="身分不明，禁止強制合併。")
        result, _ = self.process(snapshot, plan)
        self.assertEqual(result["knowledge_items"], [])
        self.assertEqual(result["occurrences"], [])
        self.assertTrue(all(r["pending_candidates"] and not r["knowledge_item_ids"] for r in result["statement_reviews"]))

    def test_filtered_and_needs_review_preserve_full_statement_and_source(self):
        snapshot = self.two_statements("謝謝閱讀。情況不明。")
        plan = annotations(snapshot, [[], []])
        for review, value, basis in zip(plan["statement_reviews"], ("filtered", "needs_review"), ("no_knowledge_value", "uncertain")):
            review.update(decision=value, basis=basis, reason="純客套無知識價值。" if value == "filtered" else "脈絡不足不可直接過濾。")
        result, _ = self.process(snapshot, plan)
        self.assertEqual(result["status"]["filtered_count"], 1)
        self.assertEqual(result["status"]["needs_review_count"], 1)
        self.assertEqual(result["step1_snapshot"], snapshot.data)
        self.assertEqual(result["knowledge_items"], [])

    def test_kept_can_include_pending_candidates(self):
        snapshot = self.input("控制器與主控接收資料。")
        plan = annotations(snapshot, [["控制器", "主控"]])
        plan["statement_reviews"][0]["candidates"][1].update(resolution="pending", reason="無法確認主控身分。")
        plan["concept_groups"].pop()
        result, _ = self.process(snapshot, plan)
        self.assertEqual(len(result["knowledge_items"]), 1)
        self.assertEqual(len(result["statement_reviews"][0]["pending_candidates"]), 1)

    def test_reviews_missing_duplicate_unknown_and_empty_kept_rejected(self):
        snapshot = self.input()
        base = annotations(snapshot, [["APC 控制器"]])
        plans = []
        for change in ("missing", "duplicate", "unknown", "empty", "wrong_basis", "filtered_confirmed"):
            plan = deepcopy(base)
            if change == "missing": plan["statement_reviews"] = []
            if change == "duplicate": plan["statement_reviews"] *= 2
            if change == "unknown": plan["statement_reviews"][0]["statement_id"] = "ks_unknown"
            if change == "empty": plan["statement_reviews"][0]["candidates"] = []
            if change == "wrong_basis": plan["statement_reviews"][0]["basis"] = "uncertain"
            if change == "filtered_confirmed": plan["statement_reviews"][0].update(decision="filtered", basis="no_knowledge_value")
            plans.append((change, plan))
        for name, plan in plans:
            with self.subTest(name=name), self.assertRaises(Step2Error):
                self.process(snapshot, plan)

    def test_table_headers_cells_unicode_and_exact_offsets(self):
        text = "# 監測\n\n| 觸發條件 | 動作 |\n| --- | --- |\n| 😀CPU 使用率高於 90% 持續 30 秒 | 重啟控制器 |\n"
        def spans(t):
            start = t.index("| 😀")
            return [decision(t, start, len(t), kind="table", reason="table_row")]
        snapshot = self.input(text, spans=spans)
        result, _ = self.process(snapshot, labels=[["CPU 使用率", "控制器"]])
        first = result["occurrences"][0]
        self.assertEqual(first["processed_range"]["start"], text.index("CPU"))
        self.assertEqual(first["statement_local_range"]["start"], 3)
        self.assertEqual(first["table_cell_ref"]["column_index"], 0)
        self.assertEqual(first["table_cell_ref"]["header_text"].strip(), "觸發條件")
        plan = annotations(snapshot, [["CPU 使用率"]])
        candidate = plan["statement_reviews"][0]["candidates"][0]
        candidate.update(surface_text="30 秒 | 重啟")
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, plan)
        self.assertEqual(caught.exception.code, "table_cell_mismatch")
        candidate.update(surface_text="觸發條件")
        with self.assertRaises(Step2Error):
            self.process(snapshot, plan)

    def test_heading_only_cannot_be_extracted(self):
        text = "# APC 控制器\n\n設備接收資料。"
        snapshot = self.input(text, spans=[decision(text, text.index("設備"))])
        plan = annotations(snapshot, [["設備"]])
        plan["statement_reviews"][0]["candidates"][0].update(surface_text="APC 控制器")
        with self.assertRaises(Step2Error):
            self.process(snapshot, plan)

    def test_step0_replacement_length_mapping_and_partial_boundary(self):
        text = "北區控制器負責調節。\n控制器接收資料。"
        snapshot = self.input(text, replacements=[replacement(text)],
                              spans=lambda t: [decision(t, 0, t.index("\n") + 1), decision(t, t.index("\n") + 1)])
        for surface, available in (("北區控制器", True), ("控制器", False)):
            plan = annotations(snapshot, [["北區控制器"], ["北區控制器"]])
            second = plan["statement_reviews"][1]["candidates"][0]
            if not available:
                second.update(surface_text=surface)
            result, _ = self.process(snapshot, plan)
            occurrence = result["occurrences"][1]
            self.assertEqual(occurrence["step0_trace"]["replacement_indexes"], [0])
            self.assertEqual(occurrence["original_mapping"]["status"], "available" if available else "unavailable")
            if available:
                self.assertEqual(occurrence["original_mapping"]["text"], "控制器")
            else:
                self.assertIsNone(occurrence["original_mapping"]["source_range"])
                self.assertTrue(occurrence["original_mapping"]["reason"])

    def test_hallucination_and_legacy_offsets_rejected(self):
        snapshot = self.input()
        for changes in ({"surface_text": "外部專有名詞"}, {"start": 1}, {"start": -1}, {"end": 9999},
                        {"start": True}, {"end": 0}, {"surface_text": "APC控制器"}):
            plan = annotations(snapshot, [["APC 控制器"]])
            plan["statement_reviews"][0]["candidates"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(Step2Error):
                self.process(snapshot, plan)

    def test_bad_json_and_schema_rejected(self):
        snapshot = self.input()
        for data in ("{not-json", None, [], {}, {"statement_reviews": [], "concept_groups": [], "extra": True}):
            fake = FakeClient()
            fake.data = data
            with self.subTest(data=data), self.assertRaises(Step2Error):
                Step2Extractor(fake).process(snapshot)
        for raw in (b'{"status":1,"status":2}', b'{"status":NaN}', b'{broken', b'\xff'):
            path = self.root / "bad.json"
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                load_step1(path)

    def test_llm_failure_does_not_publish(self):
        snapshot = self.input()
        for kind in (ErrorKind.TIMEOUT, ErrorKind.INVALID_JSON, ErrorKind.SCHEMA_VALIDATION, ErrorKind.CLI_EXECUTION):
            fake = FakeClient(error=GenerationError(kind, "SECRET_RAW_DATA"))
            with self.subTest(kind=kind), self.assertRaises(Step2LLMError) as caught:
                run_step2(snapshot.path, self.root / "failed", client=fake)
            self.assertNotIn("SECRET_RAW_DATA", str(caught.exception))
            self.assertEqual(list((self.root / "failed").glob("*.json")), [])

    def test_cli_execution_last_exit_code_reaches_cli_and_pipeline_safely(self):
        snapshot = self.input()
        secret = "SECRET_STDERR_API_KEY_ENV_PROMPT_DOCUMENT"
        error = GenerationError(ErrorKind.CLI_EXECUTION, secret)
        response = GenerationResult(False, None, error, (
            AttemptRecord(1, 0.01, -1, GenerationError(ErrorKind.TIMEOUT, secret)),
            AttemptRecord(2, 0.01, 17, error),
        ))
        fake = FakeClient()
        with patch.object(fake, "generate_json", return_value=response):
            with self.assertRaises(Step2LLMError) as caught:
                Step2Extractor(fake).process(snapshot)
            self.assertEqual(caught.exception.returncode, 17)
            self.assertIn("cli_execution", str(caught.exception))
            self.assertIn("returncode=17", str(caught.exception))
            with redirect_stderr(io.StringIO()) as stderr, redirect_stdout(io.StringIO()) as stdout:
                code = main(["--input", snapshot.path, "--output-dir", str(self.root / "failed-cli")], client=fake)
            self.assertEqual(code, 1)
            self.assertIn("returncode=17", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            cli_output = stderr.getvalue()
            # Step 0/1 are mocks here; only Step 2 processes the existing fixture.
            def failed_step2(src, out, **options):
                return run_step2(src, out, client=fake, **options)
            with patch.object(run_pipeline, "run_step0", return_value=({"status": {"execution_state": "completed"}}, self.root / "fixture.step0.json")), \
                 patch.object(run_pipeline, "run_step1", return_value=(snapshot.data, Path(snapshot.path))), \
                 patch.object(run_pipeline, "run_step2", side_effect=failed_step2), \
                 redirect_stderr(io.StringIO()) as stderr, redirect_stdout(io.StringIO()) as stdout:
                code = run_pipeline.main([str(self.root / "source.md"), "--until", "2", "--output-dir", str(self.root / "runs")])
            self.assertEqual(code, 1)
            self.assertIn("returncode=17", stderr.getvalue())
            for output in (str(caught.exception), cli_output, stderr.getvalue(), stdout.getvalue()):
                self.assertNotIn(secret, output)
                self.assertNotIn(snapshot.data["processed_text"], output)
            self.assertEqual(list(self.root.rglob("*.step2.json")), [])

    def test_cli_execution_missing_or_invalid_exit_code_stays_safe(self):
        snapshot = self.input()
        error = GenerationError(ErrorKind.CLI_EXECUTION, "SECRET_ERROR")
        for reported in (None, False, "SECRET_EXIT_CODE", -1, 3221225781):
            records = (AttemptRecord(1, 0.01, reported, error),)
            response = GenerationResult(False, None, error, records)
            fake = FakeClient()
            with self.subTest(reported=reported), patch.object(fake, "generate_json", return_value=response), self.assertRaises(Step2LLMError) as caught:
                Step2Extractor(fake).process(snapshot)
            expected = reported if type(reported) is int else None
            self.assertEqual(caught.exception.returncode, expected)
            self.assertIn(f"returncode={expected if expected is not None else 'unknown'}", str(caught.exception))
            self.assertNotIn("SECRET", str(caught.exception))
        fake = FakeClient()
        with patch.object(fake, "generate_json", return_value=GenerationResult(False, None, error, ())), self.assertRaises(Step2LLMError) as caught:
            Step2Extractor(fake).process(snapshot)
        self.assertIsNone(caught.exception.returncode)
        self.assertIn("returncode=unknown", str(caught.exception))

    def test_merge_conflicts_missing_members_unknown_refs_and_evidence(self):
        snapshot = self.two_statements()
        for kind in ("duplicate_group", "duplicate_member", "unknown_member", "missing_group", "missing_evidence", "bad_evidence", "unknown_evidence", "invented_label"):
            plan = annotations(snapshot, [["控制器"], ["控制器"]])
            group = plan["concept_groups"][0]
            if kind == "duplicate_group": plan["concept_groups"].append(deepcopy(group))
            if kind == "duplicate_member": group["candidate_ids"] *= 2
            if kind == "unknown_member": group["candidate_ids"].append("unknown")
            if kind == "missing_group": plan["concept_groups"] = []
            if kind == "missing_evidence": group["identity_evidence"].pop()
            if kind == "bad_evidence": group["identity_evidence"][0]["text"] = "幻覺證據"
            if kind == "unknown_evidence": group["identity_evidence"][0]["statement_id"] = "unknown"
            if kind == "invented_label": group["canonical_label"] = "外部完整名稱"
            with self.subTest(kind=kind), self.assertRaises(Step2Error):
                self.process(snapshot, plan)

    def test_pending_group_refs_and_duplicate_candidate_ids_rejected(self):
        snapshot = self.input()
        for kind in ("pending", "duplicate", "same_span", "overlap"):
            plan = annotations(snapshot, [["APC 控制器", "感測器資料"]])
            first, second = plan["statement_reviews"][0]["candidates"]
            if kind == "pending": second["resolution"] = "pending"
            if kind == "duplicate": second["candidate_id"] = first["candidate_id"]
            if kind == "same_span": second.update(surface_text=first["surface_text"])
            if kind == "overlap": second.update(surface_text="控制器")
            with self.subTest(kind=kind), self.assertRaises(Step2Error):
                self.process(snapshot, plan)

    def test_ids_stable_when_response_order_handles_and_explanation_change(self):
        snapshot = self.two_statements()
        plan = annotations(snapshot, [["控制器", "資料"], ["控制器", "輸出"]])
        first, _ = self.process(snapshot, plan)
        plan["statement_reviews"].reverse()
        plan["concept_groups"].reverse()
        for review in plan["statement_reviews"]:
            review["candidates"].reverse()
            for c in review["candidates"]: c["candidate_id"] = "new_" + c["candidate_id"]
        for group in plan["concept_groups"]:
            group["candidate_ids"] = ["new_" + c for c in reversed(group["candidate_ids"])]
            group["reason"] = "不同的文字說明，不影響概念來源錨點。"
        second, _ = self.process(snapshot, plan)
        self.assertEqual([i["knowledge_item_id"] for i in first["knowledge_items"]], [i["knowledge_item_id"] for i in second["knowledge_items"]])
        self.assertEqual(first["occurrences"], second["occurrences"])
        identity = first["knowledge_items"][0]["identity_input"]
        self.assertEqual(pipeline._hash_id("ki_", identity), first["knowledge_items"][0]["knowledge_item_id"])

    def test_tampered_output_links_mapping_snapshot_and_statistics_rejected(self):
        result, _ = self.process()
        for key in ("id", "mapping", "occurrence", "review", "source", "snapshot", "statistics", "warnings"):
            changed = deepcopy(result)
            if key == "id": changed["knowledge_items"][0]["knowledge_item_id"] = "ki_" + "0" * 64
            if key == "mapping": changed["occurrences"][0]["original_mapping"]["text"] = "bad"
            if key == "occurrence": changed["occurrences"][0]["processed_range"]["start"] += 1
            if key == "review": changed["statement_reviews"][0]["knowledge_item_ids"] = []
            if key == "source": changed["source"]["sha256"] = "0" * 64
            if key == "snapshot": changed["step1_snapshot"]["knowledge_statements"][0]["text"] = "bad"
            if key == "statistics": changed["status"]["kept_count"] += 1
            if key == "warnings": changed["warnings"].append({"code": "bad", "message": "bad"})
            with self.subTest(key=key), self.assertRaises((ValueError, ValidationError)):
                validate_result(changed)

    def test_upstream_corruption_rejected_before_call(self):
        snapshot = self.input()
        for key in ("text", "range", "trace", "status"):
            data = snapshot.data
            if key == "text": data["processed_text"] += "幻覺"
            if key == "range": data["knowledge_statements"][0]["processed_range"]["start"] += 1
            if key == "trace": data["knowledge_statements"][0]["step0_trace"]["replacement_indexes"] = [123]
            if key == "status": data["status"]["execution_state"] = "llm_failed"
            broken = Step1Input(snapshot.path, json.dumps(data).encode("utf-8"))
            fake = FakeClient()
            with self.subTest(key=key), self.assertRaises((ValueError, ValidationError)):
                Step2Extractor(fake).process(broken)
            self.assertEqual(fake.calls, [])

    def test_save_no_overwrite_and_preflight_blocks_llm(self):
        snapshot = self.input()
        fake = FakeClient(annotations(snapshot, [["APC 控制器"]]))
        result, path = run_step2(snapshot.path, self.root / "out", client=fake)
        before = path.read_bytes()
        fresh = FakeClient()
        with self.assertRaises(FileExistsError):
            run_step2(snapshot.path, path.parent, client=fresh)
        with self.assertRaises(FileExistsError): save_result(result, path)
        self.assertEqual(fresh.calls, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(path.name.endswith(".step2.json"))

    def test_atomic_racing_writer_and_temp_cleanup(self):
        result, _ = self.process()
        path = self.root / "race.step2.json"
        def race(_temporary, destination):
            Path(destination).write_bytes(b"HISTORY")
            raise FileExistsError("racing writer")
        with patch.object(pipeline.os, "link", side_effect=race), self.assertRaises(FileExistsError):
            save_result(result, path)
        self.assertEqual(path.read_bytes(), b"HISTORY")
        self.assertEqual(list(self.root.glob(".step2-*.tmp")), [])
        with patch.object(pipeline.os, "link", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
            save_result(result, self.root / "denied.step2.json")
        self.assertEqual(list(self.root.glob(".step2-*.tmp")), [])

    def test_source_input_and_step0_protected_and_snapshot_hash_rechecked(self):
        result, _ = self.process()
        snapshot = result["step1_snapshot"]
        for path in (result["input"]["step1_json_path"], result["source"]["path"], snapshot["input"]["step0_json_path"]):
            with self.subTest(path=path), self.assertRaises(Step2Error): save_result(result, path)
        path1 = Path(result["input"]["step1_json_path"])
        path1.write_bytes(path1.read_bytes() + b"\n")
        with self.assertRaisesRegex(Step2Error, "快照"):
            save_result(result, self.root / "changed.step2.json")

    def test_only_step1_json_is_read_at_runtime(self):
        snapshot = self.input()
        source = Path(snapshot.data["source"]["path"])
        path0 = Path(snapshot.data["input"]["step0_json_path"])
        source.unlink()
        path0.unlink()
        result, path = run_step2(snapshot.path, self.root / "out", client=FakeClient(annotations(snapshot, [["APC 控制器"]])))
        self.assertTrue(path.exists())
        self.assertEqual(result["step1_snapshot"], snapshot.data)

    def test_zero_statements_no_llm_and_warnings_preserved(self):
        snapshot = self.input("# 僅有標題\n", spans=[])
        result, fake = self.process(snapshot, {"statement_reviews": [], "concept_groups": []})
        self.assertEqual(fake.calls, [])
        self.assertEqual(result["status"]["llm_call_count"], 0)
        self.assertEqual(result["status"]["reviewed_statement_count"], 0)
        self.assertEqual(result["step1_snapshot"]["unhandled_regions"], snapshot.data["unhandled_regions"])

    def test_unresolved_step0_trace_preserved_for_pending_candidate(self):
        text = "北區控制器負責調節。\n控制器接收資料。"
        snapshot = self.input(text, replacements=[replacement(text, is_unique=False)],
                              spans=lambda t: [decision(t, 0, t.index("\n") + 1), decision(t, t.index("\n") + 1)])
        plan = annotations(snapshot, [["北區控制器"], ["控制器"]])
        plan["statement_reviews"][1].update(decision="needs_review", basis="uncertain")
        plan["statement_reviews"][1]["candidates"][0].update(resolution="pending", reason="未解析簡稱，不能確定身分。")
        plan["concept_groups"].pop()
        result, _ = self.process(snapshot, plan)
        pending = result["statement_reviews"][1]["pending_candidates"][0]
        self.assertEqual(pending["step0_trace"]["unresolved_indexes"], [0])
        self.assertEqual(result["step1_snapshot"]["step0_provenance"], snapshot.data["step0_provenance"])
        self.assertIn("upstream_issues_preserved", [w["code"] for w in result["warnings"]])

    def test_table_prompt_has_no_nested_original_mapping(self):
        text = "# 指標\r\n\r\n| 指標 | 條件 |\r\n| --- | --- |\r\n| CPU 使用率 | 高於 90% |\r\n"
        snapshot = self.input(text, spans=[decision(text, text.index("| CPU"), kind="table", reason="table_row")])
        result, fake = self.process(snapshot, labels=[["CPU 使用率"]])
        payload = json.loads(fake.calls[0][0].split("\n\nSTEP2_DATA_JSON\n")[1])
        def check(value):
            if isinstance(value, dict):
                self.assertNotIn("original_mapping", value)
                self.assertNotIn("original_text", value)
                for child in value.values(): check(child)
            elif isinstance(value, list):
                for child in value: check(child)
        check(payload)
        self.assertEqual(result["occurrences"][0]["processed_range"]["start"], text.index("CPU"))

    def test_retry_statistics_record_client_attempts(self):
        snapshot = self.input()
        fake = FakeClient(annotations(snapshot, [["APC 控制器"]]), attempts=2)
        result = Step2Extractor(fake).process(snapshot, max_retries=1)
        self.assertEqual(result["status"]["llm_call_count"], 2)
        self.assertEqual(result["status"]["llm_retry_count"], 1)
        self.assertEqual(len(fake.calls), 1)

    def test_cli_arguments_invalid_inputs_schema_and_generation_failure(self):
        snapshot = self.input()
        for args in (["--input", "x.md"], ["--input", snapshot.path, "--output-dir", str(self.root), "--timeout", "nan"],
                     ["--input", snapshot.path, "--output-dir", str(self.root), "--max-retries", "-1"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main(args, client=FakeClient())
        for path in (self.root / "missing.json", self.root / "source.md"):
            fake = FakeClient()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--input", str(path), "--output-dir", str(self.root / "out")], client=fake), 2)
            self.assertEqual(fake.calls, [])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--input", snapshot.path, "--output-dir", str(self.root / "out")], client=FakeClient(error=GenerationError(ErrorKind.TIMEOUT, "secret"))), 1)
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--input", snapshot.path, "--output-dir", str(self.root / "success")], client=FakeClient(annotations(snapshot, [["APC 控制器"]]))), 0)
        self.assertIn("knowledge_item_count", stdout.getvalue())

    def test_preflight_permission_error_and_invalid_parameters_before_llm(self):
        snapshot = self.input()
        fake = FakeClient()
        with patch.object(pipeline.tempfile, "TemporaryFile", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
            run_step2(snapshot.path, self.root / "out", client=fake)
        for options in ({"timeout": True}, {"timeout": 0}, {"timeout": float("inf")}, {"max_retries": -1}, {"max_retries": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Step2Extractor(fake).process(snapshot, **options)
        self.assertEqual(fake.calls, [])

    def test_real_python_chain_with_fake_clients(self):
        source = self.root / "chain.md"
        source.write_text("APC 控制器接收感測器資料。", encoding="utf-8")
        before = source.read_bytes()
        def step0(src, out, **kw):
            return run_step0(src, out, client=FakeClient({"candidates": [], "unresolved": []}), **kw)
        def step1(src, out, **kw):
            data = load_step0(src).data
            return run_step1(src, out, client=FakeClient({"statements": [decision(data["processed_text"])], "unhandled": []}), **kw)
        def step2(src, out, **kw):
            return run_step2(src, out, client=FakeClient(annotations(load_step1(src), [["APC 控制器", "感測器資料"]])), **kw)
        with patch.object(run_pipeline, "run_step0", side_effect=step0), patch.object(run_pipeline, "run_step1", side_effect=step1), patch.object(run_pipeline, "run_step2", side_effect=step2), redirect_stdout(io.StringIO()) as stdout:
            code = run_pipeline.main([str(source), "--output-dir", str(self.root / "runs"), "--until", "2"])
        self.assertEqual(code, 0)
        self.assertIn("去重後知識項數量：2", stdout.getvalue())
        paths = list((self.root / "runs").rglob("*.step2.json"))
        self.assertEqual(len(paths), 1)
        validate_result(json.loads(paths[0].read_text(encoding="utf-8")))
        self.assertEqual(source.read_bytes(), before)

    def test_v3_manual_golden_examples_a_c_d_and_excluded_units(self):
        # Human annotations exercise the contract. A Fake Client cannot prove LLM recall.
        golden = (
            ("PID 控制邏輯會根據目標值與量測值的誤差計算閥門開度。",
             ["PID 控制邏輯", "目標值", "量測值", "閥門開度"], ["誤差計算閥門開度"]),
            ("資料品質為 stale 時，監控畫面應顯示黃色警示。",
             ["資料品質", "stale", "監控畫面"], ["資料品質為 stale 時", "顯示黃色警示"]),
            ("stale 資料是指感測器資料的時間戳落後目前控制週期超過允許門檻。",
             ["stale 資料", "感測器資料", "時間戳", "控制週期"], ["時間戳落後目前控制週期超過允許門檻"]),
        )
        for text, expected, excluded in golden:
            with self.subTest(text=text):
                snapshot = self.input(text)
                result, fake = self.process(snapshot, labels=[expected])
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual({i["canonical_label"] for i in result["knowledge_items"]}, set(expected))
                self.assertTrue(all(x not in {i["canonical_label"] for i in result["knowledge_items"]} for x in excluded))
                self.assertEqual(result["step1_snapshot"]["knowledge_statements"][0]["text"], text)
        # The generic threshold is not promoted merely because a definition mentions it.
        snapshot = self.input(golden[-1][0])
        plan = annotations(snapshot, [golden[-1][1] + ["允許門檻"]])
        plan["statement_reviews"][0]["candidates"][-1].update(resolution="pending", reason="參數身分未充分界定。")
        plan["concept_groups"].pop()
        result, _ = self.process(snapshot, plan)
        self.assertEqual(result["statement_reviews"][0]["pending_candidates"][0]["surface_text"], "允許門檻")

    def test_v3_manual_golden_causal_chain_explicit_repeated_occurrences(self):
        text = ("控制迴圈延遲增加會造成閥門開度更新變慢。"
                "閥門開度更新變慢會導致壓力調節時間拉長。"
                "壓力調節時間拉長可能造成產線流量波動。")
        labels = [["控制迴圈延遲",
                   ("閥門開度", "控制迴圈延遲增加會造成閥門開度更新變慢。"),
                   ("閥門開度", "閥門開度更新變慢會導致壓力調節時間拉長。"),
                   ("壓力調節時間", "閥門開度更新變慢會導致壓力調節時間拉長。"),
                   ("壓力調節時間", "壓力調節時間拉長可能造成產線流量波動。"),
                   "產線流量"]]
        result, fake = self.process(self.input(text), labels=labels)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(result["status"]["knowledge_item_count"], 4)
        self.assertEqual(result["status"]["occurrence_count"], 6)
        for label, count in (("閥門開度", 2), ("壓力調節時間", 2)):
            item = next(i for i in result["knowledge_items"] if i["canonical_label"] == label)
            self.assertEqual(len(item["occurrence_ids"]), count)
            self.assertEqual(len({o["occurrence_id"] for o in result["occurrences"]
                                  if o["knowledge_item_id"] == item["knowledge_item_id"]}), count)
        self.assertFalse({"閥門開度更新變慢", "壓力調節時間拉長", "產線流量波動"} &
                         {i["canonical_label"] for i in result["knowledge_items"]})

    def test_v3_quote_locates_each_explicit_occurrence_and_never_enumerates(self):
        text = "😀 **CPU 使用率** 更新。\r\n😀 **CPU 使用率** 警告。"
        first = "😀 **CPU 使用率** 更新。\r\n"
        second = "😀 **CPU 使用率** 警告。"
        snapshot = self.input(text)
        labels = [[("CPU 使用率", first), ("CPU 使用率", second)]]
        result, _ = self.process(snapshot, labels=labels)
        item = result["knowledge_items"][0]
        self.assertEqual(len(item["occurrence_ids"]), 2)
        self.assertEqual([o["statement_local_range"]["start"] for o in result["occurrences"]],
                         [text.index("CPU 使用率"), text.rindex("CPU 使用率")])
        self.assertEqual([o["evidence_quote"] for o in result["occurrences"]], [first, second])
        single, _ = self.process(snapshot, labels=[[labels[0][0]]])
        self.assertEqual(len(single["occurrences"]), 1)
        self.assertEqual(single["occurrences"][0]["statement_local_range"]["start"], text.index("CPU 使用率"))

    def test_v3_quote_rejects_zero_ambiguous_and_overlapping_matches(self):
        cases = (
            ("控制器更新。控制器停止。", "控制器", "不存在控制器", "quote_not_found"),
            ("控制器更新。控制器更新。", "控制器", "控制器更新", "quote_ambiguous"),
            ("控制器更新。控制器停止。", "控制器", "控制器更新。控制器停止。", "quote_surface_ambiguous"),
            ("aaaa", "aa", "aaaa", "quote_surface_ambiguous"),
            ("控制器更新。", "控制器", "更新。", "quote_surface_not_found"),
            ("CPU 使用率更新。", "CPU 使用率", "cpu 使用率更新。", "quote_not_found"),
            ("Cafe\u0301 更新。", "Cafe\u0301", "Caf\u00e9 更新。", "quote_not_found"),
            ("感測器 資料更新。", "感測器 資料", "感測器資料更新。", "quote_not_found"),
        )
        for text, surface, quote, code in cases:
            with self.subTest(text=text, quote=quote):
                snapshot = self.input(text)
                with self.assertRaises(Step2EvidenceError) as caught:
                    self.process(snapshot, labels=[[(surface, quote)]])
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(surface, str(caught.exception))
        snapshot = self.two_statements("控制器更新。控制器停止。")
        plan = annotations(snapshot, [["控制器"], ["控制器"]])
        plan["statement_reviews"][0]["candidates"][0]["evidence_quote"] = "控制器停止。"
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, plan)
        self.assertEqual(caught.exception.code, "quote_not_found")

    def test_v3_identity_evidence_uses_same_quote_locator(self):
        text = "控制器接收資料。控制器更新輸出。"
        snapshot = self.input(text)
        plan = annotations(snapshot, [[("控制器", "控制器接收資料。"),
                                       ("控制器", "控制器更新輸出。")]])
        evidence = plan["concept_groups"][0]["identity_evidence"][0]
        evidence.update(text="控制器", evidence_quote="控制器更新輸出。")
        result, _ = self.process(snapshot, plan)
        self.assertEqual(result["knowledge_items"][0]["identity_evidence"][0]["start"], text.rindex("控制器"))
        for quote, code in ((None, "evidence_ambiguous"), (text, "quote_surface_ambiguous"),
                            ("控制器不更新。", "quote_not_found")):
            bad = deepcopy(plan)
            bad["concept_groups"][0]["identity_evidence"][0]["evidence_quote"] = quote
            with self.subTest(quote=quote), self.assertRaises(Step2EvidenceError) as caught:
                self.process(snapshot, bad)
            self.assertEqual(caught.exception.role, "identity_evidence")
            self.assertEqual(caught.exception.code, code)
        tampered = deepcopy(result)
        tampered["extraction_decisions"]["concept_groups"][0]["identity_evidence"][0]["evidence_quote"] = "控制器接收資料。"
        with self.assertRaises(Step2Error):
            validate_result(tampered, snapshot)

    def test_v3_three_column_table_and_repeated_cell_quote(self):
        text = ("| 設備 | 觸發條件 | 控制動作 |\n| --- | --- | --- |\n"
                "| APC 控制器 | CPU 使用率高於 90% | PID 控制邏輯調整閥門開度 |\n")
        snapshot = self.input(text, spans=[decision(text, text.index("| APC"), kind="table", reason="table_row")])
        result, _ = self.process(snapshot, labels=[["APC 控制器", "CPU 使用率", "PID 控制邏輯", "閥門開度"]])
        self.assertEqual({o["table_cell_ref"]["column_index"] for o in result["occurrences"]}, {0, 1, 2})
        self.assertEqual(result["status"]["knowledge_item_count"], 4)
        text2 = "| 指標甲 | 指標乙 |\n| --- | --- |\n| CPU 使用率 | CPU 使用率 |\n"
        snapshot2 = self.input(text2, spans=[decision(text2, text2.index("| CPU"), kind="table", reason="table_row")])
        statement = snapshot2.data["knowledge_statements"][0]["text"]
        plan = annotations(snapshot2, [[("CPU 使用率", "| CPU 使用率 | CPU 使用率 |")]])
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot2, plan)
        self.assertEqual(caught.exception.code, "quote_surface_ambiguous")
        plan["statement_reviews"][0]["candidates"][0]["evidence_quote"] = "CPU 使用率 | C"
        plan["concept_groups"][0]["identity_evidence"][0].update(text="CPU 使用率",
                                                                     evidence_quote="CPU 使用率 | C")
        result2, _ = self.process(snapshot2, plan)
        self.assertEqual(result2["occurrences"][0]["table_cell_ref"]["column_index"], 0)
        self.assertIn("|", statement)

    def test_v3_document_intro_filters_by_content_not_noun_presence(self):
        text = ("此測試文件描述一個 APC 控制器在高負載狀態下產生控制延遲的案例。"
                "APC 控制器在高負載時，控制迴圈延遲增加。"
                "本文件首次記錄 APC 控制器每 100 ms 讀取感測器資料。"
                "某段記錄提及 APC 控制器，但是否新增規則尚不明確。")
        ends = [i + 1 for i, char in enumerate(text) if char == "。"]
        starts = [0] + ends[:-1]
        snapshot = self.input(text, spans=[decision(text, start, end) for start, end in zip(starts, ends)])
        plan = annotations(snapshot, [["APC 控制器"], ["APC 控制器", "控制迴圈延遲"],
                                      ["APC 控制器", "感測器資料"], ["APC 控制器"]])
        intro, technical, factual_intro, uncertain = plan["statement_reviews"]
        intro.update(decision="filtered", basis="no_knowledge_value",
                     reason=f"文件主題介紹，技術內容已由 {technical['statement_id']} 完整提供。")
        intro["candidates"][0].update(resolution="pending", reason="名詞存在，但本條文沒有新增知識。")
        uncertain.update(decision="needs_review", basis="uncertain", reason="尚無法確認是否有獨立新增的技術規則。")
        uncertain["candidates"][0].update(resolution="pending", reason="需確認條文價值。")
        plan["concept_groups"] = [g for g in plan["concept_groups"] if g["canonical_label"] != "APC 控制器"]
        confirmed_ids = [r["candidates"][0]["candidate_id"] for r in (technical, factual_intro)]
        # Rebuild the one confirmed APC group using only the two substantive clauses.
        plan["concept_groups"].append({"candidate_ids": confirmed_ids, "canonical_label": "APC 控制器",
            "disambiguation": "條文中的控制設備。", "reason": "兩條技術事實指向同一設備。",
            "identity_evidence": [{"statement_id": r["statement_id"], "text": snapshot.data["knowledge_statements"][n]["text"],
                                   "evidence_quote": None} for n, r in ((1, technical), (2, factual_intro))]})
        result, _ = self.process(snapshot, plan)
        self.assertEqual([r["decision"] for r in result["statement_reviews"]],
                         ["filtered", "kept", "kept", "needs_review"])
        self.assertEqual(result["status"]["filtered_count"], 1)
        self.assertEqual(result["status"]["needs_review_count"], 1)
        self.assertEqual(result["status"]["kept_count"], 2)
        self.assertEqual(len(result["statement_reviews"][0]["pending_candidates"]), 1)
        self.assertEqual(result["statement_reviews"][0]["knowledge_item_ids"], [])
        self.assertEqual(result["step1_snapshot"], snapshot.data)

    def test_v3_value_review_keeps_repeated_operational_rules(self):
        # Human decisions exercise the review contract; they do not predict model classification.
        intro = "此測試文件描述一個 APC 控制器在高負載狀態下產生控制延遲的案例。"
        rule = "操作員確認降級模式啟動後，應檢查資料佇列長度。"
        repeated_rule = "降級模式啟動後，操作員應檢查資料佇列長度。"
        text = intro + rule + repeated_rule
        snapshot = self.input(text, spans=[decision(text, 0, len(intro)),
                                           decision(text, len(intro), len(intro + rule)),
                                           decision(text, len(intro + rule))])
        plan = annotations(snapshot, [[], ["降級模式", "資料佇列長度"],
                                      ["降級模式", "資料佇列長度"]])
        plan["statement_reviews"][0].update(decision="filtered", basis="no_knowledge_value",
                                            reason="僅介紹文件案例，沒有可獨立使用的技術命題。")
        result, fake = self.process(snapshot, plan)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([r["decision"] for r in result["statement_reviews"]], ["filtered", "kept", "kept"])
        self.assertEqual([r["statement_id"] for r in result["statement_reviews"]],
                         [s["statement_id"] for s in snapshot.data["knowledge_statements"]])
        self.assertEqual((result["status"]["filtered_count"], result["status"]["kept_count"]), (1, 2))
        self.assertEqual(result["status"]["knowledge_item_count"], 2)
        self.assertEqual(result["status"]["occurrence_count"], 4)
        self.assertTrue(all(len(i["occurrence_ids"]) == 2 for i in result["knowledge_items"]))
        self.assertEqual(result["step1_snapshot"], snapshot.data)
        prompt = fake.calls[0][0]
        self.assertIn("知識價值判斷", prompt)
        self.assertIn("知識重複判斷", prompt)
        self.assertIn("不得因與其他條文重複而 filtered", prompt)

    def test_v3_value_review_keeps_table_expectation_similar_to_prose(self):
        prose = "感測器資料延遲超過 250 ms 時，資料品質標記為 stale。"
        header = "| 測試項目 | 觸發條件 | 預期結果 |\n| --- | --- | --- |\n"
        row = "| 資料逾時 | 感測器資料延遲超過 250 ms | 資料品質標記為 stale |\n"
        text = prose + "\n\n" + header + row
        row_start = text.index(row)
        snapshot = self.input(text, spans=[decision(text, 0, len(prose)),
                                           decision(text, row_start, kind="table", reason="table_row")])
        result, fake = self.process(snapshot, labels=[["感測器資料", "資料品質", "stale"],
                                                      ["資料逾時", "感測器資料", "資料品質", "stale"]])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([r["decision"] for r in result["statement_reviews"]], ["kept", "kept"])
        self.assertEqual(result["status"]["occurrence_count"], 7)
        self.assertEqual(result["status"]["knowledge_item_count"], 4)
        table_sid = snapshot.data["knowledge_statements"][1]["statement_id"]
        table_occurrences = [o for o in result["occurrences"] if o["statement_id"] == table_sid]
        self.assertEqual({o["table_cell_ref"]["column_index"] for o in table_occurrences}, {0, 1, 2})
        self.assertTrue(all(text[o["processed_range"]["start"]:o["processed_range"]["end"]] == o["surface_text"]
                            for o in table_occurrences))
        self.assertEqual(result["step1_snapshot"], snapshot.data)

    def test_v3_result_schema_accepts_legacy_v2_without_quotes(self):
        snapshot = self.input()
        result, _ = self.process(snapshot, labels=[["APC 控制器"]])
        old_shape = deepcopy(result)
        def remove_quotes(value):
            if isinstance(value, dict):
                value.pop("evidence_quote", None)
                for child in value.values(): remove_quotes(child)
            elif isinstance(value, list):
                for child in value: remove_quotes(child)
        remove_quotes(old_shape)
        validate_result(old_shape, snapshot)
        self.assertEqual(old_shape["status"], result["status"])

    def test_v3_wire_schema_is_strict_nullable_and_full_validation_remains(self):
        snapshot = self.input("控制器讀取資料。")
        plan = annotations(snapshot, [["控制器"]])
        fake = WireValidatingClient(plan)
        result = Step2Extractor(fake).process(snapshot)
        self.assertEqual(len(fake.calls), 1)
        wire = fake.calls[0][1]
        for name in ("candidate", "evidence"):
            node = wire["$defs"][name]
            self.assertEqual(set(node["required"]), set(node["properties"]))
            self.assertEqual(node["properties"]["evidence_quote"]["type"], ["string", "null"])
            self.assertNotIn("start", node["properties"])
            self.assertNotIn("end", node["properties"])
        self.assertNotIn("uniqueItems", wire["properties"]["concept_groups"]["items"]["properties"]["candidate_ids"])
        self.assertTrue(pipeline.schema("step2_llm.schema.json")["properties"]["concept_groups"]["items"]["properties"]["candidate_ids"]["uniqueItems"])
        validate_result(result, snapshot)
        plan["statement_reviews"][0]["candidates"][0].pop("evidence_quote")
        with self.assertRaises(Step2EvidenceError) as caught:
            self.process(snapshot, plan)
        self.assertEqual(caught.exception.code, "llm_schema_invalid")

    def test_v3_quote_failure_never_publishes_and_anchor_ids_track_membership(self):
        text = "控制器接收資料。控制器更新輸出。"
        snapshot = self.input(text)
        labels = [[("控制器", "控制器接收資料。"), ("控制器", "控制器更新輸出。")]]
        result, _ = self.process(snapshot, labels=labels)
        alternate = annotations(snapshot, labels)
        alternate["statement_reviews"][0]["candidates"][0]["evidence_quote"] = "控制器接收資料。控"
        same_anchors, _ = self.process(snapshot, alternate)
        self.assertEqual(result["knowledge_items"][0]["knowledge_item_id"],
                         same_anchors["knowledge_items"][0]["knowledge_item_id"])
        one_member, _ = self.process(snapshot, labels=[[labels[0][0]]])
        self.assertNotEqual(result["knowledge_items"][0]["knowledge_item_id"],
                            one_member["knowledge_items"][0]["knowledge_item_id"])
        bad = annotations(snapshot, labels)
        bad["statement_reviews"][0]["candidates"][0]["evidence_quote"] = text
        outdir = self.root / "bad-quote"
        with self.assertRaises(Step2EvidenceError) as caught:
            run_step2(snapshot.path, outdir, client=WireValidatingClient(bad))
        self.assertEqual(caught.exception.code, "quote_surface_ambiguous")
        self.assertEqual(list(outdir.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
