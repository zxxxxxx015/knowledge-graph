"""Read-only source ingestion, independent of pipeline and LLM logic."""

from .source_loader import SourceDocument, SourceLoadError, load_source

__all__ = ["SourceDocument", "SourceLoadError", "load_source"]
