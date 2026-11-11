# MCQ Judge v2 Implementation Plan

> Version 0.1 · 2026-11-04 · status: **design frozen; implementation not started**

> Deployment decision (2026-11-11): the V2 implementation and recorded P9/P10
> experiments are retained. Daily MCQ judging defaults to V1; the V2 formal
> gate is not being pursued. See the [live-gate record](../evaluation/MOCK-MCQ-LIVE-GATES.md).

The normative contract is
[MCQ-JUDGE-V2-SPEC.md](../specs/MCQ-JUDGE-V2-SPEC.md). If this plan conflicts
with the spec, the spec wins.

## 1. Delivery strategy

Implement v2 as a new protocol lane beside frozen `mcq-judge-v1`. Do not first
"clean up" the v1 builder or replay path: historical reconstruction is a product
requirement, and shared refactoring is acceptable only after parity tests prove
the recorded v1 request is unchanged.

Target sequence:

```text
freeze v1 fixtures
  -> add protocol registry/digest
  -> add v2 schema and request builder
  -> share a versioned retry state machine
  -> add strict v2 replay
  -> add non-publishing selective calibration
  -> offline independent review
  -> user-authorized capability/calibration calls
  -> full 30-item same-protocol v2 Judge
```

No milestone implicitly authorizes a provider call, commit or push.

## 2. Milestones

### P0 — baseline and historical lock

- Capture compact deterministic v1 fixtures for `prepared`, `completed` and
  `incomplete` judgments.
- Include the historical v1 behavior where `finish_reason=length` is terminal.
- Assert the exact v1 request messages, JSON-object response format, manifest
  envelope and replay result before any v2 refactor.
- Record the current 800-test baseline without copying ignored live bundles into
  the repository.

### P1 — red contract tests

Write failing tests for all J2 requirements before implementation. Tests use
fake clients and temporary recordings only; network access is forbidden.

### P2 — protocol registry and digest

- Add immutable v1/v2 constants, supported registry and current-write identity.
- Split or freeze the v1 request builder.
- Dispatch writer/replay by recorded protocol.
- Add the exact v2 manifest envelope and recomputable protocol digest.
- Include message/payload serialization, rubric/schema/response envelope,
  reasoning/provider policy, temperature/stream, token/retry schedule and usage
  normalization in that digest.
- Reject unknown versions, mutated rubrics/schemas/retry identities and extra
  manifest fields.

### P3 — v2 response and provider request

- Add the exact A–D `options_material_derived` field.
- Keep citation and material-derivation judgments separate in schema, rubric,
  validation and mandatory aggregation.
- Add strict JSON Schema output, reasoning disabled, parameter enforcement and
  the 8,192/16,384 output-token schedule.
- Add a dedicated strict v2 settings boundary that reads shared credential,
  model, base URL, timeout and maximum-input values while keeping attempts,
  temperature and output limits as protocol constants.
- Keep `JUDGE_MAX_OUTPUT_TOKENS`, `JUDGE_MAX_ATTEMPTS` and
  `JUDGE_TEMPERATURE` explicitly legacy/v1-only and show the effective v2
  schedule during preflight.
- Extend the safe numeric usage allowlist with reasoning and visible completion
  counters; never retain reasoning text.

### P4 — shared finite attempt state machine

- Implement one pure protocol-aware attempt classifier.
- Make the v2 writer retry `length`, schema-invalid and retryable transport
  results only while an attempt remains.
- Preserve v1's historical `length` behavior.
- Enforce success/refusal/non-transient/interruption terminal rules and the
  derived ceiling.

### P5 — v2 replay

- Rebuild requests, retry decisions, reviews, rows, metrics and queue from raw
  records.
- Validate exact envelopes, numeric types, time/order/call identities, source
  identity, safe errors and allowlisted usage.
- Rebuild formal publications byte-for-byte when one exists.
- Prove v1 and legacy short-answer Judge replay remain unchanged.

### P6 — selective calibration command

Add an explicit command equivalent to:

```console
overfit eval judge-review GENERATION_RUN \
  --base-judgment V1_JUDGMENT \
  --items 3,4,7,14,15,17,18,19,20,22,24,27,28,29 \
  --execute
```

Requirements:

- no default or inferred item selection;
- exact, sorted, unique 1-based indices;
- source and base snapshots bound before client construction;
- derived call ceiling `selected_count * 2`;
- append-only independent calibration record;
- explicit `no publication` in CLI/report/manifest;
- replay reconstructs all selected rows; and
- no v1 row is copied into a v2 acceptance result.

### P7 — reporting and UX

- Replace "all three dimensions" with "every required dimension".
- Report reasoning/visible tokens when available without claiming unavailable
  values.
- Separate technical errors, mandatory failures and human-review pending counts.
- Clearly distinguish `calibration`, `formal complete`, `formal incomplete` and
  `prepared` statuses.

### P8 — offline acceptance and independent review

- Full pytest and Ruff.
- `git diff --check`, Markdown links, JSON fixtures and secret/path scans.
- Replay/tamper mutation suite.
- Separate author and independent reviewer passes.
- No real network observed in the default suite.

### P9 — user-authorized capability gate

Use exactly items 3 and 5 from frozen generation run
`e7b0d033-b90e-4399-a8ab-0232635410e2`:

- item 3 is the recorded v1 `finish_reason=length` case and tests bounded
  recovery without hidden reasoning exhausting visible output;
- item 5 required a v1 schema retry and tests strict structured output.

The selection is explicit and non-publishing. With two attempts per item, its
derived hard ceiling is four calls. It proves that the configured endpoint
honours reasoning disablement, strict schema, parameter enforcement and
`max_completion_tokens`; it is not a semantic quality gate.

Hard stop on:

- ignored required parameters;
- unexpected reasoning exhaustion;
- unsupported strict schema;
- unresolved call or replay mismatch; or
- any secret/private provider metadata in artifacts.

### P10 — user-authorized 14-item calibration

Run the frozen anomaly union:

```text
3,4,7,14,15,17,18,19,20,22,24,27,28,29
```

Maximum calls: 28. This gate is not publishable. Compare v2 with the independent
audit and the saved v1 conclusions:

- item 3 should recover from truncation or fail only after its second attempt;
- items 14/19/27 should not fail merely because false distractor text is absent;
- items 18/20/22/28/29 should exercise material-derived detection;
- items 17/24 should remain rejected or require human review; and
- no response may bypass replay.

### P11 — user-authorized full formal v2 Judge

Only after P9–P10 pass, run all 30 generated items under one v2 protocol. Do not
reuse v1 rows. Replay the finished judgment and inspect every pending/rejected
item before describing the result as publishable.

## 3. TDD matrix

### 3.1 Protocol and compatibility

- v1 builder output remains exact after registry introduction;
- v1 `length` terminal recording replays;
- v1 completed/incomplete/prepared records replay;
- v2 manifest has exact fields and correct digest;
- unknown protocol, wrong digest or extra fields fail;
- changing any message/payload serialization, rubric, schema/response envelope,
  reasoning/provider setting, temperature/stream rule, retry/token schedule or
  usage policy changes the digest;
- a frozen v2 builder continues to reconstruct its historical request exactly
  after a later protocol is added;
- legacy non-MCQ Judge replay remains green.

### 3.2 V2 schema and semantics

- every v2 field required, strict and extra-forbidden;
- all option maps require exact A–D keys;
- material-derived is independent from citation support;
- false distractor absence alone does not make citation support false;
- a distractor introduced from outside supplied material fails material-derived;
- every v2 mandatory boolean participates in `mandatory_pass`;
- human-review pending never publishes.

### 3.3 Provider request

- attempt 1 uses 8,192 and attempt 2 uses 16,384 completion tokens;
- reasoning is disabled and hidden reasoning is never recorded;
- response format is strict JSON Schema;
- provider parameter enforcement is present;
- request identity is deterministic and replayed exactly;
- legacy output-token/attempt/temperature settings cannot alter a v2 request;
- v2 preflight reports attempts `2` and schedule `8192 -> 16384`, labels legacy
  tuning names as not applied, and never prints their values;
