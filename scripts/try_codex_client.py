"""Opt-in LIVE smoke test: python -m scripts.try_codex_client.

Never imported or executed by the offline test suite.
"""

from dataclasses import asdict
import json
from pathlib import Path

from src.llm import CodexClient


def main() -> int:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "codex_smoke.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    result = CodexClient().generate_json(
        'Return exactly the JSON object {"success": true}. Do not use tools or read files.',
        schema,
        timeout=120,
        max_retries=0,
    )
    accepted = (
        result.success and result.data == {"success": True}
        and result.attempt_count == 1 and result.retry_count == 0
    )
    # Only the expected payload and sanitized metadata are printed.
    print(json.dumps({
        "success": accepted,
        "schema_valid": result.success,
        "attempt_count": result.attempt_count,
        "retry_count": result.retry_count,
        "error": asdict(result.error) if result.error else None,
        "attempts": [asdict(attempt) for attempt in result.attempts],
    }, ensure_ascii=False, indent=2))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
