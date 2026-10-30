"""Step 2: conservative concept review with exact, reproducible provenance.

Only the Step 1 JSON is opened. Text-evidence contract v3: the model supplies
verbatim fragments and optional context quotes; Python locates exact matches
inside their owning statements.
No offset fallback, normalization, source IO or global entity resolution.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from src.llm import CodexClient
from .step0_preprocess import JSONClient
from .step0_safety import overlaps, valid_range
from .step1_split import original_mapping, schema, step0_trace, validate_result as validate_step1


ROOT = Path(__file__).resolve().parents[2]
PROMPT_ID = "prompts/step2.md"
ID_VERSION = "step2-concept-anchors-v1"


class Step2Error(ValueError):
    """No safe Step 2 result can be published."""


class Step2LLMError(Step2Error):
    """The existing client reported failed generation."""

    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class Step2EvidenceError(Step2Error):
    """Content-free, fixed failure classification for evidence contract v3."""

    def __init__(self, code: str, role: str):
        self.code, self.role = code, role
        detail = "Schema 驗證失敗" if code == "llm_schema_invalid" else "證據定位失敗"
        super().__init__(f"Step 2 {detail} [{code}]（{role}）；未發布結果。")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Step2Error("Step 1 JSON 含重複欄位。")
        result[key] = value
    return result


def _bad_constant(value):
    raise Step2Error("Step 1 JSON 含非標準數值。")


@dataclass(frozen=True)
class Step1Input:
    path: str
    raw: bytes

    @property
    def data(self) -> dict:
        return json.loads(self.raw.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_bad_constant)

    def metadata(self) -> dict:
        return {"step1_json_path": self.path, "sha256": hashlib.sha256(self.raw).hexdigest(), "size_bytes": len(self.raw)}


def _check_step1(data: dict) -> None:
    validate_step1(data)
    if data["status"]["execution_state"] not in {"completed", "completed_with_warnings"}:
        raise Step2Error("Step 1 尚未成功完成。")
    ids = [s["statement_id"] for s in data["knowledge_statements"]]
    if len(ids) != len(set(ids)):
        raise Step2Error("Step 1 statement_id 重複。")


def load_step1(path: str | Path) -> Step1Input:
    path = Path(path).resolve()
    if path.suffix.lower() != ".json" or not path.is_file():
        raise Step2Error("Step 2 輸入必須是已存在的 Step 1 .json 檔案。")
    snapshot = Step1Input(str(path), path.read_bytes())
    _check_step1(snapshot.data)
    return snapshot


def _hash_id(prefix: str, payload) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return prefix + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _source_data(data: dict) -> dict:
    return {key: data[key] for key in ("original_text", "processed_text")} | data["step0_provenance"]


def _exact_range(statement: dict, fragment: str, role: str, evidence_quote: str | None = None) -> dict:
    """Locate one exact fragment within its statement, optionally via an exact quote.

    A quote identifies one explicitly supplied occurrence. Neither this function
    nor its caller creates occurrences for other matches of the same text.
    """
    if not isinstance(fragment, str) or not fragment.strip():
        raise Step2EvidenceError("evidence_empty", role)
    text = statement["text"]
    if evidence_quote is None:
        start = text.find(fragment)
        if start < 0:
            raise Step2EvidenceError("evidence_not_found", role)
        if text.find(fragment, start + 1) >= 0:
            raise Step2EvidenceError("evidence_ambiguous", role)
    else:
        if not isinstance(evidence_quote, str) or not evidence_quote.strip():
            raise Step2EvidenceError("quote_empty", role)
        quote_start = text.find(evidence_quote)
        if quote_start < 0:
            raise Step2EvidenceError("quote_not_found", role)
        if text.find(evidence_quote, quote_start + 1) >= 0:
            raise Step2EvidenceError("quote_ambiguous", role)
        inside = evidence_quote.find(fragment)
        if inside < 0:
            raise Step2EvidenceError("quote_surface_not_found", role)
        if evidence_quote.find(fragment, inside + 1) >= 0:
            raise Step2EvidenceError("quote_surface_ambiguous", role)
        start = quote_start + inside
    local = {"start": start, "end": start + len(fragment)}
    if statement["kind"] == "table":
        base = statement["processed_range"]["start"]
        cells = [cell for row in statement["table_context"]["rows"] for cell in row["cells"]
                 if cell["processed_range"]["start"] <= base + local["start"] < base + local["end"] <= cell["processed_range"]["end"]]
        if len(cells) != 1:
            raise Step2EvidenceError("table_cell_mismatch", role)
    return local


def _check_llm_decisions(decisions: dict) -> None:
    try:
        Draft202012Validator(schema("step2_llm.schema.json")).validate(decisions)
    except ValidationError:
        raise Step2EvidenceError("llm_schema_invalid", "response") from None


def _locate_decisions(data: dict, decisions: dict) -> dict:
    """Validate the v3 model contract, then add Python-owned ranges for storage."""
    _check_llm_decisions(decisions)
    located = deepcopy(decisions)
    statements = {s["statement_id"]: s for s in data["knowledge_statements"]}

    def locate(sid, fragment, role, evidence_quote):
        if sid not in statements:
            raise Step2EvidenceError("statement_reference_invalid", role)
        return _exact_range(statements[sid], fragment, role, evidence_quote)

    for review in located["statement_reviews"]:
        if review["statement_id"] not in statements:
            raise Step2EvidenceError("statement_reference_invalid", "review")
        for candidate in review["candidates"]:
            candidate.update(locate(review["statement_id"], candidate["surface_text"], "candidate",
                                    candidate["evidence_quote"]))
    for group in located["concept_groups"]:
        for evidence in group["identity_evidence"]:
            evidence.update(locate(evidence["statement_id"], evidence["text"], "identity_evidence",
                                   evidence["evidence_quote"]))
    return located


def _located_decision_schema() -> dict:
    """Keep the established result/decision shape, including Python-owned ranges.

    The result schema's embedded decision schema is intentionally stable; it is
    no longer the schema sent to the model. Rebase its local refs for validation.
    """
    def rebase(value):
        if isinstance(value, dict):
            return {key: (child.replace("#/$defs/llm/", "#/", 1) if key == "$ref" else rebase(child))
                    for key, child in value.items()}
        if isinstance(value, list):
            return [rebase(child) for child in value]
        return value
    return rebase(schema("step2_result.schema.json")["$defs"]["llm"])


def _location(data: dict, statement: dict, candidate: dict) -> dict:
    local = _exact_range(statement, candidate["surface_text"], "candidate", candidate.get("evidence_quote"))
    reported = {"start": candidate["start"], "end": candidate["end"]}
    if not valid_range(reported, len(statement["text"])) or reported != local:
        raise Step2EvidenceError("derived_range_mismatch", "candidate")
    base = statement["processed_range"]["start"]
    absolute = {key: base + local[key] for key in ("start", "end")}
    if data["processed_text"][absolute["start"]:absolute["end"]] != candidate["surface_text"]:
        raise Step2EvidenceError("processed_range_mismatch", "candidate")
    table_ref = None
    if statement["kind"] == "table":
        table = statement["table_context"]
        cells = [(row, cell) for row in table["rows"] for cell in row["cells"]
                 if cell["processed_range"]["start"] <= absolute["start"] < absolute["end"] <= cell["processed_range"]["end"]]
        if len(cells) != 1:
            raise Step2EvidenceError("table_cell_mismatch", "candidate")
        row, cell = cells[0]
        table_ref = {"table_id": table["table_id"], "row_index": row["row_index"],
                     "column_index": cell["column_index"], "cell_processed_range": deepcopy(cell["processed_range"]),
                     "header_text": cell["header_text"], "header_range": deepcopy(cell["header_range"])}
    source_data = _source_data(data)
    trace_spans = [absolute, table_ref["header_range"]] if table_ref else [absolute]
    quote = {"evidence_quote": candidate["evidence_quote"]} if "evidence_quote" in candidate else {}
    return {"surface_text": candidate["surface_text"], **quote, "statement_local_range": local, "processed_range": absolute,
            "original_mapping": original_mapping(source_data, absolute), "step0_trace": step0_trace(source_data, trace_spans),
            "table_cell_ref": table_ref}


def _build(data: dict, decisions: dict) -> dict:
    """Validate model references and reproduce every material output field."""
    try:
        Draft202012Validator(_located_decision_schema()).validate(decisions)
    except ValidationError:
        raise Step2Error("Step 2 LLM 回應不符合 Schema；未發布結果。") from None
    statements = {s["statement_id"]: s for s in data["knowledge_statements"]}
    reviews, candidates, confirmed, locations = {}, {}, set(), {}
    occupied = set()
    for review in decisions["statement_reviews"]:
        sid = review["statement_id"]
        if sid not in statements or sid in reviews:
            raise Step2Error("review statement_id 不存在或重複。")
        reviews[sid] = review
        expected_basis = {"kept": "knowledge_bearing", "filtered": "no_knowledge_value", "needs_review": "uncertain"}
        if review["basis"] != expected_basis[review["decision"]]:
            raise Step2Error("過濾或待審判準與 decision 不一致。")
        spans = []
        for candidate in review["candidates"]:
            cid = candidate["candidate_id"]
            if cid in candidates:
                raise Step2Error("candidate_id 重複。")
            location = _location(data, statements[sid], candidate)
            anchor = (sid, candidate["start"], candidate["end"])
            if anchor in occupied:
                raise Step2Error("同一出現位置不可重複提出候選。")
            occupied.add(anchor)
            candidates[cid] = (sid, candidate)
            locations[cid] = location
            if candidate["resolution"] == "confirmed":
                if review["decision"] != "kept" or candidate["unit_kind"] != "concept":
                    raise Step2Error("只有 kept 的核心概念候選可確認；條件、行為、狀態及因果敘述不可成為正式知識項。")
                if any(overlaps(location["statement_local_range"], span) for span in spans):
                    raise Step2Error("已確認概念的出現範圍重疊；不得同時拆分與保留整個概念。")
                spans.append(location["statement_local_range"])
                confirmed.add(cid)
        if review["decision"] == "kept" and not any(c["resolution"] == "confirmed" for c in review["candidates"]):
            raise Step2Error("kept 必須至少有一個經驗證且身分確認的概念。")
    if set(reviews) != set(statements):
        raise Step2Error("每個 Step 1 statement_id 必須恰好有一筆 review。")

    assigned, items, occurrences, candidate_items = set(), [], [], {}
    identities = set()
    for group in decisions["concept_groups"]:
        members = group["candidate_ids"]
        if len(set(members)) != len(members) or any(cid not in confirmed or cid in assigned for cid in members):
            raise Step2Error("概念群組引用不存在、未確認或重複衝突的候選。")
        assigned.update(members)
        if group["canonical_label"] not in {candidates[cid][1]["surface_text"] for cid in members}:
            raise Step2Error("canonical_label 必須取自群組中實際出現的概念文字，不能補充名稱。")
        identity = (group["canonical_label"], group["disambiguation"])
        if identity in identities:
            raise Step2Error("重複的概念身分群組；同義不明須待審，同名異義須提供不同消歧脈絡。")
        identities.add(identity)
        member_sids = {candidates[cid][0] for cid in members}
        evidence_sids = set()
        for evidence in group["identity_evidence"]:
            sid = evidence["statement_id"]
            if sid not in member_sids:
                raise Step2EvidenceError("identity_reference_invalid", "identity_evidence")
            span = _exact_range(statements[sid], evidence["text"], "identity_evidence", evidence.get("evidence_quote"))
            reported = {"start": evidence["start"], "end": evidence["end"]}
            if not valid_range(reported, len(statements[sid]["text"])) or span != reported:
                raise Step2EvidenceError("derived_range_mismatch", "identity_evidence")
            evidence_sids.add(sid)
        if evidence_sids != member_sids:
            raise Step2Error("合併群組必須為每個成員條文提供身分依據。")
        anchors = sorted(({"statement_id": candidates[cid][0], "start": candidates[cid][1]["start"], "end": candidates[cid][1]["end"]}
                          for cid in members), key=lambda a: (a["statement_id"], a["start"], a["end"]))
        identity_input = {"version": ID_VERSION, "source_sha256": data["source"]["sha256"], "anchors": anchors}
        kid = _hash_id("ki_", identity_input)
        item_occurrences = []
        for cid in members:
            sid, candidate = candidates[cid]
            oid = _hash_id("occ_", {"knowledge_item_id": kid, "statement_id": sid,
                                   "start": candidate["start"], "end": candidate["end"]})
            occurrences.append({"occurrence_id": oid, "knowledge_item_id": kid, "statement_id": sid,
                                "source_ref": data["source"]["sha256"], **locations[cid]})
            item_occurrences.append(oid)
            candidate_items[cid] = kid
        items.append({"knowledge_item_id": kid, "canonical_label": group["canonical_label"],
                      "disambiguation": group["disambiguation"], "merge_reason": group["reason"],
                      "identity_evidence": deepcopy(group["identity_evidence"]), "identity_input": identity_input,
                      "occurrence_ids": sorted(item_occurrences), "source_ref": data["source"]["sha256"]})
    if assigned != confirmed:
        raise Step2Error("每個已確認候選必須恰好屬於一個概念群組。")

    output_reviews = []
    for sid, statement in statements.items():
        review = reviews[sid]
        pending = [{**deepcopy(c), **locations[c["candidate_id"]]} for c in review["candidates"] if c["resolution"] == "pending"]
        output_reviews.append({"statement_id": sid, "decision": review["decision"], "basis": review["basis"],
                               "reason": review["reason"], "knowledge_item_ids": sorted({candidate_items[c["candidate_id"]] for c in review["candidates"] if c["resolution"] == "confirmed"}),
                               "pending_candidates": pending})
    warnings = []
    if data["warnings"] or data["step0_provenance"]["unresolved"]:
        warnings.append({"code": "upstream_issues_preserved", "message": "Step 1 警告與 Step 0 未解析紀錄原樣保留於 step1_snapshot。"})
    if any(r["decision"] == "needs_review" or r["pending_candidates"] for r in output_reviews):
        warnings.append({"code": "review_pending", "message": "部分條文或概念待人工審查；待審候選未建立正式知識項。"})
    if any(o["original_mapping"]["status"] == "unavailable" for o in occurrences):
        warnings.append({"code": "mapping_unavailable", "message": "部分出現邊界位於替換字串內部，不能精確反推原文。"})
    return {"statement_reviews": output_reviews, "knowledge_items": sorted(items, key=lambda i: i["knowledge_item_id"]),
            "occurrences": sorted(occurrences, key=lambda o: (o["processed_range"]["start"], o["processed_range"]["end"], o["occurrence_id"])),
            "warnings": warnings}


def _counts(material: dict) -> dict:
    reviews = material["statement_reviews"]
    return {"reviewed_statement_count": len(reviews),
            **{d + "_count": sum(r["decision"] == d for r in reviews) for d in ("kept", "filtered", "needs_review")},
            "knowledge_item_count": len(material["knowledge_items"]), "occurrence_count": len(material["occurrences"])}


def _processed_context(value):
    """Keep table/heading context, exclude nested alternate source text."""
    if isinstance(value, dict):
        return {key: _processed_context(child) for key, child in value.items() if key != "original_mapping"}
    if isinstance(value, list):
        return [_processed_context(child) for child in value]
    return value


def validate_result(result: dict, snapshot: Step1Input | None = None) -> None:
    """Rebuild all links, IDs, provenance, decisions and statistics, offline."""
    Draft202012Validator(schema("step2_result.schema.json")).validate(result)
    data = result["step1_snapshot"]
    _check_step1(data)
    if result["source"] != data["source"]:
        raise Step2Error("來源識別與 Step 1 不一致。")
    if snapshot is not None and (result["input"] != snapshot.metadata() or data != snapshot.data):
        raise Step2Error("Step 1 輸入快照或 SHA-256 已變動。")
    material = _build(data, result["extraction_decisions"])
    if any(result[key] != value for key, value in material.items()):
        raise Step2Error("正式結果與候選審查、概念身分、出現位置或追溯不一致。")
    status = result["status"]
    if any(status[key] != value for key, value in _counts(material).items()):
        raise Step2Error("Step 2 統計不一致。")
    if (status["execution_state"] != ("completed_with_warnings" if material["warnings"] else "completed")
            or status["llm_retry_count"] != max(0, status["llm_call_count"] - 1)
            or bool(status["llm_call_count"]) != bool(data["knowledge_statements"])):
        raise Step2Error("Step 2 執行狀態或 LLM 統計不一致。")


class Step2Extractor:
    def __init__(self, client: JSONClient | None = None):
        self.client = client if client is not None else CodexClient()
        self.llm_schema = schema("step2_llm.schema.json")
        schema("step2_result.schema.json")
        prompt_bytes = (ROOT / PROMPT_ID).read_bytes()
        self.prompt = prompt_bytes.decode("utf-8")
        self.prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()

    def process(self, snapshot: Step1Input, *, timeout: float = 360.0, max_retries: int = 0) -> dict:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必須是大於零的有限秒數。")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries 必須是非負整數。")
        data = snapshot.data
        _check_step1(data)
        decisions = {"statement_reviews": [], "concept_groups": []}
        attempts = retries = 0
        if data["knowledge_statements"]:
            # Original mappings, file paths and raw original text are not alternate
            # extraction sources. Context remains attached to its owning statement.
            payload = {"knowledge_statements": [{key: _processed_context(s[key]) for key in
                       ("statement_id", "text", "kind", "processed_range", "heading_context", "step0_trace", "table_context") if key in s}
                       for s in data["knowledge_statements"]]}
            # Relax only transport uniqueness. The complete v3 schema is still
            # checked in _locate_decisions(), before adding Python-owned ranges.
            request_schema = deepcopy(self.llm_schema)
            del request_schema["properties"]["concept_groups"]["items"]["properties"]["candidate_ids"]["uniqueItems"]
            response = self.client.generate_json(self.prompt + "\n\nSTEP2_DATA_JSON\n" + json.dumps(payload, ensure_ascii=False),
                                                 request_schema, timeout=timeout, max_retries=max_retries)
            attempts, retries = response.attempt_count, response.retry_count
            if not response.success:
                kind = response.error.kind.value if response.error else "unknown"
                returncode = None
                detail = kind
                if kind == "cli_execution":
                    reported = response.attempts[-1].returncode if response.attempts else None
                    returncode = reported if type(reported) is int else None
                    detail += f"；CLI returncode={returncode if returncode is not None else 'unknown'}"
                # Never include the client's error.message or process output.
                raise Step2LLMError(f"Step 2 LLM 呼叫失敗（{detail}）；未發布結果。", returncode=returncode)
            decisions = _locate_decisions(data, response.data)
        material = _build(data, decisions)
        result = {"input": snapshot.metadata(), "source": deepcopy(data["source"]), "step1_snapshot": deepcopy(data),
                  "extraction_decisions": deepcopy(decisions), **material,
                  "status": {"execution_state": "completed_with_warnings" if material["warnings"] else "completed",
                             "prompt_id": PROMPT_ID, "prompt_sha256": self.prompt_sha256,
                             "llm_call_count": attempts, "llm_retry_count": retries, **_counts(material)}}
        validate_result(result, snapshot)
        return result


def _protected_paths(snapshot: Step1Input) -> set[Path]:
    data = snapshot.data
    return {Path(p).resolve() for p in (snapshot.path, data["source"]["path"], data["input"]["step0_json_path"])}


def prepare_output_path(snapshot: Step1Input, output_dir: str | Path) -> Path:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (Path(snapshot.path).stem.removesuffix(".step1") + ".step2.json")
    if path.exists() or path.is_symlink():
        raise FileExistsError("Step 2 結果已存在；請使用新目錄，禁止覆寫。")
    if path.resolve() in _protected_paths(snapshot):
        raise Step2Error("輸出不能等於任何上游輸入或原始來源。")
    with tempfile.TemporaryFile(dir=directory):
        pass
    return path


def save_result(result: dict, destination: str | Path) -> Path:
    validate_result(result)
    snapshot = load_step1(result["input"]["step1_json_path"])
    validate_result(result, snapshot)
    path = Path(destination).absolute()
    if path.resolve() in _protected_paths(snapshot):
        raise Step2Error("禁止覆寫輸入或原始來源。")
    if path.exists() or path.is_symlink():
        raise FileExistsError("結果已存在，禁止覆寫。")
    if not path.name.endswith(".step2.json"):
        raise Step2Error("正式輸出檔名必須以 .step2.json 結尾。")
    payload = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".step2-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def run_step2(input_path: str | Path, output_dir: str | Path, *, client: JSONClient | None = None,
              timeout: float = 360.0, max_retries: int = 0) -> tuple[dict, Path]:
    snapshot = load_step1(input_path)
    destination = prepare_output_path(snapshot, output_dir)
    result = Step2Extractor(client).process(snapshot, timeout=timeout, max_retries=max_retries)
    return result, save_result(result, destination)
