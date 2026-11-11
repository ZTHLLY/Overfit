# Mock MCQ live-gate record

This page is the compact, append-only index for user-authorized `mcq-v1` live
gates. It records identities and bounded outcomes without copying credentials,
provider payloads, ignored output bundles, or machine-local paths into tracked
documentation. The frozen contract and gate order remain in
[MOCK-MCQ-SPEC.md](../specs/MOCK-MCQ-SPEC.md) and
[MOCK-MCQ-PLAN.md](../plans/MOCK-MCQ-PLAN.md).

## Gate status

| Gate | Status | Result |
| --- | --- | --- |
| A — four controlled cases | Complete | 4/4 cases terminal; 4/8 provider calls used; retained `0/1/2/3` |
| B — five real MCQs | Not started | Awaiting explicit user initiation |
| C — 2:1 versus 3:1 | Not started | No material ratio selected |
| D — 30-question mock | Exploratory run recorded; formal gate incomplete | User authorized an out-of-order 30-question run; generation completed, but Gates B–C remain outstanding |
| E — explicit MCQ Judge | Attempted; incomplete | 29/30 items evaluated; one technical truncation prevented judged publication |

## 2026-11-04 — Gate A

The configured Generator model was `deepseek-v4-flash`. The controlled suite
isolated Generator behavior from retrieval, embedding, reranking, and Judge.
Execution used this explicit bounded command:

```console
.venv/bin/overfit eval generator-challenge \
  eval/generator_challenges/ifn580_mcq_v1 \
  --execute --max-calls 8 --max-attempts 2
```

Parent challenge run ID: `d03d14d4-5b1e-4297-8982-88196cb0697a`.

| Case | Status | Calls | Kept | Latency | Tokens | Child run ID | Replay ID |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| `unrelated-only` | `empty` | 1 | 0 | 2.62 s | 1,356 | `e6a4895b-5fd5-4a4f-b986-8183682edc85` | `732960ee-ee62-4cbc-a1a9-1de8332611f9` |
| `partial-context` | `completed` | 1 | 1 | 9.50 s | 2,670 | `6667c34d-c56f-49ce-b9b1-6f62ab1c8c3d` | `d3d0f4ad-c7d0-40c1-bc17-47a5da1def80` |
| `gold-hard-negative` | `completed` | 1 | 2 | 15.21 s | 3,580 | `4e5003fe-d141-4236-98cd-1e97f5cd6307` | `264cb3b1-aa5e-4d6f-b669-9fd3481ff86f` |
| `multi-source` | `completed` | 1 | 3 | 9.82 s | 3,137 | `726e0498-d16a-44d8-8751-29b8e2cbd3f0` | `dfef2061-aec2-44c3-8ef7-c74e85edb715` |

All four child replays reported complete integrity. The unrelated-only case
returned no question, the partial-context case returned fewer than requested,
and the two supported cases retained their requested counts. There were no
provider errors, validation retries, citation drops, or format fallbacks. The
four calls took 37.14 seconds in total and recorded 4,541 prompt tokens plus
6,202 completion tokens, or 10,743 tokens overall.

An independent item-level semantic audit accepted Gate A: all six retained
questions had a single supported answer, accurate option feedback and citations,
and no negative stem, calculation, or all/none construction. The fabricated
hard-negative did not displace the trusted course source. Non-blocking
observations were that some distractors were easy, a few explanations were
repetitive, and correct letters were D-heavy across child exams even though
balancing was valid within each exam. Monetary cost was not available from the
recorded metadata. This controlled result is not general semantic calibration,
and no later gate was started automatically.

## 2026-11-04 — out-of-order full simulation

The user explicitly authorized one full-course simulation before Gates B and C.
This is recorded as exploratory evidence, not as formal completion of Gate D or
E and not as a 2:1 material-ratio decision. The command used the no-topic
whole-course path, so reranking was correctly skipped; the read-only index path
also made no embedding request.

