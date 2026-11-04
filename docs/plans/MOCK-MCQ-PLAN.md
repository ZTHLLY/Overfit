# Mock MCQ v1 Implementation Plan

> Version 0.1 · 2026-11-04 · status: **offline implementation complete; Gate A complete; exploratory full run recorded**
>
> Milestones P0–P8 are complete and verified offline. P9 Gate A is complete;
> Gates B–C have not started, while out-of-order exploratory Gate D/E attempts
> remain formally incomplete. No ratio winner or broad semantic acceptance result
> is claimed.

The normative contract is
[MOCK-MCQ-SPEC.md](../specs/MOCK-MCQ-SPEC.md). If this plan conflicts with the
spec, the spec wins.

## 1. Delivery boundary

The baseline before this work is the short-answer Generator on the `ai-dev`
branch. It uses a portable `{"questions": [...]}` JSON-object envelope,
Pydantic/citation validation, trace/replay, optional separate Judge evaluation,
and a no-topic default of 30. This plan replaces only new `mock` generation with
MCQ v1 while preserving historical read/replay behavior.

Target flow:

```text
whole-course or explicit topic selection
  → bounded batch plan (≤10 questions per batch)
  → DeepSeek-compatible json_object MCQ generation
  → mechanical normalization + strict local validation
  → checkpoint
  → conservative cross-batch dedup
  → optional MCQ Judge
  → deterministic A–D balancing
  → shuffled MCQ question paper + explanatory answer key
```

No commit/push or live provider call is implicit in a milestone. Live gates run
only after the user explicitly initiates each gate.

## 2. Planned milestones

1. **P0 — contract and baseline lock — complete (offline)**
   - land this spec/plan only;
   - snapshot baseline tests and current short-answer trace compatibility;
   - inventory all Generator/Judge/replay contracts affected by MCQ fields.

2. **P1 — contract tests first — complete (offline)**
   - add failing model, normalization, rendering, CLI, batching, replay, and Judge tests;
   - prove the current code cannot accept/publish MCQ v1 before implementation;
   - keep every default test offline with fake clients.

3. **P2 — MCQ models and boundary normalization — complete (offline)**
   - introduce the exact A–D option object contract;
   - implement page/page-range and label/prefix normalization;
   - derive legacy Judge compatibility fields from the correct option;
   - reject semantic ambiguity instead of repairing it.

4. **P3 — prompt and rendering migration — complete (offline)**
   - replace new short-answer generation prompts with MCQ-only instructions;
   - render question paper without topic hints;
   - render answer key with correct answer and A–D feedback/citations;
   - make topic mode require explicit `--questions`.

5. **P4 — batch orchestration and checkpointing — complete (offline)**
   - plan ≤10-question batches;
   - enforce three Generator dispatches per batch;
   - stop fail-closed while preserving complete checkpoints;
   - merge complete batches, conservatively deduplicate, and never top up.

6. **P5 — deterministic option balancing — complete (offline)**
   - reorder whole option objects after validation/dedup;
   - balance correct A–D counts to a spread ≤1 where possible;
   - record stable seed identity and before/after mappings;
   - prove option text, explanation, citation, and correct label never detach.

7. **P6 — trace/replay versioning and migration — complete (offline)**
   - add immutable `generation_contract=mcq-v1`;
   - record batch, normalization, dedup, balance, and artifact events;
   - dispatch replay by recorded contract;
   - retain legacy short-answer trace/Judge replay without rewriting old records.

8. **P7 — optional MCQ Judge — complete (offline)**
   - add MCQ-specific requests and verdict schema;
   - review single-best-answer, correct option, other options, feedback, citations,
     positive stem, and no-calculation contract;
   - derive a finite call ceiling internally;
   - exclude failed and pending-human-review items from judged publication;
   - mark any technical incompleteness fail-closed.

9. **P8 — controlled fixtures and offline acceptance — complete (offline)**
   - migrate/extend the four isolated Generator challenge cases for MCQ output;
   - add ambiguity, duplicate-option, citation, page-range, batching, dedup, and
     correct-letter-balance fixtures;
   - pass full pytest, Ruff, `git diff --check`, link/secret scans, replay checks,
     and independent review before any real call.

10. **P9 — user-authorized live gates — in progress (Gate A complete)**
    - execute the controlled four-case Generator gate — complete;
    - execute a five-question real MCQ smoke;
    - run the 2:1 versus 3:1 material-ratio comparison;
    - freeze the winning ratio only after the quality-first comparison;
    - execute a 30-question three-batch mock;
    - explicitly run MCQ Judge only when the user requests `--judge`;
    - replay and audit every completed run.

## 3. Implemented code and document changes

