"""Deterministic, offline summaries. Citation validity is not semantic support."""

from __future__ import annotations

import html
import json

from overfit.evaluation.citations import check_citation

CHECKER_VERSION = "1"


def summarize(manifest: dict, events: list[dict]) -> dict:
    """Recompute checks from recorded inputs, never trust stored grader results."""
    def payloads(kind):
        return [e["payload"] for e in events if e["event_type"] == kind]
    materials = payloads("materials_selected")
    chunks = materials[0]["chunks"] if materials else []
    starts, finishes = payloads("call_started"), payloads("call_finished")
    finished_ids = {c["call_id"] for c in finishes}
    unresolved = [c["call_id"] for c in starts if c["call_id"] not in finished_ids]
    validations = payloads("validation_finished")
    valid = [v for v in validations if v["status"] == "valid"]
    raw_count = valid[-1]["item_count"] if valid else None
    items = []
    differences = []
    for row in payloads("citation_processed"):
        pre = check_citation(row["pre_policy"], chunks)
        post = (
            check_citation(row["post_policy"], chunks)
            if row["post_policy"] is not None else None
        )
        for name, current in (("pre_check", pre), ("post_check", post)):
            if row.get(name) != current:
                differences.append({"item_id": row["item_id"], "field": name,
                                    "stored": row.get(name), "recomputed": current})
        items.append({**row, "pre_check": pre, "post_check": post,
                      "semantic_status": "not_evaluated"})
    all_processed = raw_count is not None and len(items) == raw_count
    retained = sum(i["post_policy"] is not None for i in items)
    outcome = None
    if not starts:
        outcome = "not_started"
    elif all_processed:
        outcome = "model_empty" if raw_count == 0 else (
            "retained" if retained else "all_dropped"
        )
    elif not unresolved and not valid and any(
        terminal.get("generation_outcome") == "failed"
        for terminal in payloads("run_finished")
    ):
        outcome = "failed"
    pre_valid = sum(i["pre_check"]["status"] == "valid" for i in items)
    post_valid = sum(i["post_check"] is not None and i["post_check"]["status"] == "valid"
                     for i in items)
    return {
        "schema_version": 1, "checker_version": CHECKER_VERSION,
        "run_id": manifest["run_id"], "semantic_status": "not_evaluated",
        "requested_questions": manifest["task"]["questions"],
        "material_count": len(chunks) if materials else None,
        "material_identity": ({"materials_sha256": materials[0]["materials_sha256"],
                               "index": materials[0]["index"]} if materials else None),
        "call_starts": len(starts), "call_finishes": len(finishes),
        "unresolved_calls": unresolved,
        "call_count": None if unresolved else len(finishes),
        "attempt_count": max((c["attempt_index"] for c in starts), default=0),
        "format_fallbacks": sum(c["format_index"] > 1 for c in starts),
        "provider_errors": sum(c["status"] == "error" for c in finishes),
        "validation_errors": sum(v["status"] == "invalid" for v in validations),
        "empty_responses": sum(v["status"] == "empty_body" for v in validations),
        "generation_outcome": outcome, "raw_item_count": raw_count,
        "processed_item_count": len(items),
        "retained_count": retained if all_processed else None,
        "repaired_count": sum(i["action"] == "repair" for i in items),
        "dropped_count": sum(i["action"] == "drop" for i in items),
        "pre_citation_valid": pre_valid, "post_citation_valid": post_valid,
        "pre_citation_rate": pre_valid / raw_count if all_processed and raw_count else None,
        "post_citation_rate": post_valid / retained if all_processed and retained else None,
        "items": items, "check_differences": differences,
    }


def render_report(result: dict) -> str:
    """Render only escaped/indented untrusted data; no model content as headings."""
    lines = ["# Generation trace report", "",
             "Semantic support and answerability: **not evaluated**.", "",
             "Integrity is verified from the event log and artifact hashes, not this file.",
             "A recorded terminal status does not prove the original CLI exited successfully.",
             "Counts describe accepted events only; an incomplete log may omit later activity.", ""]
    for field in (
        "run_id", "run_status", "trace_integrity", "count_scope", "failure_stage",
        "requested_questions", "material_count",
        "generation_outcome", "call_starts", "call_finishes", "call_count", "attempt_count",
        "format_fallbacks", "provider_errors", "validation_errors", "empty_responses",
        "raw_item_count", "processed_item_count", "retained_count", "repaired_count",
        "dropped_count", "pre_citation_rate", "post_citation_rate",
    ):
        value = result.get(field)
        # No free text is emitted as Markdown syntax.
        display = "N/A (unknown or zero denominator)" if value is None else str(value)
        lines += [f"## {field}", ""]
        lines.extend("    " + html.escape(line) for line in display.splitlines())
        lines.append("")
    for field in ("error", "material_identity", "problems", "unresolved_calls",
                  "check_differences", "items"):
        lines += [f"## {field}", ""]
        data = json.dumps(result.get(field, []), ensure_ascii=False, indent=2)
        lines.extend("    " + html.escape(line) for line in data.splitlines())
        lines.append("")
    return "\n".join(lines)
