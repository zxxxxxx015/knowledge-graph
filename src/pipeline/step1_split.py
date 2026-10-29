"""Step 1: semantic boundaries from Codex, exact extraction and provenance in Python.

Only Step 0 JSON is opened. Source Markdown is never consulted at runtime.
Invalid model output is rejected as a whole; offsets are never repaired.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from src.llm import CodexClient
from .step0_preprocess import JSONClient, validate_result as validate_step0
from .step0_safety import ResultIntegrityError, line_ranges, overlaps, valid_range


ROOT = Path(__file__).resolve().parents[2]
PROMPT_ID = "prompts/step1.md"
STRUCTURAL_REASONS = {
    "heading_context", "table_structure", "table_pending", "code_block", "unsupported_markdown",
}


class Step1Error(ValueError):
    """No safe Step 1 result can be published."""


class Step1LLMError(Step1Error):
    """The existing client returned a failed generation result."""


def schema(name: str) -> dict:
    result = json.loads((ROOT / "schemas" / name).read_bytes().decode("utf-8"))
    Draft202012Validator.check_schema(result)
    return result


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Step1Error("Step 0 JSON 含重複欄位。")
        result[key] = value
    return result


def _bad_constant(value):
    raise Step1Error("Step 0 JSON 含非標準數值。")


@dataclass(frozen=True)
class Step0Input:
    path: str
    raw: bytes

    @property
    def data(self) -> dict:
        return json.loads(self.raw.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_bad_constant)

    def metadata(self) -> dict:
        return {"step0_json_path": self.path, "sha256": hashlib.sha256(self.raw).hexdigest(), "size_bytes": len(self.raw)}


def load_step0(path: str | Path) -> Step0Input:
    path = Path(path).resolve()
    if path.suffix.lower() != ".json" or not path.is_file():
        raise Step1Error("Step 1 輸入必須是已存在的 Step 0 .json 檔案。")
    snapshot = Step0Input(str(path), path.read_bytes())
    _check_step0(snapshot.data)
    return snapshot


def _check_step0(data: dict) -> None:
    validate_step0(data)
    if data["status"]["execution_state"] == "llm_failed":
        raise Step1Error("Step 0 尚未成功完成；不可進入 Step 1。")


def _range(start: int, end: int) -> dict:
    return {"start": start, "end": end}


def _slice(text: str, span: dict) -> str:
    return text[span["start"]:span["end"]]


def original_mapping(data: dict, span: dict) -> dict:
    """Map boundaries only; a boundary inside replaced text is not invertible."""
    def boundary(position):
        delta = 0
        for item in data["replacements"]:
            processed, source = item["processed_range"], item["source_range"]
            if position <= processed["start"]:
                return position - delta
            if position < processed["end"]:
                return None
            delta += (processed["end"] - processed["start"]) - (source["end"] - source["start"])
        return position - delta

    start, end = boundary(span["start"]), boundary(span["end"])
    if start is None or end is None:
        return {"status": "unavailable", "source_range": None, "text": None, "reason": "邊界位於 Step 0 替換字串內部，不能安全反推。"}
    source_range = _range(start, end)
    return {"status": "available", "source_range": source_range, "text": _slice(data["original_text"], source_range), "reason": None}


def _original_envelope(data: dict, span: dict) -> dict:
    """Outward mapping is used ONLY for conservative trace association, not text."""
    points = []
    for key in ("start", "end"):
        point = span[key]
        for item in data["replacements"]:
            if item["processed_range"]["start"] < point < item["processed_range"]["end"]:
                points.append(item["source_range"][key])
                break
        else:
            mapping = original_mapping(data, _range(point, point))
            points.append(mapping["source_range"]["start"])
    return _range(*points)


def step0_trace(data: dict, spans: list[dict]) -> dict:
    envelopes = [_original_envelope(data, span) for span in spans]
    result = {
        "replacement_indexes": [i for i, item in enumerate(data["replacements"]) if any(overlaps(s, item["processed_range"]) for s in spans)],
        "unresolved_indexes": [], "warning_indexes": [],
        "unlocated_unresolved_indexes": [], "unlocated_warning_indexes": [],
    }
    for array, target in (("unresolved", "unresolved_indexes"), ("warnings", "warning_indexes")):
        for index, item in enumerate(data[array]):
            source = item["source_range"]
            if source is None:
                result["unlocated_" + target].append(index)
            elif any(overlaps(source, envelope) for envelope in envelopes):
                result[target].append(index)
    return result


def _fragment(data: dict, span: dict) -> dict:
    return {"processed_range": deepcopy(span), "text": _slice(data["processed_text"], span), "original_mapping": original_mapping(data, span)}


def _cells(text: str, span: dict) -> list[dict] | None:
    """Parse a strict pipe row without stripping any cell content or offsets."""
    line = _slice(text, span).rstrip("\r\n")
    separators = []
    for match in re.finditer(r"\|", line):
        offset = match.start()
        backslashes = len(line[:offset]) - len(line[:offset].rstrip("\\"))
        if backslashes % 2 == 0:
            separators.append(offset)
    if not separators or "`" in line or re.search(r"<[^>]*>|\\$", line):
        return None
    edges = [-1, *separators, len(line)]
    cells = [(a + 1, b) for a, b in zip(edges, edges[1:])]
    if not line[:separators[0]].strip():
        cells.pop(0)
    if not line[separators[-1] + 1:].strip():
        cells.pop()
    if len(cells) < 2 or any(not line[a:b].strip() or line[a:b].strip() in {"^", ">"} for a, b in cells):
        return None
    return [{"text": line[a:b], "processed_range": _range(span["start"] + a, span["start"] + b)} for a, b in cells]


def analyze_structure(data: dict) -> dict:
    """A conservative Markdown scanner, not a renderer or semantic segmenter."""
    text = data["processed_text"]
    lines = line_ranges(text)
    headings, tables, protected = [], [], []
    stack = []

    def protect(start, end, reason):
        protected.append({"processed_range": _range(start, end), "reason_code": reason})

    def heading(start, end, level, title):
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        item = {"heading_id": f"h_{start}", "parent_id": stack[-1]["heading_id"] if stack else None,
                "level": level, "title": title, **_fragment(data, _range(start, end))}
        headings.append(item)
        stack.append(item)
        protect(start, end, "heading_context")

    if data["source"]["document_type"] == "txt":
        return {"headings": [], "tables": [], "protected_regions": []}
    i = 0
    while i < len(lines):
        span = lines[i]
        start, end = span["start"], span["end"]
        line = _slice(text, span).rstrip("\r\n")
        clean = line.lstrip("\ufeff") if start == 0 else line
        if not clean.strip():
            i += 1
            continue
        # Fences inside list/quote containers are preserved conservatively too.
        fence = re.match(r"[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$", clean)
        if fence:
            marker = fence[1]
            j = i + 1
            while j < len(lines):
                closing = _slice(text, lines[j]).rstrip("\r\n")
                j += 1
                if re.fullmatch(r"[ \t>]*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*", closing):
                    break
            protect(start, lines[j - 1]["end"], "code_block")
            i = j
            continue
        if start == 0 and clean == "---":
            j = i + 1
            while j < len(lines) and _slice(text, lines[j]).strip() not in {"---", "..."}:
                j += 1
            j = min(j + 1, len(lines))
            protect(start, lines[j - 1]["end"], "unsupported_markdown")
            i = j
            continue
        if re.match(r"\s*<table\b", clean, re.I):
            j = i
            while j < len(lines):
                closing = _slice(text, lines[j])
                j += 1
                if re.search(r"</table\s*>", closing, re.I):
                    break
            protect(start, lines[j - 1]["end"], "table_pending")
            i = j
            continue
        html_code = re.match(r"\s*<(pre|script|style)\b", clean, re.I)
        if html_code:
            j = i
            while j < len(lines):
                closing = _slice(text, lines[j])
                j += 1
                if re.search(r"</" + html_code[1] + r"\s*>", closing, re.I):
                    break
            protect(start, lines[j - 1]["end"], "code_block")
            i = j
            continue
        if re.match(r"(?: {4}|\t)", line):
            j = i + 1
            while j < len(lines) and (not _slice(text, lines[j]).strip() or re.match(r"(?: {4}|\t)", _slice(text, lines[j]))):
                j += 1
            protect(start, lines[j - 1]["end"], "code_block")
            i = j
            continue
        match = re.match(r" {0,3}(#{1,6})(?:[ \t]+(.*)|$)", clean)
        if match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2] or "").strip()
            heading(start, end, len(match[1]), title)
            i += 1
            continue
        if i + 1 < len(lines) and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", _slice(text, lines[i + 1]).rstrip("\r\n")) and "|" not in line:
            underline = _slice(text, lines[i + 1]).lstrip()
            heading(start, lines[i + 1]["end"], 1 if underline.startswith("=") else 2, clean.strip())
            i += 2
            continue
        if "|" in line:
            # Include suspicious non-pipe continuation lines up to a blank or a
            # clear structural boundary: multiline cells are never guessed.
            j = i + 1
            while j < len(lines):
                following = _slice(text, lines[j])
                if not following.strip() or re.match(r"\s*(?:#{1,6}\s|`{3,}|~{3,}|<table\b)", following):
                    break
                j += 1
            block = lines[i:j]
            parsed = [_cells(text, row) for row in block]
            safe = len(block) >= 3 and all(cells is not None for cells in parsed)
            if safe:
                count = len(parsed[0])
                safe = all(len(cells) == count for cells in parsed) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", c["text"]) for c in parsed[1])
                safe = safe and len({c["text"].strip() for c in parsed[0]}) == count
                safe = safe and not any(all(re.fullmatch(r"\s*:?-{3,}:?\s*", c["text"]) for c in row) for row in parsed[2:])
            if not safe:
                protect(start, block[-1]["end"], "table_pending")
            else:
                table = {"table_id": f"t_{start}", **_fragment(data, _range(start, block[-1]["end"])),
                         "header": _fragment(data, block[0]), "separator": _fragment(data, block[1]), "rows": []}
                table["header"]["cells"] = [dict(column_index=k, **_fragment(data, c["processed_range"])) for k, c in enumerate(parsed[0])]
                for number, (row_span, row_cells) in enumerate(zip(block[2:], parsed[2:])):
                    row = {"row_index": number, **_fragment(data, row_span), "cells": []}
                    for k, cell in enumerate(row_cells):
                        row["cells"].append({"column_index": k, "header_text": parsed[0][k]["text"],
                                             "header_range": deepcopy(parsed[0][k]["processed_range"]), **_fragment(data, cell["processed_range"])})
                    table["rows"].append(row)
                tables.append(table)
                protect(start, block[1]["end"], "table_structure")
            i = j
            continue
        if re.match(r"\s*(?:>|<|\[[^\]]+\]:)", clean) or re.fullmatch(r"\s*(?:[-*_]\s*){3,}", clean):
            # Raw HTML / blockquote paragraphs stay opaque, including their
            # continuation lines. HTML tables were handled above.
            j = i + 1
            while j < len(lines) and _slice(text, lines[j]).strip() and not re.match(r"\s*#{1,6}\s", _slice(text, lines[j])):
                j += 1
            protect(start, lines[j - 1]["end"], "unsupported_markdown")
            i = j
            continue
        i += 1
    return {"headings": headings, "tables": tables, "protected_regions": protected}


def heading_context(structure: dict, position: int) -> list[dict]:
    # A retained heading belongs to its own parent chain, not the preceding
    # section. Keep the existing body-position lookup unchanged below.
    current = next((h for h in structure["headings"] if h["processed_range"]["start"] == position), None)
    if current is not None:
        by_id = {h["heading_id"]: h for h in structure["headings"]}
        ancestors = []
        parent_id = current["parent_id"]
        while parent_id is not None:
            parent = by_id[parent_id]
            ancestors.append(parent)
            parent_id = parent["parent_id"]
        return deepcopy(ancestors[::-1])
    stack = []
    for heading in structure["headings"]:
        if heading["processed_range"]["end"] > position:
            break
        while stack and stack[-1]["level"] >= heading["level"]:
            stack.pop()
        stack.append(heading)
    return deepcopy(stack)


def statement_id(source_hash: str, span: dict, text: str) -> str:
    payload = json.dumps([source_hash, span["start"], span["end"], text], ensure_ascii=False, separators=(",", ":"))
    return "ks_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _table_context(structure: dict, span: dict) -> dict | None:
    touched = [table for table in structure["tables"] if overlaps(span, table["processed_range"])]
    if not touched:
        return None
    if len(touched) != 1:
        raise Step1Error("條文跨越多張表格。")
    table = touched[0]
    rows = [row for row in table["rows"] if overlaps(span, row["processed_range"])]
    if not rows or span != _range(rows[0]["processed_range"]["start"], rows[-1]["processed_range"]["end"]):
        raise Step1Error("表格條文必須涵蓋完整、連續的資料列（含原始行尾換行）。")
    return {key: deepcopy(table[key]) for key in ("table_id", "processed_range", "text", "original_mapping", "header", "separator")} | {"rows": deepcopy(rows)}


def _check_decisions(data: dict, structure: dict, decisions: dict) -> None:
    text = data["processed_text"]
    occupied = [(item["processed_range"], "protected") for item in structure["protected_regions"]]
    for group in ("statements", "unhandled"):
        for item in decisions[group]:
            span = _range(item["start"], item["end"])
            if not valid_range(span, len(text)) or _slice(text, span) != item["text"] or not item["text"].strip():
                raise Step1Error("LLM 位置越界、為空或與指定文字不符；不搜尋或修正。")
            if any(overlaps(span, other) for other, _ in occupied):
                raise Step1Error("條文、未處理區域或受保護結構互相重疊。")
            table = _table_context(structure, span)
            if group == "statements":
                if (item["kind"] == "table") != (table is not None):
                    raise Step1Error("表格 kind 與結構不一致。")
                if table and len(table["rows"]) > 1 and item["boundary_reason"] != "table_dependency":
                    raise Step1Error("多列表格合併必須明確標記 table_dependency 並說明語意依賴。")
                if item["kind"] != "table" and item["boundary_reason"] in {"table_dependency", "table_row"}:
                    raise Step1Error("非表格條文不能使用表格邊界理由。")
            if data["source"]["document_type"] == "md":
                for code in re.finditer(r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)", text):
                    if any(code.start() < point < code.end() for point in (span["start"], span["end"])):
                        raise Step1Error("邊界不能切開 inline code。")
            occupied.append((span, group))
    cursor = 0
    for span, _ in sorted(occupied, key=lambda pair: pair[0]["start"]):
        if span["start"] < cursor:
            raise Step1Error("位置重疊。")
        if text[cursor:span["start"]].strip():
            raise Step1Error("存在未記錄的非空白正文，拒絕發布。")
        cursor = span["end"]
    if text[cursor:].strip():
        raise Step1Error("文件尾端有未記錄的非空白正文，拒絕發布。")


def _record(data: dict, structure: dict, span: dict, extra_spans=()) -> dict:
    return {**_fragment(data, span), "heading_context": heading_context(structure, span["start"]),
            "step0_trace": step0_trace(data, [span, *extra_spans])}


def _processed_only(value):
    """Do not expose original-text mappings as an alternate model source."""
    if isinstance(value, dict):
        return {key: _processed_only(item) for key, item in value.items() if key != "original_mapping"}
    if isinstance(value, list):
        return [_processed_only(item) for item in value]
    return value


def _expected_warnings(data: dict, statements: list, regions: list) -> list[dict]:
    warnings = []
    if data["warnings"] or data["unresolved"]:
        warnings.append({"code": "step0_issues_preserved", "message": "Step 0 unresolved 與 warnings 原樣保留；Step 1 不解決指涉。"})
    if any(r["reason_code"] not in {"heading_context", "table_structure"} for r in regions):
        warnings.append({"code": "unhandled_content", "message": "部分內容安全保留於 unhandled_regions，請人工檢視。"})
    if any(s["original_mapping"]["status"] == "unavailable" for s in [*statements, *regions]):
        warnings.append({"code": "mapping_unavailable", "message": "部分邊界不能安全映射回原文；保留 processed_range 與替換紀錄。"})
    return warnings


def validate_result(result: dict, snapshot: Step0Input | None = None) -> None:
    """Offline shape + full deterministic integrity validation, no LLM or source IO."""
    Draft202012Validator(schema("step1_result.schema.json")).validate(result)
    data = {key: deepcopy(result[key]) for key in ("source", "original_text", "processed_text")}
    data.update(deepcopy(result["step0_provenance"]))
    _check_step0(data)
    if snapshot is not None and (result["input"] != snapshot.metadata() or data != snapshot.data):
        raise Step1Error("Step 0 輸入快照或 provenance 已變動。")
    structure = analyze_structure(data)
    if result["structure"] != structure:
        raise Step1Error("Markdown 結構或表格儲存格追溯不一致。")
    decisions = {"statements": [], "unhandled": []}
    structural = []
    for item in result["knowledge_statements"]:
        span = item["processed_range"]
        table = _table_context(structure, span)
        extra = [table["header"]["processed_range"], table["separator"]["processed_range"]] if table else []
        expected = _record(data, structure, span, extra)
        if any(item[key] != value for key, value in expected.items()):
            raise Step1Error("條文文字、原文映射、章節或 Step 0 追溯不一致。")
        if item["statement_id"] != statement_id(data["source"]["sha256"], span, item["text"]) or item["source_ref"] != data["source"]["sha256"]:
            raise Step1Error("條文 ID 或來源識別不一致。")
        if item.get("table_context") != table:
            raise Step1Error("條文表格脈絡不一致。")
        decisions["statements"].append({**span, "text": item["text"], "kind": item["kind"],
                                        "boundary_reason": item["boundary_reason"], "reason": item["boundary_detail"]})
    for item in result["unhandled_regions"]:
        span = item["processed_range"]
        expected = _record(data, structure, span)
        if any(item[key] != value for key, value in expected.items()):
            raise Step1Error("未處理區域文字或追溯不一致。")
        if item["reason_code"] in STRUCTURAL_REASONS:
            structural.append({"processed_range": span, "reason_code": item["reason_code"]})
        else:
            decisions["unhandled"].append({**span, "text": item["text"], "reason_code": item["reason_code"], "reason": item["reason"]})
    if structural != structure["protected_regions"]:
        raise Step1Error("受保護結構未完整保留或被錯誤標記。")
    Draft202012Validator(schema("step1_llm.schema.json")).validate(decisions)
    _check_decisions(data, structure, decisions)
    for collection in (result["knowledge_statements"], result["unhandled_regions"]):
        if collection != sorted(collection, key=lambda item: item["processed_range"]["start"]):
            raise Step1Error("輸出範圍未依來源排序。")
    expected_warnings = _expected_warnings(data, result["knowledge_statements"], result["unhandled_regions"])
    status = result["status"]
    if result["warnings"] != expected_warnings or status["execution_state"] != ("completed_with_warnings" if expected_warnings else "completed"):
        raise Step1Error("警告及執行狀態不一致。")
    if (status["statement_count"] != len(result["knowledge_statements"])
            or status["unhandled_count"] != len(result["unhandled_regions"])
            or status["llm_retry_count"] != max(0, status["llm_call_count"] - 1)):
        raise Step1Error("執行統計不一致。")


class Step1Splitter:
    def __init__(self, client: JSONClient | None = None):
        self.client = client if client is not None else CodexClient()
        self.llm_schema = schema("step1_llm.schema.json")
        schema("step1_result.schema.json")  # Fail local schema setup before a paid request.
        prompt_bytes = (ROOT / PROMPT_ID).read_bytes()
        self.prompt = prompt_bytes.decode("utf-8")
        self.prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()

    def process(self, snapshot: Step0Input, *, timeout: float = 180.0, max_retries: int = 0) -> dict:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必須是大於零的有限秒數。")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries 必須是非負整數。")
        data = snapshot.data
        _check_step0(data)
        structure = analyze_structure(data)
        text = data["processed_text"]
        # Compute available intervals; the model never decides what is protected.
        available, cursor = [], 0
        for region in structure["protected_regions"]:
            span = region["processed_range"]
            if text[cursor:span["start"]].strip():
                available.append(_range(cursor, span["start"]))
            cursor = span["end"]
        if text[cursor:].strip():
            available.append(_range(cursor, len(text)))
        decisions = {"statements": [], "unhandled": []}
        attempts = retries = 0
        if available:
            payload = {"processed_text": text, "line_ranges": line_ranges(text),
                       "structure": _processed_only(structure), "available_ranges": available,
                       "boundary_hints": [{"start": m.start(), "end": m.end(), "text": m[0]}
                                          for m in re.finditer(r"[^。！？!?\r\n]+[。！？!?]?", text)]}
            response = self.client.generate_json(self.prompt + "\n\nSTEP1_DATA_JSON\n" + json.dumps(payload, ensure_ascii=False),
                                                 self.llm_schema, timeout=timeout, max_retries=max_retries)
            attempts, retries = response.attempt_count, response.retry_count
            if not response.success:
                kind = response.error.kind.value if response.error else "unknown"
                raise Step1LLMError(f"Step 1 LLM 呼叫失敗（{kind}）；未發布結果。")
            decisions = response.data
        try:
            Draft202012Validator(self.llm_schema).validate(decisions)
        except ValidationError:
            raise Step1Error("Step 1 LLM 回應不符合 Schema；未發布結果。") from None
        _check_decisions(data, structure, decisions)
        statements = []
        for item in sorted(decisions["statements"], key=lambda item: item["start"]):
            span = _range(item["start"], item["end"])
            table = _table_context(structure, span)
            extra = [table["header"]["processed_range"], table["separator"]["processed_range"]] if table else []
            statement = {"statement_id": statement_id(data["source"]["sha256"], span, _slice(text, span)),
                         "source_ref": data["source"]["sha256"], "kind": item["kind"],
                         "boundary_reason": item["boundary_reason"], "boundary_detail": item["reason"],
                         **_record(data, structure, span, extra)}
            if table:
                statement["table_context"] = table
            statements.append(statement)
        regions = [{**_record(data, structure, region["processed_range"]), "reason_code": region["reason_code"],
                    "reason": "Python 保護 Markdown 結構；原樣保留。"} for region in structure["protected_regions"]]
        regions.extend({**_record(data, structure, _range(item["start"], item["end"])),
                        "reason_code": item["reason_code"], "reason": item["reason"]} for item in decisions["unhandled"])
        regions.sort(key=lambda item: item["processed_range"]["start"])
        warnings = _expected_warnings(data, statements, regions)
        result = {"input": snapshot.metadata(), "source": deepcopy(data["source"]), "original_text": data["original_text"],
                  "processed_text": text, "step0_provenance": {key: deepcopy(data[key]) for key in ("replacements", "unresolved", "warnings", "status")},
                  "structure": structure, "knowledge_statements": statements, "unhandled_regions": regions,
                  "warnings": warnings, "status": {"execution_state": "completed_with_warnings" if warnings else "completed",
                  "prompt_id": PROMPT_ID, "prompt_sha256": self.prompt_sha256, "llm_call_count": attempts,
                  "llm_retry_count": retries, "statement_count": len(statements), "unhandled_count": len(regions)}}
        validate_result(result, snapshot)
        return result


def prepare_output_path(snapshot: Step0Input, output_dir: str | Path) -> Path:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(snapshot.path).stem.removesuffix(".step0")
    path = directory / (stem + ".step1.json")
    if path.exists() or path.is_symlink():
        raise FileExistsError("Step 1 結果已存在；請使用新的輸出目錄，禁止覆寫。")
    if path.resolve() in {Path(snapshot.path).resolve(), Path(snapshot.data["source"]["path"]).resolve()}:
        raise Step1Error("輸出不能等於 Step 0 JSON 或原始來源路徑。")
    with tempfile.TemporaryFile(dir=directory):
        pass
    return path


def save_result(result: dict, destination: str | Path) -> Path:
    validate_result(result)
    snapshot = load_step0(result["input"]["step0_json_path"])
    validate_result(result, snapshot)
    path = Path(destination).absolute()
    if path.resolve() in {Path(snapshot.path).resolve(), Path(result["source"]["path"]).resolve()}:
        raise Step1Error("禁止覆寫輸入或原始來源。")
    if path.exists() or path.is_symlink():
        raise FileExistsError("結果已存在，禁止覆寫。")
    payload = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".step1-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def run_step1(input_path: str | Path, output_dir: str | Path, *, client: JSONClient | None = None,
              timeout: float = 180.0, max_retries: int = 0) -> tuple[dict, Path]:
    snapshot = load_step0(input_path)
    destination = prepare_output_path(snapshot, output_dir)
    result = Step1Splitter(client).process(snapshot, timeout=timeout, max_retries=max_retries)
    return result, save_result(result, destination)