| Path | Baseline responsibility | Implemented MCQ responsibility |
| --- | --- | --- |
| [models.py](../../src/overfit/models.py) | legacy generated question/answer item and shared models | add versioned MCQ option/item models; preserve legacy read types |
| [generator.py](../../src/overfit/query/generator.py) | JSON-object generation, validation retry, citation policy, rendering context | MCQ normalization, atomic validation, compatibility projection, batch-safe rendering input |
| [system.j2](../../src/overfit/templates/system.j2) | grounded short-answer generation rules | MCQ-only, positive stem, no-calculation, concise feedback rules |
| [mock_prompt.j2](../../src/overfit/templates/mock_prompt.j2) | requests topic/question/answer/source/page | requests exact A–D option objects and `correct_option` |
| [mock_exam.md.j2](../../src/overfit/templates/mock_exam.md.j2) | topic-grouped question paper | shuffled numbered stem + A–D paper without topic hints |
| [mock_answers.md.j2](../../src/overfit/templates/mock_answers.md.j2) | one answer and citation | correct answer plus four option explanations and citations; topic retained |
| [cli.py](../../src/overfit/cli.py) | mock options and orchestration | topic explicit-question guard, `--judge`, batch status, judged/unjudged semantics |
| [live.py](../../src/overfit/evaluation/live.py) | single-generation trace lifecycle and fixture execution | versioned batch/checkpoint lifecycle and MCQ artifacts |
| [trace.py](../../src/overfit/evaluation/trace.py) | generic event writer | MCQ contract identity and complete batch/normalization/balance events |
| [replay.py](../../src/overfit/evaluation/replay.py) | offline validation of legacy generation traces | contract-version dispatch and MCQ replay |
| [judge.py](../../src/overfit/evaluation/judge.py) | short-answer answerability/support prompts | MCQ request/verdict contracts and option-level evidence |
| [runner.py](../../src/overfit/evaluation/runner.py) | Judge lifecycle, metrics, publication inputs | MCQ mandatory dimensions, internal ceiling, pending exclusion |
| [metrics.py](../../src/overfit/evaluation/metrics.py) | short-answer fixed-denominator metrics | versioned MCQ dimensions without changing old metric meaning |
| [legacy generator challenge suite](../../eval/generator_challenges/ifn580_v1/README.md) and [MCQ suite](../../eval/generator_challenges/ifn580_mcq_v1/README.md) | four controlled short-answer generation cases preserved as read-only evidence | separate versioned MCQ cases without rewriting v1 evidence |
| [README.md](../../README.md) and architecture docs | current short-answer behavior | MCQ v1 CLI, artifacts, Judge boundary, legacy note |

Names may change to match module conventions, but responsibilities and version
boundaries may not be collapsed merely to reduce file count.

## 4. TDD sequence

### 4.1 P1 red tests

Write tests before implementation for:

- exact A–D keys, legal `correct_option`, non-empty distinct option text;
- all four feedback/citation objects required;
- prohibited all/none and negative stems;
- page normalization for integer, numeric string, range string, and explicit
  `page_end`;
- page range/source must match supplied material;
- lowercase labels and duplicate A-prefix mechanical repair;
- semantic fields never auto-repaired;
- an invalid option invalidates the whole item;
- topic without explicit `--questions` fails before settings/client/index access;
- no-topic omission still resolves to 30;
- ≤10 planned questions per batch and ≤3 provider calls per batch;
- failed batch stops later dispatches and does not publish a complete exam;
- fewer items and duplicate removal do not trigger top-up;
- conservative dedup keeps related-but-distinct questions;
- correct letter balancing moves full option objects and achieves spread ≤1;
- question paper hides topics/citations; answer key contains all feedback/citations;
- old short-answer trace replay remains unchanged;
- ordinary mock says `not judged` and creates no Judge client;
- explicit Judge sees A–D, correct option, explanations, citations, and materials;
- Judge failures/pending/technical incompleteness have the frozen publication status;
- real provider construction/network is impossible in default tests.

### 4.2 Green implementation

Implement the smallest behavior that satisfies the frozen tests in milestone order.
Do not combine semantic Judge decisions with deterministic local validation. Do not
add calculation, difficulty, mixed-format, or public resume behavior while making
tests pass.

### 4.3 Refactor and review

After green:

- remove duplicated legacy/MCQ parsing without erasing the version boundary;
- keep provider SDK types at adapters;
- review all output schemas for finite call/size limits;
- run independent author and reviewer passes;
- rerun old replay fixtures before considering any live gate.

## 5. Versioning and migration strategy

### 5.1 Write contracts

- Current historical Generator records keep their recorded legacy behavior.
- New MCQ writes use `generation_contract=mcq-v1`.
- No unversioned record may be guessed as MCQ from field presence alone.
- New `mock` has no short-answer generation option.

### 5.2 Read/replay contracts

Replay chooses a validator using immutable recorded identity:

