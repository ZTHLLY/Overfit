# Generator Challenge Spec

> Version 0.1 · 2026-11-03 · contract frozen; offline implementation and authorized four-case live run complete

## Purpose

Evaluate generation behavior under controlled material fixtures without opening or
modifying the production index. This is separate from retrieval/rerank evaluation
and from Judge calibration.

## Contract

- CLI: `overfit eval generator-challenge SUITE_DIR`.
- Prepare is the default and reads no runtime settings, keys, or providers.
- Real execution requires `--execute` and a positive global `--max-calls` budget.
- Execution is sequential. `--max-attempts` defaults to 2.
- The budget counts every provider dispatch, including structured-output fallbacks.
  The next call is rejected before dispatch once the budget is exhausted.
- Ordinary `mock` behavior is unchanged when no budget is supplied.
- Each case receives only its ordered chunks, course, topic, and question count.
  Human annotations, targets, trusted/distractor labels, sibling cases, and design
  reasons are never model-visible.
- Each case produces a normal generation trace that must pass `eval replay`; a
  retained trace must be consumable by `eval judge` as a separate explicit step.
- `model_empty` and `all_dropped` are semantic outcomes, not technical failures.
  Provider/schema exhaustion, recording failure, shutdown, or budget exhaustion
  stops the parent run as incomplete/interrupted. No automatic continuation.
- Inputs and annotations are strict, fully validated before selection, snapshotted,
  hashed, and may not be symlinks. Duplicate JSON keys/IDs and non-finite values fail.
- Parent JSON is the source of truth; Markdown is a compact index/report.
- No course file, production database, historical output, or API secret is modified.

## Frozen v1 fixtures

1. `unrelated-only`: topic is absent from all supplied chunks; observe empty output,
   topic drift, or unsupported invention.
2. `partial-context`: only one half of a composite concept is supplied; observe
   narrowing versus invented completion.
3. `gold-hard-negative`: trusted material and a fluent contradictory distractor are
   both supplied; annotations provide the external ground truth.
4. `multi-source`: a useful composite answer spans two sources/pages, exposing the
   current one-`source/page` item schema.

The first version reports facts and human-designed targets. It does not collapse
them into one automatic accuracy score.

## Acceptance seams

- Prepare: zero settings/key/client/network access.
- Suite loader: strict schema, IDs, references, symlinks, finite numbers, full-set
  validation before `max-items`.
- Information isolation: annotation sentinels never occur in provider requests.
- Provider budget: call intent precedes dispatch; N+1 never dispatches.
- Trace interoperability: replay succeeds and Judge prepare accepts retained traces.
- Failure recording and secret handling are fail closed.
- Full pytest, Ruff, diff check, and independent review pass before real execution.

## Completed evidence

The offline implementation satisfied the acceptance seams above and the authorized
live suite completed at
generator challenge run `9a28bfe3-2b6f-4631-9b97-f1d704817796` (local artifact: `outputs/eval/generator-challenges/9a28bfe3-2b6f-4631-9b97-f1d704817796/report.md`).
It completed 4/4 cases with 8/20 provider calls and 11,392 total tokens. Each
case's first `json_schema` request produced a provider-format error and the
explicit fallback succeeded: four provider errors/format fallbacks in total,
with zero validation errors. Cost is unavailable rather than zero.

- `unrelated-only`: returned no item (`model_empty`).
- `partial-context`: requested two and retained one deliberately narrowed,
  material-supported few-shot item; it did not invent fine-tuning or RLHF content.
- `gold-hard-negative`: retained 2/2; both cite the trusted course chunk and none
  cite the contradictory distractor.
- `multi-source`: retained 3/3 as separate single-citation questions. In the
  follow-up Judge run, the first item's material was supported but its citation
  was `partially_supported` and remains pending human review.

All four standard generation traces passed `eval replay`. The six retained items
were then evaluated in three explicit Judge runs: 12/12 responses, 33,977 tokens,
zero retry and zero technical error. Four items were all-positive; one hard-negative
item was `not_answerable` because the supplied materials conflicted even though its
answer remained supported; one multi-source formula item had supported material but
`partially_supported` citation and remains pending human review. All three Judge
replays completed. Exact configured secret values were absent from the recorded
artifacts. The fixture supplied ordered chunks directly, so retrieval and the
reranker did **not** participate in this isolated Generator experiment.

The compact artifact map and run-by-run links are maintained in
[GENERATOR-JUDGE-CHALLENGE.md](../evaluation/GENERATOR-JUDGE-CHALLENGE.md);
raw model responses remain only in their run artifacts.
