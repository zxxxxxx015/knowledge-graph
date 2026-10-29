"""Explicit Step 0 CLI. Importing this module does not make an LLM call."""

import argparse
import json
import math
from pathlib import Path
import sys

# Also support python scripts/run_step0.py from outside the project directory.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingestion import SourceLoadError
from src.pipeline.step0_preprocess import run_step0
from src.pipeline.step0_safety import ResultIntegrityError
from jsonschema.exceptions import SchemaError, ValidationError


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout 必須是數字。") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout 必須是大於零的有限秒數。")
    return seconds


def _retries(value: str) -> int:
    try:
        retries = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-retries 必須是整數。") from None
    if retries < 0:
        raise argparse.ArgumentTypeError("max-retries 不得小於零。")
    return retries


def main(argv: list[str] | None = None, *, client=None) -> int:
    parser = argparse.ArgumentParser(description="Step 0：以原文證據還原指涉，完整保存來源與處理後文字。")
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 .md 或 .txt 來源路徑")
    parser.add_argument("--output-dir", required=True, type=Path, help="獨立輸出目錄；禁止覆寫既有結果")
    parser.add_argument("--timeout", type=_positive_seconds, default=120.0, help="每次 LLM 嘗試的逾時秒數（預設 120）")
    parser.add_argument("--max-retries", type=_retries, default=0, help="逾時後的額外嘗試上限（預設 0）")
    args = parser.parse_args(argv)
    try:
        result, path = run_step0(
            args.input, args.output_dir, client=client,
            timeout=args.timeout, max_retries=args.max_retries,
        )
    except (SchemaError, ValidationError, ResultIntegrityError):
        print("Step 0 驗證失敗；未發布新的結果。請檢查本機 Prompt、Schema 或程式版本。", file=sys.stderr)
        return 2
    except (OSError, SourceLoadError, ValueError) as exc:
        print(f"Step 0 無法執行：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Step 0 已中斷。", file=sys.stderr)
        return 130
    print(json.dumps({"output_path": str(path), "status": result["status"]}, ensure_ascii=True, indent=2))
    return 1 if result["status"]["execution_state"] == "llm_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
