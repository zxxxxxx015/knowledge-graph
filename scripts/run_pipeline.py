"""Explicit Step 0 -> Step 1 -> optional Step 2; imports never launch an LLM."""

import argparse
from datetime import datetime
import math
from pathlib import Path
import sys
import tempfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jsonschema.exceptions import SchemaError, ValidationError

from src.ingestion import SourceLoadError
from src.pipeline.step0_preprocess import run_step0
from src.pipeline.step0_safety import ResultIntegrityError
from src.pipeline.step1_split import Step1LLMError, run_step1
from src.pipeline.step2_extract import Step2LLMError, run_step2


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs" / "pipeline"
SUCCESS_STATES = {"completed", "completed_with_warnings"}


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


def _report_failure(stage: str, message: str, step0_path: Path | None,
                    step1_path: Path | None = None, *, until: int = 1) -> None:
    print(f"{stage}：失敗（{message}）", file=sys.stderr)
    if stage not in {"Step 1", "Step 2"}:
        print("Step 1：未執行（前置步驟未成功）。", file=sys.stderr)
    if step0_path is not None:
        print(f"已保留 Step 0 JSON：{step0_path}", file=sys.stderr)
    if step1_path is not None:
        print(f"已保留 Step 1 JSON：{step1_path}", file=sys.stderr)
    else:
        print("知識條文數量：未產生（Step 1 未完成）。", file=sys.stderr)
    if until == 2:
        if stage != "Step 2":
            print("Step 2：未執行（前置步驟未成功）。", file=sys.stderr)
        print("Step 2 審查及提取統計：未產生。", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知識圖譜統一入口：依序執行 Step 0 → Step 1 → Step 2。")
    parser.add_argument("source", type=Path, help="必要的 UTF-8 .md 或 .txt 來源文件路徑")
    parser.add_argument("--until", type=int, choices=(0, 1, 2), default=1, help="執行到哪一步（預設 1）；2 啟用知識項提取")
    parser.add_argument("--timeout", type=_positive_seconds, default=360.0, help="每一步每次 LLM 嘗試的逾時秒數（預設 360）")
    parser.add_argument("--max-retries", type=_retries, default=0, help="每一步額外重試上限（預設 0）")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="執行目錄的上層目錄；每次都建立新的子目錄（預設 outputs/pipeline）")
    args = parser.parse_args(argv)

    stage = "初始化"
    step0_path = None
    step1_path = None

    def report_failure(message):
        _report_failure(stage, message, step0_path, step1_path, until=args.until)

    try:
        output_root = args.output_dir.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        # mkdtemp atomically reserves a fresh directory, including concurrent runs
        # with the same timestamp. Never reuse a previous execution directory.
        run_dir = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=output_root))
        print(f"執行目錄：{run_dir}", flush=True)
        options = {"timeout": args.timeout, "max_retries": args.max_retries}

        stage = "Step 0"
        print("Step 0：執行中…", flush=True)
        result0, actual_step0_path = run_step0(args.source.resolve(), run_dir / "step0", **options)
        state0 = result0["status"]["execution_state"]
        print(f"Step 0：{state0}\n  輸出 JSON：{actual_step0_path}", flush=True)
        if state0 not in SUCCESS_STATES:
            print("Step 1：未執行（Step 0 未成功）。", flush=True)
            print("知識條文數量：未產生（Step 1 未執行）。", flush=True)
            if args.until == 2:
                print("Step 2：未執行（Step 0 未成功）。", flush=True)
            return 1
        step0_path = actual_step0_path
        if args.until == 0:
            print("Step 1：未執行（--until 0）。", flush=True)
            print("知識條文數量：未產生（僅執行 Step 0）。", flush=True)
            return 0

        stage = "Step 1"
        print("Step 1：執行中…", flush=True)
        # Use the actual path returned by Step 0, never a guessed filename.
        result1, actual_step1_path = run_step1(actual_step0_path, run_dir / "step1", **options)
        state1 = result1["status"]["execution_state"]
        print(f"Step 1：{state1}\n  輸出 JSON：{actual_step1_path}", flush=True)
        if state1 not in SUCCESS_STATES:
            report_failure("執行狀態未成功")
            return 1
        print(f"知識條文數量：{len(result1['knowledge_statements'])}", flush=True)
        step1_path = actual_step1_path
        if args.until == 2:
            stage = "Step 2"
            print("Step 2：執行中…", flush=True)
            result2, step2_path = run_step2(step1_path, run_dir / "step2", **options)
            state2 = result2["status"]["execution_state"]
            print(f"Step 2：{state2}\n  輸出 JSON：{step2_path}", flush=True)
            if state2 not in SUCCESS_STATES:
                report_failure("執行狀態未成功")
                return 1
            status = result2["status"]
            print(f"審查條文數量：{status['reviewed_statement_count']}\n"
                  f"kept：{status['kept_count']} / filtered：{status['filtered_count']} / needs_review：{status['needs_review_count']}\n"
                  f"去重後知識項數量：{status['knowledge_item_count']}\noccurrences 數量：{status['occurrence_count']}", flush=True)
        return 0
    except (SchemaError, ValidationError, ResultIntegrityError):
        report_failure("Schema 或資料完整性驗證失敗，未宣稱成功")
        return 2
    except (Step1LLMError, Step2LLMError) as exc:
        report_failure(str(exc))
        return 1
    except (OSError, SourceLoadError, ValueError) as exc:
        report_failure(str(exc))
        return 2
    except KeyboardInterrupt:
        report_failure("使用者中斷")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