```text
legacy/absent historical contract → legacy validator
mcq-v1                           → MCQ validator
unknown future contract          → fail unsupported
```

No migration job edits historical JSON/JSONL/Markdown. Existing short-answer
Judge results retain their original dimensions and denominators.

### 5.3 Compatibility projection

For existing common Judge plumbing, MCQ v1 may derive a read-only projection from
the correct option:

```text
answer = correct option text + correct option explanation
source/page/page_end = correct option citation
```

This projection is not a second model-generated fact, is recorded as derived, and
cannot replace MCQ-specific Judge input.

## 6. Batch/checkpoint design gate

Before P4 implementation, tests must freeze a parent run identity containing:

- requested total and ≤10-question batch plan;
- frozen selected material identities per batch;
- prompt/model/config identities;
- per-batch call counter and maximum three dispatches;
- batch state: planned/running/completed/failed/interrupted;
- normalized item hashes;
- terminal completeness;
- no provider secret.

A completed batch is immutable. Development recovery may reuse it, but public
resume UX is out of scope. Final artifacts are written only after all required
batches are complete, merge/dedup succeeds, optional Judge reaches a terminal
complete state, and deterministic balancing is recorded.

## 7. Offline test matrix

| Area | Required tests |
| --- | --- |
| Models | exact keys, labels, uniqueness, explanation/citation presence, page ranges |
| Normalizer | allowed mechanical repair, idempotence, invalid/ambiguous rejection |
| Prompt | MCQ-only fields, positive stem, no calculation, fewer-is-valid |
| Renderer | A–D question paper; correct/incorrect feedback and per-option citations |
| CLI | no-topic default30; topic requires explicit count; Judge default off |
| Batching | 30→3×10, partial last batch, three-call limit, stop-on-failure |
| Checkpoint | pre-dispatch intent, immutable completed batch, terminal fail-closed |
| Dedup | clear duplicate removed; related distinct retained; no top-up |
| Balance | deterministic mapping, A–D spread, atomic option movement |
| Citation | each correction supported by its own supplied source/page range |
| Trace/replay | MCQ identity/events and unchanged legacy replay |
| Judge | single best, correct support, other-option invalidity, feedback, citations |
| Publication | failed/pending excluded; technical error incomplete; unjudged disclosure |
| Security | no keys, headers, private metadata, symlinks, or non-finite values |
| Network | fake-only default suite; live tests skipped without explicit execution |

Full regression, Ruff, `git diff --check`, Markdown-link checks, XML validation for
changed diagrams, and exact-secret scans are required at P8.

## 8. Controlled and live gates

No live gate begins from a failed offline gate. Each live gate requires the user to
initiate it while present.

### Gate A — four controlled cases

Use versioned MCQ fixtures covering:

1. unrelated-only material: empty output is valid;
2. partial context: fewer questions, no invented completion;
3. trusted + contradictory material: one best answer and evidence-correct feedback;
4. multi-source material: option-level citations remain attached correctly.

Required before advancing:

- valid `questions` envelope and exact A–D shape for every retained item;
- zero secret leakage;
- complete call/normalization trace;
- replay completes;
- no later gate is started after a technical failure.

### Gate B — five real MCQs

Run one small real selection/generation path. Inspect:

- stem/option/feedback lengths;
- correct letter distribution;
- page normalization;
- whether any option is also correct;
- per-option citation accuracy;
- prohibited question styles/calculation;
- token, calls, latency, and provider errors.

This gate is a pilot, not semantic calibration.

### Gate C — 2:1 versus 3:1 material ratio

Run frozen parallel tracks with identical requested counts/configuration except
material count. Compare in the spec’s priority order. A ratio is frozen only after
reviewing structured metrics and actual questions. Quality tie → lower cost wins.

### Gate D — 30-question three-batch mock

Only after Gates A–C:

- request 30 in three ≤10-question batches;
- verify checkpoint and no full-run retry for one batch-format error;
- merge, conservatively deduplicate, and balance labels;
- confirm final count honestly reflects drops/fewer results;
- render question and answer files;
- replay the complete run.

### Gate E — explicit MCQ Judge

Run only when the user explicitly selects `--judge`. Confirm:

- finite internally derived call ceiling;
- every mandatory dimension complete for published items;
- pending items remain review-only;
- failed items are excluded atomically;
- technical incompleteness does not publish a judged-complete exam;
- Judge replay and secret scans pass.

## 9. Acceptance and stop conditions

### 9.1 Offline engineering acceptance

Implementation may reach the live gate only when:

- all MCQ contract requirements MCQ-01…MCQ-16 have deterministic tests;
- legacy replay/Judge regression remains green;
- default tests prove zero real network;
- complete/failure artifacts are replayable;
- full pytest, Ruff, diff, link, XML, and secret checks pass;
- independent review has no blocking finding.

### 9.2 Live acceptance