```console
.venv/bin/overfit mock \
  --course IFN580_machine_learning \
  --questions 30 --trace --judge
```

### Generator result

Generator run ID: `e7b0d033-b90e-4399-a8ab-0232635410e2`. Replay ID:
`e3904cdd-4c42-41e5-ac67-c3d9ba59022f`.

- Model: `deepseek-v4-flash`.
- Three batches each received 20 frozen chunks and requested 10 questions.
- All three calls completed once, retaining `30/30`; there were no validation
  errors, citation drops, format fallbacks, or cross-batch duplicate removals.
- Correct letters were balanced `A=7, B=7, C=8, D=8`, with 30 distinct
  model-provided topic labels.
- Calls took 157.47 seconds and recorded 14,480 prompt tokens plus 30,342
  completion tokens, or 44,822 total.
- Offline replay reconstructed the complete generation record.

### Judge result

Judgment ID: `fe7e49c6-63df-498b-82b0-af8bd07a4e2b`. Replay ID:
`f0121e2f-6bcc-4dcb-89c2-33147031e26b`.

- Model: `qwen/qwen3.7-plus`.
- The Judge used 31 calls from a ceiling of 60. One schema-invalid response was
  retried successfully.
- It evaluated 29/30 items: 22 were model-positive on all mandatory dimensions,
  six were left for human review (items 4, 7, 14, 15, 17, and 27), and item 19
  was rejected automatically.
- Item 3 reached `finish_reason=length` at its output limit and returned no
  usable content. The current implementation did not spend its remaining
  attempt, so the Judge run correctly finished `incomplete` and produced no
  formal judged publication.
- Calls took 1,019.87 seconds and recorded 196,888 prompt tokens plus 60,252
  completion tokens, or 257,140 total. Judge replay reproduced the same
  incomplete state.

The complete simulation recorded 301,962 model tokens. Monetary cost was not
available from the artifacts.

### Independent item audit

An independent item-level audit against the exact per-item supplied materials
classified the 30 generated questions as 19 pass, nine pending, and two fail.
Items 17 and 24 should not be published: item 17 treated an unanswered quiz as
an answer key, while item 24 committed to one side of contradictory extracted
table/prose evidence. The pending set primarily reflected distractors not
clearly derived from the supplied material or explanations whose citations did
not fully support their extra rebuttal claims.

The audit also found Judge calibration signals in both directions. It correctly
identified several weak option-level citations, but sometimes required a source
to contain the deliberately false distractor itself even though `mcq-v1`
requires the citation to support the corrective explanation. Conversely, it
missed the contradictory extraction behind item 24. The run therefore provides
useful failure evidence, not a broad quality acceptance claim. Gates B and C
remain not started, no ratio winner is frozen, and Gates D and E remain formally
incomplete.

## 2026-11-09 — MCQ Judge v2 P9 capability gate

The user explicitly authorized sending frozen items **3 and 5**, including their
corresponding course materials, to the configured OpenRouter endpoint and
requested a record of every call. This gate used `qwen/qwen3.7-plus` with a hard
ceiling of **four calls**. No other items or later gates were executed.

The offline suite passed **953 tests**, Ruff, and `git diff --check` before the
live run. The v2 contract and gate sequence are defined in
[MCQ-JUDGE-V2-SPEC.md](../specs/MCQ-JUDGE-V2-SPEC.md) and
[MCQ-JUDGE-V2-PLAN.md](../plans/MCQ-JUDGE-V2-PLAN.md).

- Generation run: `e7b0d033-b90e-4399-a8ab-0232635410e2`.
- Frozen comparison judgment: `fe7e49c6-63df-498b-82b0-af8bd07a4e2b`.
- V2 capability judgment: `0bea90fc-e8d0-498f-8473-29ec0ae58f34`.
- Offline replay: `30539bfd-594d-4da5-9e30-edcceb94f6f6`.
- Protocol: `mcq-judge-v2`.
- Protocol digest:
  `bbcdfa3958bdb552c887eebbb2e38103a51a9d7f2be57a54836dc1579771f4c3`.

