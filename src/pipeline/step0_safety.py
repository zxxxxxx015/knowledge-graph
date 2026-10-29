"""Deterministic span, Markdown, boundary and reconstruction guards for Step 0.

These checks never infer an antecedent or repair a model's offsets.
"""

import re
from typing import Any


class ResultIntegrityError(ValueError):
    """A result cannot be reproduced or violates preservation invariants."""


def valid_range(span: dict, length: int) -> bool:
    return (
        isinstance(span, dict) and set(span) == {"start", "end"}
        and type(span["start"]) is int and type(span["end"]) is int
        and 0 <= span["start"] < span["end"] <= length
    )


def overlaps(left: dict, right: dict) -> bool:
    return left["start"] < right["end"] and right["start"] < left["end"]


def line_ranges(text: str) -> list[dict]:
    return [
        {"start": match.start(), "end": match.end()}
        for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text)
        if match.end() > match.start()
    ]


def protected_spans(text: str, document_type: str) -> list[tuple[int, int, str]]:
    """Conservatively protect syntax and non-prose; this is not a Markdown renderer."""
    protected: list[tuple[int, int, str]] = []

    def add(start: int, end: int, code: str = "protected_content") -> None:
        if end > start:
            protected.append((start, end, code))

    fence: tuple[str, int, int] | None = None
    definition_level: int | None = None
    lines = line_ranges(text)
    for span in lines:
        start, end = span["start"], span["end"]
        line = text[start:end].rstrip("\r\n")
        marker = re.search(r"(?:^|[ \t>])(`{3,}|~{3,})", line)
        if fence is not None:
            closing = r"[ \t>]*(?:(?:[-+*]|\d+[.)])[ \t]+)?" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*"
            if re.fullmatch(closing, line):
                add(fence[2], end)
                fence = None
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]), start)
            continue
        heading = re.match(r"^\ufeff?[ \t>]*(#{1,6})[ \t]+(.*)", line)
        if heading:
            level, title = len(heading[1]), heading[2]
            if definition_level is not None and level <= definition_level:
                definition_level = None
            if re.search(r"定義|術語|名詞解釋|\b(?:definitions?|glossary|terminology)\b", title, re.I):
                definition_level = level
            if document_type == "md":
                add(start, end, "markdown_heading")
        if definition_level is not None:
            add(start, end, "generic_definition")
        if document_type == "md":
            if line.startswith(("    ", "\t")):
                add(start, end)
            prefix = re.match(r"[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])[ \t]+)?(?:\[[ xX]\][ \t]+)?", line)
            if prefix:
                add(start, start + prefix.end(), "markdown_structure")
            if re.fullmatch(r"\s*(?:\|?\s*:?-{3,}:?\s*)+\|?\s*", line):
                add(start, end, "markdown_structure")
            # Setext headings and their preceding title line remain intact.
            if re.fullmatch(r"[ \t]*(?:=+|-+)[ \t]*", line):
                add(start, end, "markdown_heading")
                previous = next((p for p in reversed(lines) if p["end"] == start), None)
                if previous:
                    add(previous["start"], previous["end"], "markdown_heading")
            if re.match(r"^\s*\[[^\]]+\]:", line):
                add(start, end)
    if fence is not None:
        add(fence[2], len(text))

    # Definitions and document self-reference are protected for both md and txt.
    for sentence in re.finditer(r"[^。！？!?\r\n]+[。！？!?]?", text):
        if re.search(r"是指|意指|定義(?:為|是|[:：])|\b(?:is defined as|refers to|means)\b", sentence[0], re.I):
            add(sentence.start(), sentence.end(), "generic_definition")
    self_reference = (
        r"本文|本報告|本文件|本章節|本手冊|本測試文件|"
        r"(?:此|該|這份|這篇|這個)(?:測試)?(?:文件|報告|文章|手冊|章節)|"
        r"\bthis\s+(?:document|report|paper|manual|chapter)\b"
    )
    for match in re.finditer(self_reference, text, re.I):
        add(match.start(), match.end(), "document_self_reference")

    patterns = [
        r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)",
        r"(?i)\b(?:https?|ftp|file)://[^\s<>]+|\bwww\.[^\s<>]+",
        r"(?:[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/]|/)[^\s<>\"'，。；]+",
        r"[^\s<>\"'，。；()]+[\\/][^\s<>\"'，。；()]+",
        r"(?i)[\w.\-\u3400-\u9fff]+\.(?:md|txt|json|py|pdf|csv|yaml|yml|toml|exe|cmd|ps1|log|ini|conf)\b",
        r"(?<!\w)[\w.-]*_[\w.-]+",
    ]
    if document_type == "md":
        patterns.extend([
            r"!?\[[^\]\r\n]*\]\([^\r\n]*?\)",
            r"!?\[[^\]\r\n]*\]\s*\[[^\]\r\n]*\]",
            r"\[[^\]\r\n]*\]",  # Also protect shortcut references and footnotes.
            r"<([A-Za-z][A-Za-z0-9-]*)\b[^>]*>[\s\S]*?(?:</\1\s*>|$)",
            r"<!--[\s\S]*?(?:-->|$)|<[^>\r\n]*>",
            r"\$\$[\s\S]*?\$\$|(?<!\$)\$[^$\r\n]+\$(?!\$)",
            r"\\[^\r\n]",  # Escaped syntax stays byte-for-byte equivalent.
        ])
        front = re.match(r"\ufeff?---(?:\r\n|\r|\n)[\s\S]*?(?:\r\n|\r|\n)(?:---|\.\.\.)(?=\r|\n|$)", text)
        if front:
            add(front.start(), front.end())
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            add(match.start(), match.end())
    return sorted(protected)


