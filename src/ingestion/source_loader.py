"""Load UTF-8 Markdown/text without newline, whitespace or BOM normalization."""

from dataclasses import dataclass
import hashlib
from pathlib import Path


class SourceLoadError(ValueError):
    """The source cannot be loaded losslessly in a supported format."""


@dataclass(frozen=True)
class SourceDocument:
    path: str
    document_type: str
    sha256: str
    size_bytes: int
    original_text: str

    def metadata(self) -> dict:
        return {
            "path": self.path,
            "document_type": self.document_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "char_count": len(self.original_text),
            "encoding": "utf-8",
        }


def load_source(path: str | Path) -> SourceDocument:
    """Read exactly once as bytes. Never write to or normalize the source."""
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise SourceLoadError("輸入必須是一般檔案。")
    extension = source.suffix.casefold()
    if extension not in {".md", ".txt"}:
        raise SourceLoadError("Step 0 只支援 .md 與 .txt。")
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SourceLoadError("來源不是有效 UTF-8；請另存轉碼副本，勿覆寫原始來源。") from None
    return SourceDocument(
        str(source), extension[1:], hashlib.sha256(raw).hexdigest(), len(raw), text,
    )