### Per-call outcomes

Each call's intent was persisted before execution, followed by its full visible
response, normalized usage, duration and safe error status in `calls.jsonl`.
Credentials, headers, hidden reasoning and arbitrary provider metadata were not
retained. Source and base identities were verified before calls and after the
final response.

| Item | Call ID | Attempt | Finish | Duration | Prompt tokens | Completion / visible tokens | Reasoning tokens | Total tokens | Model result |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 3 | `27571552-63f6-435c-9fe3-f3c4131d39ba` | 1 | `stop` | 7.91 s | 5,506 | 321 / 321 | 0 | 5,827 | All mandatory dimensions positive; no human review requested |
| 5 | `e87859d5-e882-41da-b3cb-6069b428ad0c` | 1 | `stop` | 7.99 s | 5,952 | 333 / 333 | 0 | 6,285 | All mandatory dimensions positive; no human review requested |

Both recorded requests used strict JSON Schema output, `reasoning.enabled=false`,
`provider.require_parameters=true`, temperature zero, non-streaming output and an
8,192 completion-token limit. Both responses passed strict local validation on
the first attempt. There were **two calls out of four**, no retries and no
technical errors. Recorded totals were 11,458 prompt tokens, 654 completion tokens
(all visible), zero reasoning tokens and 12,112 overall tokens, with 15.90 seconds
of summed call time. No monetary cost is inferred without a recorded price
snapshot or explicit rates.

### Interpretation and boundary

Item 3, which previously truncated at 4,096 completion tokens without usable
content, now returned a valid review in one call. Item 5, which previously needed
a schema retry, also returned a valid review on its first attempt. These are
observed technical improvements, not a controlled speed or quality benchmark.

The endpoint accepted the required request parameters; the two visible outputs
matched the strict schema and reported zero reasoning tokens. These observations
meet the P9 capability checks without an observed parameter violation or replay
mismatch. They do not independently prove every provider-side enforcement detail:
neither call reached the token cap, and the second-attempt 16,384-token request
was not exercised live. P9 is not a semantic calibration or general quality gate.

Offline replay reconstructed the selected rows, metrics, report and review queue.
The recording is a **non-publishing calibration**: no judged question paper or
answer key was written, no v1 decisions were reused, and both the generation and
comparison records remain unchanged. P10 (the fixed 14-item calibration) and P11
(the full formal v2 run) remain **not started**, pending separate explicit user
initiation.

## 2026-11-09 — MCQ Judge v2 P10 anomaly calibration

After reviewing P9, the user explicitly initiated the fixed 14-item calibration
against the same generation, frozen v1 comparison judgment and configured
OpenRouter `qwen/qwen3.7-plus` service. Selection:
`3,4,7,14,15,17,18,19,20,22,24,27,28,29`. The hard ceiling was **28 calls**.

**Outcome: technically complete and replay-verified; semantic calibration
failed. P11 must not proceed on this evidence.** A `completed` recording means
technical completion, not that the Judge's semantic decisions were accepted.

- Generation run: `e7b0d033-b90e-4399-a8ab-0232635410e2`.
- Frozen v1 comparison: `fe7e49c6-63df-498b-82b0-af8bd07a4e2b`.
- V2 calibration judgment: `6078a0ca-961b-48b7-8a88-6da1ba7c6449`.
- Offline replay: `4a5925db-354a-451e-8cc3-f49256082f89`.
- Protocol digest remains
  `bbcdfa3958bdb552c887eebbb2e38103a51a9d7f2be57a54836dc1579771f4c3`.

### Every recorded call

