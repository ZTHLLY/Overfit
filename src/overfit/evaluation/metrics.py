"""Independent model conclusions, technical coverage and human-review routing."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

_PHASES = ("answerability", "support")
_DIMENSIONS = {
    "answerability": ("answerable", "not_answerable", "unknown"),
    "material_support": ("supported", "partially_supported", "unsupported", "unknown"),
    "citation_support": ("supported", "partially_supported", "unsupported", "unknown"),
}
_LABELS = {
    "answerable": "可作答", "not_answerable": "不可作答", "supported": "支持",
    "partially_supported": "部分支持", "unsupported": "不支持", "unknown": "未知",
    "evaluated": "模型评审完整", "error": "技术／结构错误", "not_evaluated": "未评",
    "pending": "待人工复核", "not_required": "未触发人工复核",
}
_DIAGNOSTICS = {
    "unknown_chunk": "模型引用了未提供的 chunk，交由人工检查。",
    "quote_not_found": "摘录未逐字定位；可能有省略或改写，仅作诊断提示。",
    "quote_not_provided": "未提供摘录，来源标识仍可核对。",
    "ambiguous_quote": "摘录存在多个候选位置，未唯一定位，仅作诊断提示。",
    "citation_source_mismatch": "引用证据来源与最终引用不一致，交由人工检查。",
    "citation_page_mismatch": "引用证据页码与最终引用不一致，交由人工检查。",
    "citation_page_unresolved_in_multipage_chunk": "跨页 chunk 无法确定具体引用页，交由人工检查。",
    "positive_evidence_missing": "正向结论缺少必要来源证据，交由人工检查。",
    "material_aggregate_inconsistent": "材料总判定与所列论断不一致，保留原判并交由人工检查。",
    "model_uncertainty": "模型表示未知或部分支持，交由人工判断。",
    "model_requested_review": "模型主动请求人工复核，原因见模型说明。",
    "schema_invalid_json": "响应不是合法且无重复键的 JSON。",
    "schema_missing": "响应缺少必需字段。",
    "schema_extra_forbidden": "响应含不允许的字段。",
    "schema_literal_error": "响应判定不属于允许的枚举。",
    "schema_string_type": "响应字段应为字符串。",
    "schema_list_type": "响应字段应为列表。",
    "schema_model_type": "响应字段应为对象。",
    "schema_string_too_short": "响应必需字段为空。",
    "schema_invalid_structure": "响应字段未满足结构约定。",
    "schema_error": "响应不是符合约定结构的 JSON，没有完整模型评审。",
    "truncated_response": "响应未完整结束，没有完整模型评审。",
    "refusal": "服务返回拒绝而非完整评审。",
    "transport_error": "传输失败，没有完成本次评审。",
    "provider_error": "服务执行错误，没有完成本次评审。",
    "interrupted": "执行已中断。",
}


def _phase(row: dict, name: str) -> dict:
    return row.get(name) or {"status": "not_evaluated", "reason": "missing_review", "model_review": None}


def _verdict(row: dict, dimension: str) -> str:
    phase = _phase(row, "answerability" if dimension == "answerability" else "support")
    if phase["status"] != "evaluated":
        return "not_evaluated" if phase["status"] == "not_evaluated" else "error"
    review = phase.get("model_review") or {}
    value = review.get("verdict" if dimension == "answerability" else dimension)
    return value if value in _DIMENSIONS[dimension] else "error"


def _all_positive(row: dict) -> bool:
    """Describe the model's three labels, never claim a final quality approval."""
    return [_verdict(row, dimension) for dimension in _DIMENSIONS] == [
        "answerable", "supported", "supported"]


def _complete(row: dict) -> bool:
    return all(_phase(row, phase)["status"] == "evaluated" for phase in _PHASES)


def build_review_queue(rows: list[dict]) -> list[dict]:
    """Keep a per-phase human worklist, independent of technical and model outcomes."""
    result = []
    for row in rows:
        if row.get("post_policy") is None:
            continue
        for phase in _PHASES:
            state = _phase(row, phase)
            human = state.get("human_review") or {}
            if human.get("status") == "pending":
                result.append({"schema_version": 3, "item_id": row["item_id"], "phase": phase,
                               "status": "pending", "reasons": deepcopy(human.get("reasons", [])),
                               "model_review": deepcopy(state.get("model_review")),
                               "evidence_records": deepcopy((state.get("verification") or {}).get(
                                   "evidence_records", []))})
    return result


