# Rerank 评测索引

> 最后更新：2026-10-28 · 状态：完整

本页只是评测产物索引；不复制 94 条逐题记录。JSON 是事实源，Markdown 是人类可读摘要。

## P9：47 题双轨正式评测

- 运行目录（local artifact）：`outputs/eval/rerank-20261028T072026Z-4a90e805-c4c9-40d5-94e1-3040e67ae23d`
- JSON 事实源（local artifact）：`outputs/eval/rerank-20261028T072026Z-4a90e805-c4c9-40d5-94e1-3040e67ae23d/results.json`
- 人类可读摘要（local artifact）：`outputs/eval/rerank-20261028T072026Z-4a90e805-c4c9-40d5-94e1-3040e67ae23d/report.md`
- Dense baseline（local artifact）：`outputs/eval/retrieval-20261027T084444Z/results.json`
- 行为规范：[`RETRIEVAL-RERANK-SPEC.md`](../specs/RETRIEVAL-RERANK-SPEC.md)
- 实施计划与验收证据：[`RETRIEVAL-RERANK-PLAN.md`](../plans/RETRIEVAL-RERANK-PLAN.md)

本次运行为 47 题 × `question-only` / `question+options`，顺序 `concurrency=1`，强制 `--no-cache`。评测未调用 Generator 或 judge。索引快照、调用计数、指标、延迟、usage/cost 和 secret 检查证据见上述 spec/plan 与运行报告。

显式执行命令：

```bash
uv run overfit eval rerank \
  --course IFN580_machine_learning \
  --dataset eval/ifn580_eval.json \
  --execute \
  --no-cache
```