All 14 requests used the frozen first-attempt policy: strict JSON Schema,
reasoning disabled, required provider parameters, 8,192 completion tokens,
temperature zero and non-streaming output. Every response finished with `stop`
and passed strict local schema validation. All were model-positive on every
mandatory dimension, including every material-derived and citation flag; none
requested human review. Each complete visible request/response, safe status,
duration and normalized usage is retained in the judgment's `calls.jsonl`.

| Item | Call ID | Attempt | Finish | Duration | Prompt tokens | Completion / visible tokens | Reasoning tokens | Total tokens |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 3 | `c3362cb8-b513-4e3f-9199-12653176765d` | 1 | `stop` | 8.03 s | 5,506 | 335 / 335 | 0 | 5,841 |
| 4 | `c2f9d5e5-aabb-4b82-8b4e-b3cdfef74206` | 1 | `stop` | 7.36 s | 6,232 | 339 / 339 | 0 | 6,571 |
| 7 | `e0b0dc59-e16f-41e6-9002-24cb6e1c4911` | 1 | `stop` | 7.08 s | 5,911 | 332 / 332 | 0 | 6,243 |
| 14 | `5722889a-cece-496a-b64d-58564ed5b1af` | 1 | `stop` | 7.02 s | 6,235 | 331 / 331 | 0 | 6,566 |
| 15 | `58cabf38-40bd-420c-9ad2-48d83536323e` | 1 | `stop` | 7.64 s | 6,176 | 333 / 333 | 0 | 6,509 |
| 17 | `3940915e-dbc1-4d7c-8fa2-969eb174a54e` | 1 | `stop` | 9.46 s | 6,175 | 478 / 478 | 0 | 6,653 |
| 18 | `fdc02ffc-b565-43e2-af12-915ae6464862` | 1 | `stop` | 7.20 s | 5,929 | 333 / 333 | 0 | 6,262 |
| 19 | `7a3abd79-b860-4734-9547-868265c82b7d` | 1 | `stop` | 7.21 s | 5,949 | 310 / 310 | 0 | 6,259 |
| 20 | `b24dfdf2-df59-4376-9843-c378492e6a97` | 1 | `stop` | 7.33 s | 5,932 | 327 / 327 | 0 | 6,259 |
| 22 | `011728eb-56a7-4ee9-b6a3-f2ff89c8fa37` | 1 | `stop` | 6.59 s | 5,935 | 296 / 296 | 0 | 6,231 |
| 24 | `a4415ef8-6c77-463a-9ee5-f250cd043d3e` | 1 | `stop` | 7.78 s | 6,226 | 351 / 351 | 0 | 6,577 |
| 27 | `9b7aac79-edfe-4611-a413-5b9d5ce6f362` | 1 | `stop` | 7.73 s | 5,914 | 338 / 338 | 0 | 6,252 |
| 28 | `a1a8c702-254d-452a-89d3-a2c8aee06b76` | 1 | `stop` | 6.74 s | 5,442 | 309 / 309 | 0 | 5,751 |
| 29 | `91c2a2c3-7e58-42c8-9125-f485459e8866` | 1 | `stop` | 6.26 s | 5,900 | 309 / 309 | 0 | 6,209 |

Totals: **14/28 calls**, no retries or technical errors, **103.41 seconds** of
summed call time (median 7.27 seconds), **83,462 prompt tokens**, **4,721 completion
tokens** (all visible), **zero reported reasoning tokens**, and **88,183 total
tokens**. No monetary cost is inferred. Both attempts' requests remain covered
offline; no second attempt or 16,384-token fallback was needed live.

### Comparison with frozen v1

For these same selected items, v1 had six model-positive decisions (18, 20, 22,
24, 28, 29), six pending human review (4, 7, 14, 15, 17, 27), one rejection (19)
and one technical truncation (3). V2 marked all 14 model-positive, with no pending
items or mandatory failures. The empty review queue therefore reflects the
model's decisions, not evidence that review is unnecessary.

The technical truncation on item 3 is resolved in this run. The six former
pending items and the former rejection all became positive. This is not, by
itself, a semantic improvement: in particular, item 17 regressed from pending to
unsupported acceptance, and item 24's known extraction conflict remained missed.

