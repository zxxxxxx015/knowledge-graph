"""Python -> Codex CLI 0.165.0 -> strictly parsed and validated JSON.

No API client, credential access, automatic login, or live probe is performed.
Only generate_json() launches Codex. Logs deliberately exclude user content.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable


_LOGGER = logging.getLogger(__name__)
_IS_WINDOWS = os.name == "nt"


class ErrorKind(StrEnum):
    INVALID_SCHEMA = "invalid_schema"
    CLI_NOT_FOUND = "cli_not_found"
    CLI_EXECUTION = "cli_execution"
    TIMEOUT = "timeout"
    INVALID_JSON = "invalid_json"
    SCHEMA_VALIDATION = "schema_validation"
    IO_ERROR = "io_error"
    OUTPUT_LIMIT = "output_limit"
    PROCESS_CLEANUP = "process_cleanup"


# CLI exit codes do not reliably distinguish authentication/configuration failures
# from transient failures. Never retry these or local setup/cleanup failures.
_RETRYABLE = frozenset({
    ErrorKind.TIMEOUT, ErrorKind.INVALID_JSON, ErrorKind.SCHEMA_VALIDATION,
})


@dataclass(frozen=True)
class GenerationError:
    kind: ErrorKind
    message: str


@dataclass(frozen=True)
class AttemptRecord:
    number: int
    elapsed_seconds: float
    returncode: int | None
    error: GenerationError | None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class GenerationResult:
    success: bool
    data: Any
    error: GenerationError | None
    attempts: tuple[AttemptRecord, ...]

    @property
    def attempt_count(self) -> int:
        """Number of subprocess launch attempts, including failed launches."""
        return len(self.attempts)

    @property
    def retry_count(self) -> int:
        return max(0, self.attempt_count - 1)


def _no_external_resource(uri: str) -> Any:
    # Do not fetch remote schemas or read file:// references.
    raise NoSuchResource(ref=uri)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _reject_constant(value: str) -> Any:
    raise ValueError("Non-standard JSON number")


def _strict_loads(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_float=_finite_float,
        parse_constant=_reject_constant,
    )


class CodexClient:
    """One independent, ephemeral CLI session per attempt; no retry by default.

    codex_command is a trusted executable path/name, never a command string.
    temp_dir optionally selects an existing writable parent for temporary folders.
    The client keeps no mutable call history; each result contains its own records.
    """

    def __init__(
        self,
        *,
        codex_command: str | os.PathLike[str] | None = None,
        temp_dir: str | os.PathLike[str] | None = None,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")
        self.codex_command = os.fspath(codex_command) if codex_command else (
            "codex.cmd" if _IS_WINDOWS else "codex"
        )
        self.temp_dir = os.fspath(temp_dir) if temp_dir is not None else None
        self.max_output_bytes = max_output_bytes

    def generate_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        timeout: float = 120.0,
        max_retries: int = 0,
        retry_on: Iterable[ErrorKind] = (ErrorKind.TIMEOUT,),
        retry_delay: float = 0.0,
        model: str | None = None,
    ) -> GenerationResult:
        """Return validated data or a content-free error and attempt history.

        timeout applies separately to each CLI attempt, not the entire operation.
        max_retries counts additional attempts. Only TIMEOUT, INVALID_JSON and
        SCHEMA_VALIDATION may be selected for retry; only TIMEOUT is selected by
        default. Invalid arguments raise ValueError before launching anything.
        Schema syntax errors return INVALID_SCHEMA with zero attempts; unresolved
        references can also be discovered during final response validation.
        """
        self._check_arguments(prompt, timeout, max_retries, retry_delay, model)
        try:
            selected = frozenset(ErrorKind(kind) for kind in retry_on)
        except (TypeError, ValueError):
            raise ValueError("retry_on must contain ErrorKind values") from None
        if not selected <= _RETRYABLE:
            raise ValueError("Only timeout/JSON/schema validation errors may be retried")

        try:
            if not isinstance(schema, dict):
                raise ValueError("Schema must be an object")
            # Snapshot caller data and reject values that are not JSON serializable.
            schema_text = json.dumps(schema, ensure_ascii=False, allow_nan=False)
            schema_text.encode("utf-8")
            snapshot = _strict_loads(schema_text)
            if "$schema" in snapshot and not isinstance(snapshot["$schema"], str):
                raise ValueError("Schema dialect must be a URI string")
            validator_type = (
                validator_for(snapshot, default=None) if "$schema" in snapshot
                else Draft202012Validator
            )
            if validator_type is None:
                raise ValueError("Unknown schema dialect")
            validator_type.check_schema(snapshot)
            validator = validator_type(snapshot, registry=Registry(retrieve=_no_external_resource))
        except (SchemaError, ValueError, TypeError, RecursionError):
            return GenerationResult(False, None, GenerationError(
                ErrorKind.INVALID_SCHEMA, "Invalid or unsupported JSON Schema."
            ), ())

        executable = shutil.which(self.codex_command)
        if executable is None:
            return GenerationResult(False, None, GenerationError(
                ErrorKind.CLI_NOT_FOUND, "Codex executable was not found."
            ), ())

        records: list[AttemptRecord] = []
        for number in range(1, max_retries + 2):
            started = time.monotonic()
            data, error, returncode = self._attempt(
                executable, prompt, schema_text, validator, timeout, model,
            )
            record = AttemptRecord(number, time.monotonic() - started, returncode, error)
            records.append(record)
            _LOGGER.info(
                "codex attempt=%d status=%s returncode=%s elapsed_seconds=%.3f",
                number, error.kind.value if error else "success", returncode,
                record.elapsed_seconds,
            )
            if error is None:
                return GenerationResult(True, data, None, tuple(records))
            if error.kind not in selected or number > max_retries:
                return GenerationResult(False, None, error, tuple(records))
            if retry_delay:
                time.sleep(retry_delay)
        raise AssertionError("Unreachable attempt limit")

    @staticmethod
    def _check_arguments(
        prompt: str, timeout: float, max_retries: int,
        retry_delay: float, model: str | None,
    ) -> None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        try:
            prompt.encode("utf-8")
        except UnicodeError:
            raise ValueError("prompt must be valid UTF-8 text") from None
        for value, name, allow_zero in (
            (timeout, "timeout", False), (retry_delay, "retry_delay", True),
        ):
            if (
                isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0)
            ):
                raise ValueError(f"{name} must be finite and {'non-negative' if allow_zero else 'positive'}")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if model is not None and (
            not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", model)
            or model.startswith("-")
        ):
            raise ValueError("model must be a model identifier")

    @staticmethod
    def _command(executable: str, model: str | None) -> str | list[str]:
        args = [
            executable, "--no-daemon", "--ask-for-approval", "never", "exec",
            "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ephemeral", "--color", "never",
            "--output-schema", "schema.json", "--output-last-message", "response.json",
        ]
        if model is not None:
            args.extend(["--model", model])
        args.append("-")
        if not _IS_WINDOWS:
            return args
        # .cmd is a batch file: invoke cmd.exe explicitly without PowerShell or
        # shell=True. Quote EVERY argument; list2cmdline alone cannot quote cmd
        # metacharacters safely. Prompt content never enters this command line.
        if any(any(char in arg for char in '\x00\r\n"%!') for arg in args):
            raise ValueError("Unsupported character in Windows executable path")
        cmd_exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
        prefix = subprocess.list2cmdline([cmd_exe, "/d", "/s", "/v:off", "/c"])
        return prefix + ' "' + " ".join('"' + arg + '"' for arg in args) + '"'

    def _attempt(
        self, executable: str, prompt: str, schema_text: str,
        validator: Any, timeout: float, model: str | None,
    ) -> tuple[Any, GenerationError | None, int | None]:
        returncode = None
        try:
            command = self._command(executable, model)
            with tempfile.TemporaryDirectory(prefix="kg-codex-", dir=self.temp_dir) as folder:
                work = Path(folder)
                (work / "schema.json").write_text(schema_text, encoding="utf-8")
                process = subprocess.Popen(
                    command, cwd=work, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="strict", shell=False,
                    env={key: value for key, value in os.environ.items()
                         if key.upper() not in {"OPENAI_API_KEY", "CODEX_API_KEY"}},
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WINDOWS
                       else {"start_new_session": True}),
                )
                try:
                    process.communicate(input=prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    cleaned = self._stop_process_tree(process)
                    returncode = process.returncode
                    if not cleaned:
                        return None, GenerationError(
                            ErrorKind.PROCESS_CLEANUP,
                            "Could not confirm process tree termination; retries stopped.",
                        ), returncode
                    return None, GenerationError(
                        ErrorKind.TIMEOUT, "Codex exceeded the per-attempt timeout."
                    ), returncode
                except BaseException:
                    # Interruptions must also stop children before temp file cleanup.
                    self._stop_process_tree(process)
                    raise
                returncode = process.returncode
                if returncode != 0:
                    return None, GenerationError(
                        ErrorKind.CLI_EXECUTION, "Codex exited unsuccessfully."
                    ), returncode
                response_path = work / "response.json"
                if not response_path.is_file():
                    return None, GenerationError(
                        ErrorKind.INVALID_JSON, "Codex did not write a final JSON message."
                    ), returncode
                with response_path.open("rb") as response:
                    raw = response.read(self.max_output_bytes + 1)
                if len(raw) > self.max_output_bytes:
                    return None, GenerationError(
                        ErrorKind.OUTPUT_LIMIT, "Final message exceeded max_output_bytes."
                    ), returncode
                try:
                    data = _strict_loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    return None, GenerationError(
                        ErrorKind.INVALID_JSON, "Final message is not strict UTF-8 JSON."
                    ), returncode
                try:
                    validator.validate(data)
                except ValidationError as exc:
                    return None, GenerationError(
                        ErrorKind.SCHEMA_VALIDATION,
                        f"Final JSON failed Schema validation ({exc.validator}).",
                    ), returncode
                except (Unresolvable, SchemaError, RecursionError):
                    return None, GenerationError(
                        ErrorKind.INVALID_SCHEMA,
                        "Schema reference could not be resolved locally.",
                    ), returncode
                return data, None, returncode
        except FileNotFoundError:
            return None, GenerationError(
                ErrorKind.IO_ERROR, "Executable or temporary path became unavailable."
            ), returncode
        except (OSError, ValueError):
            # Includes temporary-file cleanup failure: never return success then.
            return None, GenerationError(
                ErrorKind.IO_ERROR, "Local process or temporary-file operation failed."
            ), returncode

    @staticmethod
    def _stop_process_tree(process: subprocess.Popen[str]) -> bool:
        """Bounded cleanup. Failure is terminal, even when retries were requested."""
        confirmed = False
        try:
            if _IS_WINDOWS:
                taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
                killed = subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, shell=False, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                confirmed = killed.returncode == 0
            else:
                os.killpg(process.pid, signal.SIGKILL)
                confirmed = True
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if not confirmed:
                process.kill()
            process.communicate(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return confirmed
