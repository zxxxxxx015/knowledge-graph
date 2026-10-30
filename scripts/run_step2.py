"""Independent Step 2 CLI; importing never launches Codex."""

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jsonschema.exceptions import SchemaError, ValidationError
from scripts.run_pipeline import _positive_seconds, _retries
from src.pipeline.step0_safety import ResultIntegrityError
from src.pipeline.step1_split import Step1Error
from src.pipeline.step2_extract import Step2Error, Step2LLMError, run_step2


def main(argv: list[str] | None = None, *, client=None) -> int:
    parser = argparse.ArgumentParser(description="Step 2：僅從 Step 1 JSON 提取核心概念、保守過濾並保留追溯。")
    parser.add_argument("--input", required=True, type=Path, help="正式 Step 1 .json 路徑")
    parser.add_argument("--output-dir", required=True, type=Path, help="獨立輸出目錄；禁止覆寫")
    parser.add_argument("--timeout", type=_positive_seconds, default=360.0)
    parser.add_argument("--max-retries", type=_retries, default=0)
    args = parser.parse_args(argv)
    try:
        result, path = run_step2(args.input, args.output_dir, client=client, timeout=args.timeout, max_retries=args.max_retries)
    except (SchemaError, ValidationError, ResultIntegrityError):
        print("Step 2 輸入或輸出完整性驗證失敗；未發布結果。", file=sys.stderr)
        return 2
    except Step2LLMError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, (Step2Error, Step1Error, OSError)) else "JSON 編碼、格式或參數錯誤。"
        print(f"Step 2 無法執行：{message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Step 2 已中斷；未宣稱成功。", file=sys.stderr)
        return 130
    print(json.dumps({"output_path": str(path), "status": result["status"]}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