### Independent evidence checks

Three separate read-only reviewers inspected disjoint item groups against the
exact supplied per-item chunks before reading the detailed item-level v2
rationales, then compared their findings with those rationales. Aggregate live
outcome information was already available to some reviewers, so this was a
source-first diagnostic review, not a fully blinded study. It is not a
human-labeled gold set or a reconstruction of the earlier
19-pass/nine-pending/two-fail audit. No overall accuracy is claimed for this
deliberately selected anomaly set.

There are legitimate citation-semantics improvements as well as failures.
Item 14's vocabulary-presence code supports the corrective explanations without
needing to contain the deliberately false counting/sorting distractors. Item 27
can likewise retain positive citation judgments for the BERT pre-training
correction; supervised fine-tuning is grounded elsewhere in its supplied batch.
These two new positive outcomes are consistent with the independent review.
For item 19, correcting the old citation objection is appropriate, but the
support-vector-machine/linear-kernel distractor in option C introduces a concept
absent from that item's supplied chunks. Its material-derived flag should still
fail: a supported decoder-only correction does not establish SVM provenance.

The following findings are sufficient to fail P10's semantic gate:

- **Item 17 — unsupported answer-key inference.**
  `week3_tutorial_p2_c2` presents an unanswered data-types quiz and candidate
  answer combinations; it does not identify a correct combination. The v2
  rationale guesses the likely intended answer key and invokes general
  statistics, yet sets support/citations true and human review false. The
  generator's claim that the material establishes the answer remains unverified.
- **Item 24 — no escalation for contradictory extraction.**
  `week3_tutorial_solutions_p5_c10` shows missing Income and Religion in the
  table, while its prose describes missing Age and filling age with average
  income. V2 selects the prose account without acknowledging this conflict.
  The numeric/categorical imputation concept is not itself the claimed error;
  the failure is unqualified acceptance rather than the required human review.
- **Item 7 — unsupported attribution to the cited page.**
  `week10_gen_ai_p78_c53` defines Support bots, Personalization engines and
  Knowledge summaries, not correlation heatmaps. V2's rationale attributes a
  heatmap distinction to that same material and approves the option-D citation.
  Heatmaps do occur elsewhere in the supplied batch, so this is a citation
  coverage problem, not proof that the option is outside the supplied material.
- **Material-derived detection did not discriminate.** Clear examples requiring
  rejection or review include fixed grammar rules in 18A/20B, a grammar checker
  in 22D, and notebook-to-Python-package conversion in 29C. Their corrective
  explanations can be supported without establishing that these added concepts
  derive from the supplied material. V2 nevertheless marks every derived flag
  true. Item 22's rationale even refers to unrelated concepts while leaving all
  four material-derived flags true.

This is not a blanket rejection of every distractor absent verbatim from a
source. For example, item 28's overfitting distractor can be grounded in other
chunks supplied to that item (`week5_tutorial_solutions_p3_c2` and
`week6_neural_networks_p95_c75`) and deliberately misapplied to RNNs. Its positive
material-derived decision should not be counted as a miss simply because the
RNN citation does not contain that distractor's wording.

The observed failure pattern is over-acceptance: the model often substitutes
outside knowledge or a supported correction for evidence supporting the full
review contract. The run changed multiple protocol features, so it cannot
isolate whether disabling reasoning, rubric changes or another factor caused
the semantic regression.

### Publication and next-step boundary

All results remain **non-publishing calibration evidence**. No judged paper or
answer key was written, no v1 rows were merged, and no pending item was manually
accepted. Raw v2 decisions remain unchanged even where this independent review
disagrees with them. P11 was not executed and is blocked by the failed semantic
gate. Any proposed rubric/request/review-policy fix must use a new immutable
protocol entry, not silently rewrite this recorded v2 protocol or rerun the
calibration without a new authorization.