def structure_signature(text: str, document_type: str) -> tuple:
    """Offsets may move, but line endings, prefixes and markup tokens may not."""
    newlines = tuple(re.findall(r"\r\n|\r|\n", text))
    if document_type != "md":
        return (newlines,)
    lines = []
    for span in line_ranges(text):
        line = text[span["start"]:span["end"]].rstrip("\r\n")
        prefix = re.match(r"[ \t]*(?:>[ \t]*)*(?:(?:#{1,6}|[-+*]|\d+[.)])[ \t]+)?(?:\[[ xX]\][ \t]+)?", line)[0]
        tokens = tuple(re.findall(r"[`~*_\[\](){}<>|\\]", line))
        lines.append((prefix, tokens, not bool(line.strip())))
    return newlines, tuple(lines)


def overlap_indices(candidates: list[dict], length: int) -> set[int]:
    spans = sorted(
        (item["source_range"]["start"], item["source_range"]["end"], index)
        for index, item in enumerate(candidates)
        if valid_range(item["source_range"], length)
    )
    conflicts: set[int] = set()
    for position, (start, end, index) in enumerate(spans):
        for other_start, _, other_index in spans[position + 1:]:
            if other_start >= end:
                break
            conflicts.update((index, other_index))
    return conflicts


def _char_kind(char: str) -> str:
    if char and re.fullmatch(r"[A-Za-z]", char):
        return "latin"
    if char and re.fullmatch(r"[\u3400-\u9fff\U00020000-\U0003134f]", char):
        return "han"
    return "other"


def unsafe_boundary(text: str, span: dict, replacement: str) -> bool:
    start, end = span["start"], span["end"]
    original = text[start:end]
    left, right = text[start - 1:start] if start else "", text[end:end + 1]
    pairs = ((left, original[0], replacement[0]), (right, original[-1], replacement[-1]))
    for neighbor, old, new in pairs:
        old_pair = (_char_kind(neighbor), _char_kind(old))
        new_pair = (_char_kind(neighbor), _char_kind(new))
        if set(new_pair) == {"han", "latin"} and old_pair != new_pair:
            return True
        # Do not replace a substring inside an English identifier/word.
        if neighbor and neighbor.isascii() and (neighbor.isalnum() or neighbor == "_"):
            if old.isascii() and (old.isalnum() or old == "_"):
                return True
            if new.isascii() and (new.isalnum() or new == "_"):
                return True
    return False


