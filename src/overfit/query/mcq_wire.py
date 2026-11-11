"""Pure, reusable JSON parsing and mechanical normalization for mcq-v1."""

from __future__ import annotations

import json
import re
from copy import deepcopy

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_PAGE_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def strict_json_loads(text: str):
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def extract_json(raw: str) -> dict:
    """Extract one strict JSON object while tolerating common prose fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE
        ).strip()
    try:
        value = strict_json_loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise ValueError("no JSON object in reply") from None
        value = strict_json_loads(match.group())
    if not isinstance(value, dict):
        raise TypeError("response must contain one JSON object")
    return value


def normalize_mcq_wire(data: object) -> dict:
    """Apply only the mechanical transformations frozen by mcq-v1."""
    if not isinstance(data, dict):
        raise TypeError("MCQ response must be one JSON object")
    if set(data) != {"questions"}:
        raise ValueError("MCQ response must contain only the top-level questions field")
    questions = data["questions"]
    if not isinstance(questions, list):
        raise TypeError("questions must be an array")

    normalized: list[object] = []
    for question in questions:
        if not isinstance(question, dict):
            normalized.append(question)
            continue
        current = deepcopy(question)
        for field in ("topic", "question"):
            if isinstance(current.get(field), str):
                current[field] = current[field].strip()

        correct = current.get("correct_option")
        if isinstance(correct, str):
            current["correct_option"] = correct.strip().upper()

        options = current.get("options")
        if isinstance(options, dict):
            canonical: dict[str, object] = {}
            for raw_label, raw_option in options.items():
                label = (
                    raw_label.strip().upper()
                    if isinstance(raw_label, str)
                    else raw_label
                )
                if label in canonical:
                    raise ValueError(
                        f"duplicate option label after normalization: {label}"
                    )
                if not isinstance(raw_option, dict):
                    canonical[label] = raw_option
                    continue
                option = deepcopy(raw_option)
                for field in ("text", "explanation", "source"):
                    if isinstance(option.get(field), str):
                        option[field] = option[field].strip()
                text = option.get("text")
                if isinstance(label, str) and isinstance(text, str):
                    option["text"] = re.sub(
                        rf"^{re.escape(label)}\.\s*",
                        "",
                        text,
                        count=1,
                        flags=re.IGNORECASE,
                    ).strip()
                normalize_option_pages(option)
                canonical[label] = option
            current["options"] = canonical
        normalized.append(current)
    return {"items": normalized}


def normalize_option_pages(option: dict) -> None:
    page = option.get("page")
    explicit_end = option.get("page_end")

    range_end = None
    if isinstance(page, str):
        value = page.strip()
        if value.isdigit():
            page = int(value)
        else:
            match = _PAGE_RANGE.fullmatch(value)
            if match is None:
                raise ValueError(f"invalid page value: {page!r}")
            page, range_end = (int(part) for part in match.groups())

    if isinstance(explicit_end, str):
        value = explicit_end.strip()
        if not value.isdigit():
            raise ValueError(f"invalid page_end value: {explicit_end!r}")
        explicit_end = int(value)

    if range_end is not None:
        if explicit_end is not None and explicit_end != range_end:
            raise ValueError("page range conflicts with explicit page_end")
        explicit_end = range_end

    option["page"] = page
    option["page_end"] = explicit_end
