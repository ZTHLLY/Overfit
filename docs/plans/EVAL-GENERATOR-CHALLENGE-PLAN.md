# Generator Challenge Implementation Plan

> Version 0.1 · 2026-11-03 · offline implementation, authorized live execution, replay, and Judge follow-up complete

1. **P0 – freeze contract — complete**: spec, schemas, four case meanings, permission and budget.
2. **P1 – hard call budget — complete**: add an optional Generator provider-call budget; retain
   ordinary mock behavior and test fallback-boundary exhaustion first.
3. **P2 – fixture trace — complete**: reuse `TraceWriter`, standard report/artifacts, and citation
   policy for controlled ordered chunks without a database.
4. **P3 – suite runner — complete**: strict input/annotation isolation, prepare/execute modes,
   sequential global budget, atomic parent JSON/Markdown, stop-on-failure.
5. **P4 – CLI and fixtures — complete**: add `eval generator-challenge`; create the four frozen
   IFN580 fixture cases and human annotations.
6. **P5 – offline acceptance — complete**: targeted/full tests with empty rerank key,
   Ruff, `git diff --check`, and independent review. No real API call occurred during
   implementation.
7. **P6 – explicit live run — complete**: after user authorization, execute within
   the hard budget, replay every trace, and explicitly Judge the retained traces.

No commit/push is part of this plan. Existing untracked indexes and historical
outputs are out of scope.

## Offline evidence

- Targeted Generator challenge/trace tests: `28 passed`.
- Full test suite with `OVERFIT_RERANK_API_KEY=''`: `740 passed`.
- `ruff check .`: passed.
- `git diff --check`: passed.
- Frozen suite loads with four ordered cases and matching annotation case types.
- Prepare CLI completed without loading settings or making a model call.

## Live execution evidence

- Parent run: report (local artifact: `outputs/eval/generator-challenges/9a28bfe3-2b6f-4631-9b97-f1d704817796/report.md`)
  and structured results (local artifact: `outputs/eval/generator-challenges/9a28bfe3-2b6f-4631-9b97-f1d704817796/results.json`).
- Completion: 4/4 cases, 8/20 provider calls, 11,392 tokens, four initial
  `json_schema` provider/fallback errors, zero validation errors, cost unavailable.
- Outcomes: unrelated-only 0 items (`model_empty`); partial-context 1/2 narrowed
  supported items; gold-hard-negative 2/2 trusted-chunk citations and zero distractor
  citations; multi-source generated 3/3 single-citation questions, with the first
  later judged material-supported but citation `partially_supported`.
- Every generation replay completed. Follow-up Judge covered all six retained items
  with 12/12 responses, 33,977 tokens, zero retry/error: four all-positive, one
  `not_answerable` because the supplied materials conflicted while the answer was
  supported, and one citation `partially_supported` pending human review. Every
  Judge replay completed.
- Exact configured secret values were absent from all checked artifacts. The fixture
  directly supplied chunks; retrieval and reranking were outside this isolated run.

Artifact links and the non-duplicated result index are in
[GENERATOR-JUDGE-CHALLENGE.md](../evaluation/GENERATOR-JUDGE-CHALLENGE.md).
