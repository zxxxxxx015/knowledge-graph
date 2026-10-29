"""Independent Step 1 CLI; importing this module never launches Codex."""

import argparse
import json
import math
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jsonschema.exceptions import SchemaError, ValidationError
from src.pipeline.step0_safety import ResultIntegrityError
from src.pipeline.step1_split import Step1Error, Step1LLMError, run_step1


def _positive_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout 必須是數字。") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("timeout 必須是大於零的有限秒數。")
    return result


def _retries(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-retries 必須是整數。") from None
    if result < 0:
        raise argparse.ArgumentTypeError("max-retries 不得小於零。")
    return result


def main(argv: list[str] | None = None, *, client=None) -> int:
    parser = argparse.ArgumentParser(description="Step 1：僅從 Step 0 JSON 提出知識條文邊界並保留完整追溯。")
    parser.add_argument("--input", required=True, type=Path, help="正式 Step 0 .json 路徑，不接受 .md 或 .txt")
    parser.add_argument("--output-dir", required=True, type=Path, help="獨立輸出目錄；自動建立，禁止覆寫")
    parser.add_argument("--timeout", type=_positive_seconds, default=180.0, help="每次 LLM 呼叫逾時秒數（預設 180）")
    parser.add_argument("--max-retries", type=_retries, default=0, help="額外重試上限（預設 0，沿用 Client 的 timeout 重試）")
    args = parser.parse_args(argv)
    try:
        result, path = run_step1(args.input, args.output_dir, client=client, timeout=args.timeout, max_retries=args.max_retries)
    except (SchemaError, ValidationError, ResultIntegrityError):
        print("Step 1 輸入或輸出完整性驗證失敗；未發布結果。請檢查 Step 0 JSON、Schema 與版本。", file=sys.stderr)
        return 2
    except Step1LLMError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError) as exc:
        # Avoid echoing malformed JSON or raw model diagnostics into the console.
        message = str(exc) if isinstance(exc, (Step1Error, OSError)) else "JSON 編碼、格式或參數錯誤。"
        print(f"Step 1 無法執行：{message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Step 1 已中斷；未宣稱成功。", file=sys.stderr)
        return 130
    print(json.dumps({"output_path": str(path), "status": result["status"]}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