def validate_candidate(
    text: str, candidate: dict, index: int,
    conflicts: set[int], protections: list[tuple[int, int, str]],
) -> tuple[str, str] | None:
    span, evidence = candidate["source_range"], candidate["evidence_range"]
    original, replacement = candidate["original_text"], candidate["replacement_text"]
    if not valid_range(span, len(text)):
        return "invalid_source_range", "候選原文範圍越界、倒置或為空；不搜尋修正。"
    if text[span["start"]:span["end"]] != original:
        return "source_text_mismatch", "候選文字與指定原文位置不一致；不搜尋修正。"
    if index in conflicts:
        return "overlapping_candidates", "候選與其他候選重疊，所有互相重疊的候選均拒絕。"
    if not valid_range(evidence, len(text)):
        return "invalid_evidence_range", "證據範圍越界、倒置或為空。"
    if text[evidence["start"]:evidence["end"]] != candidate["evidence_text"]:
        return "evidence_text_mismatch", "證據與指定的原文範圍不一致。"
    if candidate["context"] != "specific_reference":
        return candidate["context"], "通用定義或文件自稱不允許替換。"
    if not candidate["is_unique"]:
        return "ambiguous_reference", "語意判斷未能唯一確認指涉。"
    for start, end, code in protections:
        if span["start"] < end and start < span["end"]:
            return code, "候選位於受保護的定義、文件自稱、Markdown 或非自然語言內容。"
    if original == replacement:
        return "no_change", "原文與替換文字相同，無需替換。"
    if any(char.isdigit() for char in original):
        return "numeric_content", "候選包含數字，保守保留原始數值及識別文字。"
    if re.search(
        r"不會|不能|不應|可能|必須|需要|應該|如果|當|則|會|進行|執行|停止|啟動|提高|降低|超過|低於|高於|增加|減少|造成|導致|因為|因此|由於|若|"
        r"\b(?:will|must|should|can|may|if|when|stop|start|increase|decrease|cause)\b",
        original + " " + replacement, re.I,
    ):
        return "non_reference_edit", "候選含常見動作、條件或情態敘述，保守拒絕知識改寫。"
    if (
        len(original) > 40 or len(replacement) > 120
        or original != original.strip() or replacement != replacement.strip()
        or replacement.startswith("-")
        or any(not (char.isalpha() or char.isdigit() or char in " -")
               for char in original + replacement)
    ):
        return "non_reference_edit", "候選超出短名稱範圍，或包含標點、語法、換行或非名稱字元。"
    if candidate["reference_type"] == "pronoun":
        if original.casefold() not in {
            "其", "它", "它們", "他", "他們", "她", "她們", "此", "該", "這", "那",
            "這個", "那個", "前者", "後者", "it", "its", "they", "them", "their",
            "this", "that", "these", "those", "he", "she", "his", "her",
        }:
            return "non_reference_edit", "候選不在保守的代名詞集合中。"
    elif candidate["reference_type"] == "elliptical_name":
        if original not in replacement:
            return "term_changed", "省略性名稱必須逐字保留於完整名稱，不能改成其他技術術語。"
    else:
        noun = re.sub(r"^(?:該項|該款|這個|這項|這些|上述|前述|此|該|本|this |that |these |those )", "", original, count=1, flags=re.I)
        if noun == original or not noun or noun not in replacement:
            return "term_changed", "指示性簡稱只能以有證據的完整名稱擴充原有名詞。"
    # Require an exact antecedent occurrence outside the target. This does not
    # relocate either model-provided range or use fuzzy matching.
    if not any(
        not overlaps(span, {
            "start": evidence["start"] + match.start(),
            "end": evidence["start"] + match.end(),
        })
        for match in re.finditer(re.escape(replacement), candidate["evidence_text"])
    ):
        return "unsupported_replacement", "證據中沒有待替換處以外逐字出現的完整名稱。"
    if unsafe_boundary(text, span, replacement):
        return "unsafe_boundary", "替換會產生未確認的中英文邊界或黏合英文識別字；不自行加空格。"
    return None


def apply_candidates(text: str, candidates: list[dict]) -> tuple[str, list[dict]]:
    """Construct by unchanged slices; processed offsets are calculated in Python."""
    chunks, replacements = [], []
    source_cursor = processed_cursor = 0
    for candidate in sorted(candidates, key=lambda item: item["source_range"]["start"]):
        start, end = candidate["source_range"]["start"], candidate["source_range"]["end"]
        untouched = text[source_cursor:start]
        chunks.append(untouched)
        processed_cursor += len(untouched)
        replacement = candidate["replacement_text"]
        replacements.append({
            key: candidate[key] for key in (
                "original_text", "replacement_text", "source_range", "evidence_text",
                "evidence_range", "reference_type", "reason",
            )
        } | {"processed_range": {"start": processed_cursor, "end": processed_cursor + len(replacement)}})
        chunks.append(replacement)
        processed_cursor += len(replacement)
        source_cursor = end
    chunks.append(text[source_cursor:])
    return "".join(chunks), replacements


def reconstruct_processed_text(original: str, replacements: list[dict]) -> str:
    """Verify every stored coordinate and reconstruct without consulting an LLM."""
    cursor = processed_cursor = 0
    chunks = []
    for item in replacements:
        span, evidence = item["source_range"], item["evidence_range"]
        if not valid_range(span, len(original)) or span["start"] < cursor:
            raise ResultIntegrityError("替換範圍無效、未排序或重疊。")
        if original[span["start"]:span["end"]] != item["original_text"]:
            raise ResultIntegrityError("替換原文與來源不一致。")
        if not valid_range(evidence, len(original)) or original[evidence["start"]:evidence["end"]] != item["evidence_text"]:
            raise ResultIntegrityError("替換證據與來源不一致。")
        untouched = original[cursor:span["start"]]
        processed_cursor += len(untouched)
        end = processed_cursor + len(item["replacement_text"])
        if item["processed_range"] != {"start": processed_cursor, "end": end}:
            raise ResultIntegrityError("處理後範圍與計算結果不一致。")
        chunks.extend((untouched, item["replacement_text"]))
        cursor, processed_cursor = span["end"], end
    chunks.append(original[cursor:])
    return "".join(chunks)