- shared model/base URL/timeout/input fields reject type confusion and unsafe
  values before client construction;
- unknown or conflicting v2 config/CLI policy overrides fail rather than being
  ignored;
- arbitrary provider metadata, headers and secret-like fields are rejected.

### 3.4 Retry behavior

- v2 `length -> valid stop` uses two calls and completes;
- v2 `length -> length` exhausts at two and is incomplete;
- a missing required second call fails replay;
- a third call or a call after success fails replay;
- schema invalid and retryable transport errors retry within the same ceiling;
- refusal and non-retryable errors do not retry;
- interruption stops later items and prevents publication;
- v1 `length` still does not require a retry.

### 3.5 Calibration

- empty, duplicated, unsorted, zero, negative, float and out-of-range indices
  fail before settings/client construction;
- selected indices and ceiling are type-sensitive and exact;
- calls for unselected items fail replay;
- base/source mutations fail before execution and during replay;
- calibration metrics/queue/report are rebuilt from raw calls;
- calibration never writes judged paper/answers, including when every item passes;
- two independent calibration runs never overwrite each other.
- the P9 capability fixture accepts only frozen items `3,5`, derives ceiling
  four, and binds generation run `e7b0d033-b90e-4399-a8ab-0232635410e2` by
  recorded identity rather than a machine-local path.

### 3.6 Formal publication

- only a technically complete full v2 run can publish;
- failed/pending items are atomically excluded;
- accepted items are rebalanced after review;
- formal artifacts are byte-for-byte replayed;
- no v1/v2 mixed row set can be labelled a v2 publication.

## 4. Proposed implementation map

| Path | Responsibility |
| --- | --- |
| `src/overfit/evaluation/judge.py` | protocol registry, frozen builders, v2 schema/rubric, request identities and safe usage |
| `src/overfit/evaluation/runner.py` | versioned writer/state machine, v2 replay, calibration lifecycle and publication boundary |
| `src/overfit/cli.py` | `judge-review`, explicit execution and accurate summary language |
| `tests/test_mcq_judge.py` | v1 preservation, v2 requests/retries/replay/publication |
| new focused calibration tests | index parsing, no-publication lifecycle and tamper rejection |
| `docs/evaluation/MOCK-MCQ-LIVE-GATES.md` | append-only authorized v2 gate outcomes after execution |

Module names may change to keep the runner small, but responsibilities and
version boundaries may not be collapsed.

## 5. Live evaluation interpretation

Calibration is a prompt/protocol diagnostic, not a quality score. Compare:

1. technical completion and strict schema compliance;
2. independent audit agreement on known failures;
3. false-negative reduction for correction-citation semantics;
4. detection of distractors not derived from supplied material;
5. pending-review rate;
6. calls, latency, reasoning/visible tokens and estimated cost.

A lower rejection rate is not automatically better. V2 must still reject or
escalate unsupported questions such as the unanswered-quiz and contradictory
extraction cases.

## 6. Stop conditions

Stop the current phase and do not advance automatically on:

- any v1 replay regression;
- protocol digest or request reconstruction mismatch;
- a call beyond the derived ceiling;
- required provider parameters being ignored;
- a technical error without a recorded terminal state;
- calibration writing publication artifacts;
- a secret, hidden reasoning or private provider metadata leak;
- a response/source/base identity mismatch; or
- independent review finding an unresolved blocker.

Near the live gates, an incomplete result remains evidence, not permission to
raise budgets or rerun automatically.

## 7. Completion record

Planning record on 2026-11-04:

- design decisions are frozen in the companion spec;
- implementation and TDD have not started;
- no v2 API call has occurred;
- the anomaly set and maximum calibration ceiling are fixed;
- no provider price is frozen; and
- the incomplete v1 judgment and its replay remain immutable evidence.
