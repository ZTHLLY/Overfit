"""Independent model labels, technical coverage and human routing; entirely offline."""

from copy import deepcopy

import pytest

from overfit.evaluation.metrics import (
    build_review_queue,
    render_judge_report,
    summarize_judgments,
)


def evidence():
    return {"chunk_id": "chunk-1", "quote": "Training error is low", "start": 0, "end": 21}


def row(identity="q1"):
    item = {"question": "What is overfitting?", "answer": "Low training error",
            "source": "week1.pdf", "page": 1}
    phases = {
        "answerability": {"verdict": "answerable", "reason": "Enough context", "evidence": [evidence()]},
        "support": {"material_support": "supported", "citation_support": "supported",
                    "reason": "All necessary assertions are supported", "claims": [{
                        "text": "Training error is low", "verdict": "supported", "evidence": [evidence()]}],
                    "citation_evidence": [evidence()]},
    }
    return {"item_id": identity, "pre_policy": deepcopy(item), "post_policy": item,
            "pre_check": {"status": "valid"}, "post_check": {"status": "valid"},
            **{phase: {"status": "evaluated", "technical_status": "completed", "reason": None,
                       "model_review": review, "human_review": {"status": "not_required", "reasons": []},
                       "verification": {"structure": "valid", "consistency": "valid", "evidence": "valid",
                                        "diagnostics": [], "evidence_records": [evidence()]}}
               for phase, review in phases.items()}}


def source(count=1, requested=3, dropped=0):
    return {"run_id": "source-1", "requested_questions": requested,
            "retained_count": count, "raw_item_count": count + dropped, "dropped_count": dropped}


def state(status, reason="test"):
    return {"status": status, "technical_status": status, "reason": reason, "model_review": None,
            "human_review": {"status": "not_evaluated", "reasons": []}}


def summarize(rows, calls=None, requested=3, mode="execute", max_items=None):
    count = sum(r["post_policy"] is not None for r in rows)
    return summarize_judgments(source(count, requested, len(rows) - count), rows, calls or [], mode,
                               max_items=max_items)


def call(call_id="c1", attempt=1, usage=None):
    return [
        {"type": "call_started", "call_id": call_id, "item_id": "q1",
         "phase": "support", "attempt": attempt, "request": {}},
        {"type": "call_finished", "call_id": call_id, "status": "response",
         "response": {"content": "{}", "usage": usage}},
    ]


def test_full_model_review_three_labels_without_automatic_approval():
    result = summarize([row()])
    assert result["schema_version"] == 3
    assert result["model_all_positive_count"] == result["complete_model_review_item_count"] == 1
    assert result["evaluated_phase_count"] == result["planned_phase_count"] == 2
    assert result["selected_evaluated_phase_count"] == result["selected_phase_count"] == 2
    assert result["all_retained_evaluated"]
    assert result["dimension_counts"]["answerability"]["answerable"] == 1
    assert result["dimension_counts"]["material_support"]["supported"] == 1
    assert result["items"] == [{"item_id": "q1", "model_all_positive": True,
                                "model_review_complete": True, "human_review_status": "not_required"}]
    assert result["calibration_status"] == "not_calibrated"
    assert not any("confirmed" in key or "pass" in key for key in result)
    assert "outcome_counts" not in result


@pytest.mark.parametrize("dimension,verdict", [
    ("answerability", "not_answerable"), ("answerability", "unknown"),
    ("material_support", "partially_supported"), ("material_support", "unsupported"),
    ("citation_support", "partially_supported"), ("citation_support", "unsupported"),
    ("citation_support", "unknown"),
])
def test_content_verdicts_count_as_completed_not_technical_failure(dimension, verdict):
    sample = row()
    phase = "answerability" if dimension == "answerability" else "support"
    key = "verdict" if dimension == "answerability" else dimension
    sample[phase]["model_review"][key] = verdict
    result = summarize([sample])
    assert result["model_all_positive_count"] == 0
    assert result["dimension_counts"][dimension][verdict] == 1
    assert result["evaluated_phase_count"] == 2
    assert result["technical_error_phase_count"] == 0


def test_error_unreviewed_unknown_and_negative_all_remain_in_denominator():
    samples = [row(str(index)) for index in range(5)]
    samples[1]["answerability"] = state("error", "schema_error")
    samples[2]["support"] = state("not_evaluated", "budget_exhausted")
    samples[3]["support"]["model_review"]["citation_support"] = "unknown"
    samples[4]["answerability"]["model_review"]["verdict"] = "not_answerable"
    result = summarize(samples, requested=5)
    assert result["model_all_positive_count"] == 1
    assert result["complete_model_review_item_count"] == 3
    assert result["phase_counts"]["answerability"]["error"] == 1
    assert result["dimension_counts"]["material_support"]["not_evaluated"] == 1
    assert result["evaluated_phase_count"] == 8
    assert result["planned_phase_count"] == 10
    assert not result["all_retained_evaluated"]


