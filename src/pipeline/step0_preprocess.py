"""Step 0: request semantic candidates, validate them, and apply exact slices."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from src.ingestion import SourceDocument, load_source
from src.llm import CodexClient, GenerationResult
from .step0_safety import (
    ResultIntegrityError, apply_candidates, line_ranges, overlap_indices, overlaps,
    protected_spans, reconstruct_processed_text, structure_signature,
    valid_range, validate_candidate,
)


_ROOT = Path(__file__).resolve().parents[2]
PROMPT_ID = "prompts/step0.md"


class JSONClient(Protocol):
    def generate_json(self, prompt: str, schema: dict, *, timeout: float, max_retries: int) -> GenerationResult: ...


def _schema(name: str) -> dict:
    schema = json.loads((_ROOT / "schemas" / name).read_bytes().decode("utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _unresolved(item: dict, code: str, reason: str, origin: str, text: str, index: int | None) -> dict:
    reported = item.get("source_range")
    exact = (
        reported is not None and valid_range(reported, len(text))
        and text[reported["start"]:reported["end"]] == item.get("original_text")
    )
    return {
        "original_text": item["original_text"] if exact else "",
        "source_range": dict(reported) if exact else None,
        "reported_text": item.get("original_text"),
        "reported_range": dict(reported) if reported is not None else None,
        "proposed_replacement": item.get("replacement_text"),
        "reason_code": code, "reason": reason, "origin": origin,
        "candidate_index": index,
    }


def validate_result(result: dict, schema: dict | None = None) -> None:
    """Validate both JSON shape and invariants JSON Schema cannot express."""
    Draft202012Validator(schema if schema is not None else _schema("step0_result.schema.json")).validate(result)
    original, processed, source = result["original_text"], result["processed_text"], result["source"]
    raw = original.encode("utf-8")
    if (
        source["char_count"] != len(original) or source["size_bytes"] != len(raw)
        or source["sha256"] != hashlib.sha256(raw).hexdigest()
    ):
        raise ResultIntegrityError("原文與來源大小、字元數或 SHA-256 不一致。")
    if reconstruct_processed_text(original, result["replacements"]) != processed:
        raise ResultIntegrityError("processed_text 不能由原文與 replacements 完整重建。")
    if structure_signature(original, source["document_type"]) != structure_signature(processed, source["document_type"]):
        raise ResultIntegrityError("換行或 Markdown 結構發生變化。")
    protections = protected_spans(original, source["document_type"])
    for index, item in enumerate(result["replacements"]):
        candidate = item | {"context": "specific_reference", "is_unique": True}
        if validate_candidate(original, candidate, index, set(), protections):
            raise ResultIntegrityError("結果包含未通過安全驗證的替換。")
        span = item["processed_range"]
        if processed[span["start"]:span["end"]] != item["replacement_text"]:
            raise ResultIntegrityError("processed_range 與替換後文字不一致。")
    for item in result["unresolved"]:
        span = item["source_range"]
        if span is not None:
            if not valid_range(span, len(original)) or original[span["start"]:span["end"]] != item["original_text"]:
                raise ResultIntegrityError("unresolved 的已驗證位置與原文不一致。")
            if any(overlaps(span, replacement["source_range"]) for replacement in result["replacements"]):
                raise ResultIntegrityError("unresolved 內容與成功替換重疊。")
        elif item["original_text"]:
            raise ResultIntegrityError("未驗證位置不能宣稱有已驗證原文。")
    for warning in result["warnings"]:
        if warning["source_range"] is not None and not valid_range(warning["source_range"], len(original)):
            raise ResultIntegrityError("warning 的來源位置無效。")
    status = result["status"]
    if (
        status["applied_count"] != len(result["replacements"])
        or status["unresolved_count"] != len(result["unresolved"])
        or status["warning_count"] != len(result["warnings"])
        or status["candidate_count"] != status["applied_count"] + status["rejected_count"]
        or status["llm_retry_count"] != max(0, status["llm_call_count"] - 1)
    ):
        raise ResultIntegrityError("執行統計與結果內容不一致。")
    rejected = [item for item in result["unresolved"] if item["candidate_index"] is not None]
    if (
        len(rejected) != status["rejected_count"]
        or len({item["candidate_index"] for item in rejected}) != len(rejected)
        or any(item["candidate_index"] >= status["candidate_count"] for item in rejected)
    ):
        raise ResultIntegrityError("候選拒絕紀錄與候選統計不一致。")
    if status["execution_state"] == "llm_failed":
        if result["replacements"] or original != processed or status["llm_error_kind"] is None:
            raise ResultIntegrityError("LLM 失敗時不得有替換或遺失原文。")
    else:
        expected = "completed_with_warnings" if result["warnings"] or result["unresolved"] else "completed"
        if status["execution_state"] != expected or status["llm_error_kind"] is not None:
            raise ResultIntegrityError("執行狀態與錯誤、警告紀錄不一致。")


class Step0Preprocessor:
    def __init__(self, client: JSONClient | None = None) -> None:
        self.client = client if client is not None else CodexClient()
        self.llm_schema = _schema("step0_llm.schema.json")
        self.result_schema = _schema("step0_result.schema.json")
        prompt_bytes = (_ROOT / PROMPT_ID).read_bytes()
        self.prompt = prompt_bytes.decode("utf-8")
        self.prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()

    def process(self, source: SourceDocument, *, timeout: float = 120.0, max_retries: int = 0) -> dict:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必須是大於零的有限秒數。")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries 必須是大於等於零的整數。")
        text = source.original_text
        output = {
            "source": source.metadata(), "original_text": text, "processed_text": text,
            "replacements": [], "unresolved": [], "warnings": [],
            "status": {
                "execution_state": "completed", "prompt_id": PROMPT_ID,
                "prompt_sha256": self.prompt_sha256,
                "llm_call_count": 0, "llm_retry_count": 0,
                "candidate_count": 0, "applied_count": 0, "rejected_count": 0,
                "unresolved_count": 0, "warning_count": 0, "llm_error_kind": None,
            },
        }
        # Validate the source snapshot before spending any quota.
        validate_result(output, self.result_schema)
        if not text.strip("\ufeff \t\r\n"):
            return output
        prompt = self.prompt + "\n\nSOURCE_DATA_JSON\n" + json.dumps({
            "source_text": text, "line_ranges": line_ranges(text),
        }, ensure_ascii=False)
        response = self.client.generate_json(
            prompt, self.llm_schema, timeout=timeout, max_retries=max_retries,
        )
        output["status"]["llm_call_count"] = response.attempt_count
        output["status"]["llm_retry_count"] = response.retry_count
        if not response.success:
            kind = response.error.kind.value if response.error is not None else "unknown_llm_failure"
            return self._failure(output, kind, "LLM 未成功完成；保留完整原文，不套用替換。")
        try:
            # Defense in depth: fake/adapted clients cannot bypass the contract.
            Draft202012Validator(self.llm_schema).validate(response.data)
        except ValidationError:
            return self._failure(output, "invalid_llm_response", "LLM 回應未通過 Step 0 候選 Schema；整批不套用。")
        candidates = response.data["candidates"]
        output["status"]["candidate_count"] = len(candidates)
        protections = protected_spans(text, source.document_type)
        conflicts = overlap_indices(candidates, len(text))
        for item in response.data["unresolved"]:
            unresolved = _unresolved(item, "ambiguous_reference", item["reason"], "llm", text, None)
            if unresolved["source_range"] is None:
                unresolved.update({
                    "origin": "python_validation", "reason_code": "invalid_unresolved_range",
                    "reason": "LLM unresolved 的位置或文字無法驗證；保留回報內容但不宣稱有效位置。",
                })
                self._warning(output, unresolved["reason_code"], unresolved["reason"], None, None)
            output["unresolved"].append(unresolved)
        accepted: list[tuple[int, dict]] = []
        for index, candidate in enumerate(candidates):
            rejection = validate_candidate(text, candidate, index, conflicts, protections)
            if rejection is None and any(
                item["source_range"] is not None and overlaps(candidate["source_range"], item["source_range"])
                for item in output["unresolved"]
            ):
                rejection = ("conflicting_llm_decisions", "此位置同時有 unresolved 或已拒絕紀錄，保守不替換。")
            if rejection is not None:
                self._reject(output, candidate, index, *rejection)
            else:
                accepted.append((index, candidate))
        processed, replacements = apply_candidates(text, [item for _, item in accepted])
        if structure_signature(text, source.document_type) != structure_signature(processed, source.document_type):
            for index, candidate in accepted:
                self._reject(output, candidate, index, "structure_changed", "整批替換未通過結構檢查，回復原文。")
        else:
            output["processed_text"], output["replacements"] = processed, replacements
        return self._finish(output)

    @staticmethod
    def _warning(output: dict, code: str, message: str, span: dict | None, index: int | None) -> None:
        output["warnings"].append({
            "code": code, "message": message, "source_range": span,
            "candidate_index": index,
        })

    def _reject(self, output: dict, candidate: dict, index: int, code: str, reason: str) -> None:
        item = _unresolved(candidate, code, reason, "python_validation", output["original_text"], index)
        output["unresolved"].append(item)
        self._warning(output, code, reason, item["source_range"], index)
        output["status"]["rejected_count"] += 1

    def _failure(self, output: dict, kind: str, message: str) -> dict:
        output["status"].update(execution_state="llm_failed", llm_error_kind=kind)
        self._warning(output, kind, message, None, None)
        text = output["original_text"]
        output["unresolved"].append(_unresolved({
            "original_text": text, "source_range": {"start": 0, "end": len(text)},
        }, "llm_failure", message, "llm_failure", text, None))
        return self._finish(output)

    def _finish(self, output: dict) -> dict:
        status = output["status"]
        status.update(
            applied_count=len(output["replacements"]), unresolved_count=len(output["unresolved"]),
            warning_count=len(output["warnings"]),
        )
        if status["execution_state"] != "llm_failed":
            status["execution_state"] = "completed_with_warnings" if output["unresolved"] or output["warnings"] else "completed"
        validate_result(output, self.result_schema)
        return output


def prepare_output_path(source: SourceDocument, output_dir: str | Path) -> Path:
    """Check common output errors before making a model request."""
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (Path(source.path).stem + ".step0.json")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Step 0 結果已存在；請改用新的輸出目錄，禁止覆寫。")
    # Probe write permission now, rather than after a paid request.
    with tempfile.TemporaryFile(dir=directory):
        pass
    return destination


def save_result(result: dict, destination: str | Path) -> Path:
    """Publish a complete file atomically without ever replacing an existing path.

    The temporary file and destination share a directory. os.link fails if the
    destination exists (including a race), unlike os.replace or POSIX rename.
    """
    validate_result(result)
    path = Path(destination).absolute()
    if path.resolve() == Path(result["source"]["path"]).resolve():
        raise ValueError("禁止將輸出寫入來源路徑。")
    if path.exists() or path.is_symlink():
        raise FileExistsError("結果已存在，禁止覆寫。")
    payload = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".step0-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def run_step0(
    input_path: str | Path, output_dir: str | Path, *,
    client: JSONClient | None = None, timeout: float = 120.0, max_retries: int = 0,
) -> tuple[dict, Path]:
    source = load_source(input_path)
    destination = prepare_output_path(source, output_dir)
    result = Step0Preprocessor(client).process(source, timeout=timeout, max_retries=max_retries)
    return result, save_result(result, destination)