def _usage(finishes: list[dict], unresolved: list[str]) -> dict:
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [(call.get("response") or {}).get("usage", {}).get(key)
                  if isinstance((call.get("response") or {}).get("usage"), dict) else None
                  for call in finishes]
        result[key] = (sum(values) if not unresolved and finishes
                       and all(type(value) is int and value >= 0 for value in values) else None)
    return result


def summarize_judgments(source: dict, rows: list[dict], calls: list[dict], mode: str,
                        *, max_items: int | None = None) -> dict:
    """All retained items remain visible; selected completion is a separate denominator."""
    requested = source["requested_questions"]
    if type(requested) is not int or requested <= 0:
        raise ValueError("Requested question count must be a positive integer")
    if max_items is not None and (type(max_items) is not int or max_items <= 0):
        raise ValueError("Selected item limit must be a positive integer")
    retained = [row for row in rows if row.get("post_policy") is not None]
    if source.get("retained_count") != len(retained):
        raise ValueError("Judge rows must include every retained source item")
    ids = [row["item_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Judge item identities must be unique")
    if mode not in {"prepare", "execute"}:
        raise ValueError("Unknown judge mode")
    if mode == "prepare" and (any(
        call.get("type") in {"call_started", "call_finished"} for call in calls
    ) or any(_phase(row, phase)["status"] != "not_evaluated" for row in rows for phase in _PHASES)):
        raise ValueError("Prepare mode cannot contain executed reviews")
    selected = retained[:max_items]
    dimensions = {dimension: {label: 0 for label in (*labels, "error", "not_evaluated")}
                  for dimension, labels in _DIMENSIONS.items()}
    for row in retained:
        for dimension, counts in dimensions.items():
            counts[_verdict(row, dimension)] += 1
    phases = {}
    for phase in _PHASES:
        counts = Counter(_phase(row, phase)["status"] for row in retained)
        phases[phase] = {status: counts[status] for status in ("evaluated", "error", "not_evaluated")}
    queue = build_review_queue(retained)
    pending_ids = {entry["item_id"] for entry in queue}
    pending_reasons = Counter(reason["code"] for entry in queue for reason in entry["reasons"])
    starts = [call for call in calls if call.get("type") == "call_started"]
    finishes = [call for call in calls if call.get("type") == "call_finished"]
    finish_ids = {call["call_id"] for call in finishes}
    unresolved = [call["call_id"] for call in starts if call["call_id"] not in finish_ids]
    return {
        "schema_version": 3, "source_run_id": source["run_id"], "mode": mode,
        "calibration_status": "not_calibrated", "automatic_review": True,
        "semantic_status": "not_evaluated" if mode == "prepare" else "model_review",
        "requested_questions": requested, "raw_item_count": source.get("raw_item_count"),
        "retained_count": len(retained), "dropped_count": source.get("dropped_count"),
        "pre_policy_semantic_status": "not_evaluated", "dropped_semantic_status": "not_evaluated",
        "model_all_positive_count": sum(_all_positive(row) for row in retained),
        "complete_model_review_item_count": sum(_complete(row) for row in retained),
        "excess_retained_count": max(0, len(retained) - requested),
        "dimension_counts": dimensions, "phase_counts": phases,
        "evaluated_phase_count": sum(counts["evaluated"] for counts in phases.values()),
        "technical_error_phase_count": sum(counts["error"] for counts in phases.values()),
        "planned_phase_count": 2 * len(retained),
        "selected_item_count": len(selected), "selected_phase_count": 2 * len(selected),
        "selected_evaluated_phase_count": sum(_phase(row, phase)["status"] == "evaluated"
                                               for row in selected for phase in _PHASES),
        "human_review_pending_item_count": len(pending_ids),
        "human_review_pending_phase_count": len(queue),
        "human_review_reason_counts": dict(sorted(pending_reasons.items())),
        "all_retained_evaluated": bool(retained) and all(_complete(row) for row in retained),
        "call_starts": len(starts), "call_finishes": len(finishes),
        "response_count": sum(call.get("status") == "response" for call in finishes),
        "call_count": None if unresolved else len(finishes), "unresolved_calls": unresolved,
        "retry_count": sum(call.get("attempt", 1) > 1 for call in starts),
        "usage": _usage(finishes, unresolved), "cost": None,
        "items": [{"item_id": row["item_id"], "model_all_positive": _all_positive(row),
                   "model_review_complete": _complete(row),
                   "human_review_status": "pending" if row["item_id"] in pending_ids else
                   "not_required" if _complete(row) else "not_evaluated"} for row in retained],
    }


def _block(text) -> list[str]:
    """Every untrusted line stays in an indented code block, never Markdown syntax."""
    value = "未记录" if text is None else str(text)
    return ["    " + line for line in value.splitlines()] or ["    （空）"]


def _display(value) -> str:
    return "N/A" if value is None else str(value)


def _evidence(lines: list[str], evidence: list[dict]) -> None:
    if not evidence:
        lines.extend(["无来源证据记录。", ""])
    for record in evidence:
        lines.extend(_block(
            f"来源核验：{record.get('source_status', '未记录')}"
            f" | 摘录定位：{record.get('quote_status', '未记录')}"
            f" | 匹配：{record.get('match_method', '未记录')}"
            f" | chunk: {record.get('chunk_id', '未记录')}"
            f" | 字符 [{record.get('start', '?')}, {record.get('end', '?')})"))
        if record.get("source") is not None:
            lines.extend(_block(f"来源：{record['source']} · 页范围 {record.get('page', '?')}"
                                f"–{record.get('page_end', record.get('page', '?'))}"))
        if record.get("model_quote") is not None:
            lines += ["模型解释性摘录：", *_block(record["model_quote"])]
        lines += ["已定位原文摘录：", *_block(record.get("quote")), ""]


def _diagnostics(lines: list[str], diagnostics: list[dict]) -> None:
    for diagnostic in diagnostics:
        code = diagnostic["code"]
        lines += [*_block(f"{diagnostic['path']}: {code} — "
                           + _DIAGNOSTICS.get(code, "请检查原始调用记录。")), ""]


def render_judge_report(metrics: dict, rows: list[dict]) -> str:
    """Expose the three independent layers without turning code into a semantic judge."""
    lines = ["# 题目语义审查报告", "", "**初步模型审查，尚未人工校准。**",
             "模型原判、技术完成状态、人工复核状态分开记录；程序不作最终语义裁决。",
             "摘录逐字定位只是辅助诊断，不证明逻辑支持，也不单独否决模型结论。", ""]
    if metrics.get("run_status") or metrics.get("record_integrity"):
        lines += ["运行状态与记录完整性：", "",
                  *_block(f"{metrics.get('run_status', '未记录')} / "
                          f"{metrics.get('record_integrity', '未记录')}"), ""]
    if metrics.get("run_status") == "incomplete" or metrics.get("record_integrity") == "incomplete":
        lines += ["**选中任务或记录不完整，不能当作完整评测结论。**", ""]
    if metrics.get("run_status") == "interrupted":
        lines += ["**运行已中断；尚未完成的审查没有质量结论。**", ""]
    if metrics.get("same_model_as_generator") is True:
        lines += ["**Judge 与生成器使用相同模型标识，存在同源偏差。独立调用不等于可靠。**", ""]
    elif metrics.get("same_model_as_generator") is False:
        lines += ["Judge 与生成器的模型标识不同，但这不证明评审可靠或没有共同偏差。", ""]
    else:
        lines += ["Judge 与生成器的模型同源关系尚未确认。", ""]
    if metrics["mode"] == "prepare":
        lines += ["**本次仅准备任务，没有调用 judge；语义仍未评。**",
                  "尚无模型结果不表示题目全部错误。", ""]
    lines += ["## 汇总", "", "| 项目 | 结果 |", "| --- | ---: |"]
    for label, value in (
        ("请求题数", metrics["requested_questions"]), ("原始可解析题数", metrics["raw_item_count"]),
        ("最终保留题数", metrics["retained_count"]), ("产品丢弃题数", metrics["dropped_count"]),
        ("选中任务完整模型评审 / 选中计划", f"{metrics['selected_evaluated_phase_count']} / {metrics['selected_phase_count']}"),
        ("全量完整模型评审 / 全量计划", f"{metrics['evaluated_phase_count']} / {metrics['planned_phase_count']}"),
        ("两阶段模型评审完整题数", metrics["complete_model_review_item_count"]),
        ("Judge 三维均为正向（不是最终通过）", metrics["model_all_positive_count"]),
        ("待人工复核题数", metrics["human_review_pending_item_count"]),
        ("待人工复核阶段数", metrics["human_review_pending_phase_count"]),
        ("技术／结构错误阶段数", metrics["technical_error_phase_count"]),
        ("客户端调用尝试数", metrics["call_starts"]), ("收到模型响应数", metrics["response_count"]),
        ("重试意图次数", metrics["retry_count"]), ("费用", "未知"),
    ):
        lines.append(f"| {label} | {_display(value)} |")
    lines += ["", "完整评审表示结构完整，不表示结论正向；负判、未知、待人工复核仍计入完整模型评审。",
              "Judge 三维均为正向只描述模型原判，可同时存在待人工复核，不等于最终通过或人工认可。",
              "全量计划保留所有最终题；未选题不阻止本次选中任务 completed。",
              "调用尝试不证明请求已经送达或计费；响应数也不等于完整模型评审数。",
              "人工工作清单见 review_queue.jsonl；本轮不生成任何人工裁决。", "",
              "## 模型原判分布", "", "| 维度 | 支持 / 可作答 | 部分支持 | 不支持 / 不可作答 | 未知 | 技术错误 | 未评 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for dimension, title in (("answerability", "可作答性"), ("material_support", "材料支持"),
                             ("citation_support", "具体引用页支持")):
        counts = metrics["dimension_counts"][dimension]
        positive = "answerable" if dimension == "answerability" else "supported"
        negative = "not_answerable" if dimension == "answerability" else "unsupported"
        lines.append(f"| {title} | {counts[positive]} | {counts.get('partially_supported', 0)} | "
                     f"{counts[negative]} | {counts['unknown']} | {counts['error']} | {counts['not_evaluated']} |")
    lines += ["", "## 逐题结果", ""]
    for ordinal, row in enumerate(rows, 1):
        item = row.get("post_policy") or row.get("pre_policy") or {}
        dropped = row.get("post_policy") is None
        lines += [f"### 题目 {ordinal}", "", "身份：", "", *_block(row["item_id"]), ""]
        if dropped:
            lines += ["产品已丢弃；不属于最终保留题分母，语义未评。", ""]
        for field, title in (("question", "题干"), ("answer", "生成答案")):
            lines += [f"**{title}**", "", *_block(item.get(field)), ""]
        lines += ["**最终引用**" if not dropped else "**原始引用**", "",
                  *_block(f"{item.get('source', '未记录')} · 第 {item.get('page', '?')} 页"), "",
                  "PR1 严格引用检查（独立元数据，不覆盖模型原判）：", "",
                  *_block((row.get("post_check") or {}).get("status")), ""]
        for dimension, title in (("answerability", "可作答性"), ("material_support", "材料支持"),
                                 ("citation_support", "具体引用页支持")):
            lines += [f"**模型{title}**：{_LABELS[_verdict(row, dimension)]}", ""]
        for phase, title in (("answerability", "可作答性评审"), ("support", "材料和引用评审")):
            state = _phase(row, phase)
            model_review = state.get("model_review") or {}
            verification = state.get("verification") or {}
            human = state.get("human_review") or {"status": "not_evaluated", "reasons": []}
            lines += [f"**{title}：技术状态**", "", *_block(
                f"{state.get('technical_status', state['status'])} | 结构：{verification.get('structure', '未记录')}"), ""]
            if state.get("reason"):
                lines += [*_block(state["reason"]), ""]
            lines += ["**模型原判**", ""]
            if model_review:
                for key in ("verdict", "material_support", "citation_support", "human_review_required"):
                    if key in model_review:
                        lines += [*_block(f"{key}: {model_review[key]}"), ""]
                lines += [*_block(model_review.get("reason")), ""]
                for claim in model_review.get("claims", []):
                    lines += ["模型论断：", *_block(claim["text"]),
                              *_block(f"判定：{claim['verdict']}"), ""]
            else:
                lines += ["没有完整可解析的模型结论。", ""]
            lines += ["**人工复核状态**：" + _LABELS.get(human["status"], "未评") + "。", ""]
            _diagnostics(lines, human["reasons"])
            lines += ["**辅助核验与诊断（不改写模型原判）**", ""]
            _diagnostics(lines, verification.get("diagnostics", []))
            _evidence(lines, verification.get("evidence_records", []))
            if not model_review and state.get("raw_response") is not None:
                lines += ["原始可见响应（不构成完整模型评审）：", "", *_block(state["raw_response"]), ""]
    lines += ["## 解释边界", "", "修复前版本及丢弃题的语义均未评；不能借最终版本的判定覆盖原始输出。",
              "可疑、模糊和矛盾结果交由人工处理；本报告不提供人工最终裁决。",
              "本报告不等于人工核验、答案绝对正确或整套题教学质量认证。", "",
              "来源运行：", "", *_block(metrics["source_run_id"]), ""]
    return "\n".join(lines)