def test_selected_coverage_is_independent_from_all_retained():
    samples = [row(str(n)) for n in range(3)]
    for sample in samples[1:]:
        sample["answerability"] = sample["support"] = state("not_evaluated", "max_items")
    result = summarize(samples, max_items=1)
    assert result["selected_item_count"] == 1
    assert result["selected_evaluated_phase_count"] == result["selected_phase_count"] == 2
    assert result["evaluated_phase_count"] == 2 and result["planned_phase_count"] == 6
    assert not result["all_retained_evaluated"]


def test_strict_citation_metadata_never_rewrites_raw_model_verdict():
    sample = row()
    sample["post_check"]["status"] = "invalid_page"
    assert summarize([sample])["model_all_positive_count"] == 1


def test_empty_and_excess_generation():
    empty = summarize([])
    assert empty["model_all_positive_count"] == 0
    assert empty["complete_model_review_item_count"] == 0
    assert not empty["all_retained_evaluated"]
    excess = summarize([row(str(n)) for n in range(5)], requested=3)
    assert excess["model_all_positive_count"] == 5
    assert excess["excess_retained_count"] == 2


def test_drop_and_pre_policy_explicitly_not_evaluated():
    dropped = row("drop")
    dropped["post_policy"] = dropped["post_check"] = None
    dropped["answerability"] = dropped["support"] = state("not_evaluated", "product_dropped")
    result = summarize([row(), dropped])
    assert result["retained_count"] == 1
    assert result["raw_item_count"] == 2 and result["dropped_count"] == 1
    assert result["pre_policy_semantic_status"] == result["dropped_semantic_status"] == "not_evaluated"
    assert len(result["items"]) == 1
    assert "产品已丢弃" in render_judge_report(result, [row(), dropped])


def test_prepare_does_not_call_zero_a_failure():
    sample = row()
    sample["answerability"] = sample["support"] = state("not_evaluated", "prepare_only")
    result = summarize([sample], mode="prepare")
    assert result["semantic_status"] == "not_evaluated"
    assert result["model_all_positive_count"] == 0
    report = render_judge_report(result, [sample])
    assert "没有调用 judge" in report and "不表示题目全部错误" in report
    assert result["usage"]["total_tokens"] is None and result["cost"] is None


def test_calls_count_errors_retries_and_responses_separately():
    calls = call(usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
    calls += call("c2", attempt=2)
    calls[-1]["status"] = "error"
    result = summarize([row()], calls)
    assert result["call_count"] == result["call_finishes"] == result["call_starts"] == 2
    assert result["response_count"] == 1 and result["retry_count"] == 1
    assert result["usage"]["total_tokens"] is None and result["cost"] is None


def test_usage_allowlist_aggregated_only_when_known_for_all_calls():
    usage = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7,
             "secret": "must-not-copy", "completion_tokens_details": {"reasoning_tokens": 8}}
    result = summarize([row()], call(usage=usage) + call("c2", usage=usage))
    assert result["usage"] == {"prompt_tokens": 6, "completion_tokens": 8, "total_tokens": 14}
    assert "secret" not in str(result)


@pytest.mark.parametrize("bad", [True, -1, 1.5, "8"])
def test_invalid_usage_is_unknown(bad):
    assert summarize([row()], call(usage={"total_tokens": bad}))["usage"]["total_tokens"] is None