Live testing does not create an unconditional semantic accuracy claim. Advancement
requires:

- no unresolved call or incomplete artifact;
- exact structural validity for every retained item;
- observed provider behavior within frozen call ceilings;
- page normalization/citation mapping preserved;
- human inspection of all items in the 5-question gate;
- ratio selection based on the frozen metric order;
- every item in a judged publication passes all mandatory MCQ Judge dimensions.

The 30-question unjudged run may publish with the explicit “not judged” disclosure;
it is not described as semantically verified.

### 9.3 Immediate stop

Stop the current gate and do not continue automatically on:

- provider/recording/checkpoint exhaustion;
- unresolved or over-budget call;
- secret or authorization data in an artifact;
- response/material identity mismatch;
- replay integrity failure;
- unknown contract version;
- Judge technical incompleteness for a judged publication; or
- user interrupt/shutdown.

Completed evidence remains; no historical artifact is rewritten.

## 10. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Two or more acceptable options | optional MCQ Judge; fail item atomically when judged |
| Weak/unrelated distractors make questions easy | accepted v1 tradeoff; correctness beats forced plausibility |
| Feedback “supports” a false distractor | citation semantics explicitly support the correction, not false text |
| Nested JSON drifts | portable `json_object`, tolerant mechanical adapter, strict local model |
| Page ranges cause full retry | normalize integer/string/range forms before deciding to retry |
| One malformed option poisons exam | invalidate one item; batch/checkpoint prevents full 30-question restart |
| Correct-letter pattern leaks | deterministic whole-option balancing after filtering |
| Batch duplicate questions | conservative global dedup; no automatic top-up |
| Output becomes verbose | stem/options/explanations have frozen sentence limits |
| Calculation error repeats source error | numerical calculation excluded; semantic warning/Judge check |
| Judge adds latency/cost | default off; explicit flag and finite derived ceiling |
| Legacy artifacts break | immutable contract version and legacy replay path |
| Topic default produces repeated variants | topic requires explicit question count |
| Retry/call growth | ≤3 Generator dispatches per batch; finite Judge ceiling |
| Resume scope expands product UX | checkpoint now; public resume explicitly deferred |

## 11. Non-goals for this plan

Do not add while implementing MCQ v1:

- automatic question-count estimation from vectors/clusters;
- mixed exam sections or difficulty allocation;
- numerical calculation validation;
- negative/EXCEPT questions;
- multiple citations for one option;
- distractor relevance scoring or guaranteed misconception mining;
- automatic semantic repair or model-written replacement after Judge rejection;
- top-up calls after drops/dedup;
- public production resume workflow;
- default automatic Judge execution;
- second generation/Judge provider adapters;
- embedding or reranker redesign; or
- rewriting saved historical runs.

## 12. Completion record

Implementation and live-run record (2026-11-04):

- worktree implementation is intentionally uncommitted/unpushed in this task;
- default offline suite: `800 passed`;
- Ruff: `All checks passed!`;
- `git diff --check`: passed;
- controlled fixture loader: `mcq-v1`, four cases, prepare remains offline;
- legacy short-answer trace/replay regression remains green;
- MCQ generation and Judge replay reconstruct raw events and final publications;
- authorized Generator Gate A completed with four isolated cases, four provider
  calls from a ceiling of eight, and retained counts `0/1/2/3`;
- every Gate A child trace completed offline replay; no reranker, embedding, or
  Judge API was called by this gate;
- 2:1 versus 3:1 winner: **not decided**;
- five-question and ratio A/B gates: **not started**;
- Gate A recorded 10,743 tokens and 37.14 seconds across its four calls; monetary
  cost was not available from the provider metadata;
- an independent item-level semantic audit accepted all six retained controlled
  questions; easier distractors, some repetitive feedback, and D-heavy
  cross-child answer letters are non-blocking observations, not broad semantic
  calibration;
- a user-authorized out-of-order full simulation generated `30/30` questions in
  three 20-chunk batches with three calls, balanced correct letters `7/7/8/8`,
  44,822 Generator tokens, 157.47 seconds, and complete offline replay;
- its explicit Judge used 31/60 calls, 257,140 tokens, and 1,019.87 seconds;
  29/30 items were evaluated, but item 3 truncated, so the run was `incomplete`
  and no judged publication was produced;
- independent item audit classified the generated set as 19 pass, nine pending,
  and two fail (items 17 and 24); this is failure evidence rather than formal
  Gate D/E acceptance;
- independent review: **approved with no blocking findings** after post-fix verification.

Offline milestones P0–P8 and P9 Gate A are complete. Gates B–C remain **not
started**; exploratory Gate D/E runs remain **incomplete**. See
[MOCK-MCQ-LIVE-GATES.md](../evaluation/MOCK-MCQ-LIVE-GATES.md) for the append-only
live-gate index.