def test_unresolved_intent_does_not_claim_actual_call_count_or_complete_usage():
    calls = call(usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
    calls += call("orphan", attempt=2)[:1]
    result = summarize([row()], calls)
    assert result["call_count"] is None
    assert result["call_finishes"] == 1 and result["call_starts"] == 2
    assert result["unresolved_calls"] == ["orphan"] and result["usage"]["total_tokens"] is None


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_invalid_requested_count_rejected(bad):
    with pytest.raises(ValueError, match="positive integer"):
        summarize_judgments(source(requested=bad), [row()], [], "execute")


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_invalid_selected_limit_rejected(bad):
    with pytest.raises(ValueError, match="positive integer"):
        summarize([row()], max_items=bad)


def test_incomplete_denominator_duplicate_identity_and_fake_prepare_rejected():
    with pytest.raises(ValueError, match="every retained"):
        summarize_judgments(source(2), [row()], [], "execute")
    with pytest.raises(ValueError, match="unique"):
        summarize([row(), row()])
    with pytest.raises(ValueError, match="Prepare"):
        summarize([row()], mode="prepare")


def test_readable_report_escapes_all_untrusted_text_as_indented_lines():
    poison = '# forged heading\n<script>alert("x")</script>\n[click](https://bad.test)\n```'
    sample = row(poison)
    for field in ("question", "answer", "source"):
        sample["post_policy"][field] = poison
    sample["answerability"]["model_review"]["reason"] = poison
    sample["answerability"]["verification"]["evidence_records"][0]["model_quote"] = poison
    sample["support"]["model_review"]["claims"][0]["text"] = poison
    metrics = summarize([sample])
    metrics["source_run_id"] = poison
    report = render_judge_report(metrics, [sample])
    for line in report.splitlines():
        if any(token in line for token in ("forged heading", "<script>", "[click]", "```")):
            assert line.startswith("    ")
    assert '&quot;' not in report
    assert "## 汇总" in report and "### 题目 1" in report
    assert '"post_policy":' not in report
    assert "[0, 21)" in report and "尚未人工校准" in report
    assert "自动确认通过" not in report and "各维可采纳结果" not in report


def test_functions_do_not_mutate_inputs():
    rows, calls, src = [row()], call(), source()
    before = deepcopy((rows, calls, src))
    metrics = summarize_judgments(src, rows, calls, "execute")
    render_judge_report(metrics, rows)
    build_review_queue(rows)
    assert (rows, calls, src) == before


def test_pending_preserves_positive_labels_completion_and_queue():
    sample = row()
    state = sample["support"]
    reason = {"code": "citation_page_unresolved_in_multipage_chunk", "path": "citation_evidence[0]"}
    state["human_review"] = {"status": "pending", "reasons": [reason]}
    state["verification"] = {"structure": "valid", "consistency": "valid", "evidence": "diagnostics",
                             "diagnostics": [reason], "evidence_records": [
                                 {**evidence(), "source": "week1.pdf", "page": 1, "page_end": 2}]}
    result = summarize([sample])
    assert result["model_all_positive_count"] == 1
    assert result["evaluated_phase_count"] == 2
    assert result["human_review_pending_item_count"] == result["human_review_pending_phase_count"] == 1
    assert result["human_review_reason_counts"] == {reason["code"]: 1}
    queue = build_review_queue([sample])
    assert queue[0]["phase"] == "support" and queue[0]["model_review"] == state["model_review"]
    assert queue[0]["reasons"] == [reason]
    queue[0]["model_review"]["citation_support"] = "unknown"
    assert state["model_review"]["citation_support"] == "supported"
    report = render_judge_report(result, [sample])
    assert "citation_support: supported" in report
    assert "交由人工检查" in report and "不是最终通过" in report
    assert "来源：week1.pdf · 页范围 1–2" in report


def test_one_pending_item_may_have_multiple_pending_phases():
    sample = row()
    for phase in ("answerability", "support"):
        sample[phase]["human_review"] = {"status": "pending", "reasons": [
            {"code": "model_requested_review", "path": "human_review_required"}]}
    result = summarize([sample])
    assert result["human_review_pending_item_count"] == 1
    assert result["human_review_pending_phase_count"] == 2
    assert result["human_review_reason_counts"]["model_requested_review"] == 2
    assert result["items"][0]["human_review_status"] == "pending"


def test_prepare_with_recorded_calls_cannot_claim_no_judge_call():
    sample = row()
    sample["answerability"] = sample["support"] = state("not_evaluated")
    with pytest.raises(ValueError, match="Prepare"):
        summarize([sample], calls=call(), mode="prepare")


def test_prepare_accepts_task_finished_not_evaluated_events():
    sample = row()
    sample["answerability"] = sample["support"] = state("not_evaluated", "prepare_only")
    events = [{"type": "task_finished", "item_id": "q1", "phase": phase}
              for phase in ("answerability", "support")]
    metrics = summarize([sample], calls=events, mode="prepare")
    assert metrics["call_count"] == metrics["evaluated_phase_count"] == 0


@pytest.mark.parametrize("status,message", [("incomplete", "不能当作完整评测"),
                                             ("interrupted", "运行已中断")])
def test_report_front_displays_status_integrity_and_same_model_bias(status, message):
    sample = row()
    metrics = summarize([sample])
    metrics.update(run_status=status, record_integrity="complete", same_model_as_generator=True)
    first_screen = render_judge_report(metrics, [sample]).split("## 汇总")[0]
    assert status + " / complete" in first_screen and message in first_screen
    assert "同源偏差" in first_screen
