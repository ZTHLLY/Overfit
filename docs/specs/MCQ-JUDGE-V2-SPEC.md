# MCQ Judge v2 Spec

> Version 0.1 · 2026-11-04 · status: **design frozen; implementation not started**
>
> This contract upgrades only the independent MCQ Judge. It does not change the
> `mcq-v1` Generator schema, rewrite any saved run, or authorize a provider call.

> Deployment decision (2026-11-11): V1 is the default for new daily MCQ
> judgments. V2 remains implemented for explicit experiments and offline
> replay. This supersedes the default-write migration in §3 and J2-01/J2-02;
> recorded protocol contracts are unchanged.

Implementation order and live gates are defined in
[MCQ-JUDGE-V2-PLAN.md](../plans/MCQ-JUDGE-V2-PLAN.md). The Generator contract
remains [MOCK-MCQ-SPEC.md](MOCK-MCQ-SPEC.md).

## 1. Why v2 exists

The authorized 30-question exploratory run exposed three separate issues:

1. one Judge response exhausted 4,096 output tokens on hidden reasoning,
   returned `finish_reason=length` with no visible content, and did not spend its
   remaining retry;
2. the v1 rubric sometimes treated a deliberately false distractor as something
   its citation had to support, although MCQ v1 requires that citation to support
   the corrective explanation; and
3. v1 has no independent dimension for whether each distractor was derived from
   supplied course material.

The run also proved that a Judge protocol cannot be changed in place. Replay
must reconstruct the exact prompt, schema, retry rules and provider request that
created a saved judgment. V2 is therefore a new immutable protocol, not a patch
to `mcq-judge-v1`.

## 2. Scope and exclusions

V2 delivers:

- immutable v1 and v2 protocol dispatch;
- strict structured Judge output;
- bounded recovery from truncated responses;
- separate citation-support and material-derived judgments;
- reasoning/output-token accounting without retaining hidden reasoning;
- a non-publishing selective calibration workflow; and
- a later full-run, same-protocol publication gate.

V2 does **not** deliver:

- a mixed v1/v2 judged publication;
- a publishable targeted resume;
- resume-of-resume or in-place mutation of a judgment directory;
- automatic question rewriting;
- automatic acceptance of pending human-review items;
- concurrent provider calls; or
- any claim that the Judge is human-calibrated before the live gates pass.

## 3. Protocol registry and migration

The implementation must expose stable identities equivalent to:

```text
mcq-judge-v1  -> frozen v1 request builder, schema, retry and replay
mcq-judge-v2  -> frozen v2 request builder, schema, retry and replay
```

New MCQ Judge writes use `mcq-judge-v2`. Replay dispatches only from the
recorded `protocol_version`; it must never infer a protocol from fields or use a
single mutable "current" builder for historical requests.

### 3.1 V1 preservation

- The current v1 prompt, JSON-object response request and retry behavior remain
  byte-for-byte reconstructable.
- V1 `finish_reason=length` remains terminal because that is what historical
  v1 writers recorded.
- V1 manifests are validated against their historical exact field set; a v2
  digest is not retrofitted.
- Existing v1 `complete`, `incomplete` and `prepared` judgments remain replayable.

### 3.2 V2 protocol digest

Every v2 manifest contains an exact `protocol_digest`. It is the canonical
SHA-256 identity of:

- `protocol_version`;
- system/user message composition and canonical serialization rules;
- the complete user-payload shape and v2 system rubric;
- the review schema and complete strict `response_format` envelope;
- reasoning configuration and `provider.require_parameters` policy;
- temperature and stream policy;
- the retry-policy identity and per-attempt output-token schedule; and
- usage normalization, derivation and allowlist rules.

The digest identifies the complete deterministic request/review policy, not
only its prose prompt. A change to any component above changes the digest and
requires a new immutable protocol registry entry; an existing v2 builder must
continue to reconstruct its historical request byte-for-byte.

Run-specific values are not protocol policy: model, safe base URL, timeout,
maximum input characters and other explicitly shared runtime identities remain
bound by the recorded strict config and exact per-call request. Credentials are
never recorded or hashed.

Replay recomputes this digest from the recorded version's immutable registry.
Unknown versions or digest mismatches fail closed.

## 4. V2 review contract

The v2 response retains all v1 mandatory dimensions and adds one exact A–D map:

```json
{
  "human_review_required": false,
  "answerable": true,
  "one_best_answer": true,
  "correct_option_supported": true,
  "other_options_incorrect": {"A": false, "B": true, "C": true, "D": true},
  "explanations_accurate": {"A": true, "B": true, "C": true, "D": true},
  "citations_supported": {"A": true, "B": true, "C": true, "D": true},
  "options_material_derived": {"A": true, "B": true, "C": true, "D": true},
  "positive_stem": true,
  "no_calculation": true,
  "reason": "Short decision rationale."
}
```

All fields are strict and required; extra fields are forbidden. Each map has
exactly `A`, `B`, `C`, and `D`.

### 4.1 Citation semantics

For the correct option, its citation must support the answer and explanation.
For an incorrect option, its citation must support the **corrective knowledge in
the explanation**. The material is not expected to contain or support the false
distractor statement itself. A Judge must not set `citations_supported[label]`
to false solely because the distractor text is absent from the material.

### 4.2 Material-derived semantics

`options_material_derived[label]` answers a separate question: are the option's
concepts grounded in supplied material, even when they are deliberately
reversed, misapplied or combined into a false statement?

- Verbatim presence is not required.
- A generic outside concept introduced only to make an easy distractor is false.
- A correction supported by a citation does not automatically prove that the
  distractor itself is material-derived.
- Citation support and material derivation may therefore differ.

### 4.3 Mandatory pass

Formal acceptance requires every boolean mandatory dimension to pass, including
all four `options_material_derived` and `citations_supported` values. The
declared correct option must be false in `other_options_incorrect`; each other
option must be true. `human_review_required=true` always routes the item to the
review queue and excludes it from publication.

## 5. Provider request contract

The first v2 provider implementation remains the configured OpenRouter/Qwen
adapter. Each request uses:

```text
max_completion_tokens = 8192 on attempt 1
max_completion_tokens = 16384 on attempt 2
reasoning.enabled      = false
response_format        = strict json_schema
provider.require_parameters = true
temperature            = 0
stream                 = false
```

The strict JSON Schema is derived from the frozen v2 response model. The
request records the complete visible messages, response format, reasoning
configuration, provider constraint and per-attempt token limit. Credentials,
authorization headers and hidden reasoning are never recorded.

### 5.1 V2 settings boundary

V2 protocol behavior is defined by immutable code constants, not legacy tuning
settings:

```text
max_attempts                 = 2
max_completion_tokens       = [8192, 16384]
temperature                  = 0
stream                       = false
reasoning.enabled            = false
provider.require_parameters  = true
response_format              = frozen strict JSON Schema envelope
```

`JUDGE_MAX_OUTPUT_TOKENS`, `JUDGE_MAX_ATTEMPTS`, and `JUDGE_TEMPERATURE` remain
legacy/v1 settings only. They may coexist in `.env` so historical commands keep
working, but they cannot override, clamp or otherwise influence a v2 request.
V2 preflight must explicitly display the frozen attempt/token schedule and label
those names as legacy-only rather than silently appearing to honour them.

V2 reads only the shared runtime fields needed to reach the configured Judge:
credential, model, base URL, timeout and maximum input characters. These fields
use strict types and existing safe endpoint/secret validation. A dedicated v2
settings/config boundary accepts exactly its declared shared fields; booleans as
numbers, coercive numeric strings where strict values are required, conflicting
v2 policy overrides, and unknown v2 CLI/config fields fail before client
construction. Unrelated process environment variables are not v2 config and do
not fail merely because they exist.

The configuration is based on current official capabilities:

- [OpenRouter reasoning-token guidance](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [Qwen3.7 Plus model page](https://openrouter.ai/qwen/qwen3.7-plus-20260602)
- [Qwen3.7 Plus endpoint metadata](https://openrouter.ai/api/v1/models/qwen/qwen3.7-plus/endpoints)
- [OpenRouter Chat Completions API](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion)
- [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)

Provider documentation can change. A future request change requires another
protocol version; it cannot silently alter v2.

## 6. Finite retry state machine

`max_attempts` remains two. For `N` selected items, the hard ceiling is exactly
`N * 2`; fewer calls are normal when the first attempt succeeds or a terminal
non-retryable result occurs.

| Attempt result | Attempt remains | Required action |
| --- | --- | --- |
| valid `stop` response | either | evaluate and stop this item |
| schema-invalid `stop` response | yes | retry |
| schema-invalid `stop` response | no | terminal schema error |
| `finish_reason=length` | yes | retry with the recorded 16,384-token request |
| `finish_reason=length` | no | terminal truncated response |
| retryable transport/network error | yes | retry |
| retryable transport/network error | no | terminal technical error |
| refusal | either | terminal refusal; do not retry |
| non-retryable transport/configuration error | either | terminal error |
| interruption | either | stop the whole run; no publication |

Writer and replay use one pure disposition function keyed by protocol version.
Replay rejects a missing required retry, a retry after a terminal result, an
extra attempt after success, an attempt beyond the ceiling, or item-order
regression.

## 7. Usage, privacy and cost

Only finite numeric usage fields on an allowlist may be recorded:

```json
{
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "reasoning_tokens": 0,
  "visible_completion_tokens": 0,
  "total_tokens": 0
}
```

`visible_completion_tokens` is derived when the provider exposes reasoning-token
details. Hidden reasoning text, arbitrary provider metadata, headers, account
identifiers and secret-derived hashes are prohibited.

Reports may estimate cost only from recorded usage and a separately recorded
price snapshot or explicit user-supplied rates. The spec freezes no provider
price and makes no billing claim from model-page prices alone.

## 8. Selective calibration, not resume

V2 first exposes a non-publishing calibration command:

```console
overfit eval judge-review GENERATION_RUN \
  --base-judgment V1_JUDGMENT \
  --items 3,4,7,14,15,17,18,19,20,22,24,27,28,29 \
  --execute
```

The item list is required, 1-based, strictly increasing, unique and within the
generation run. The base judgment is used only as frozen comparison evidence;
its rows are never merged into a v2 publication. For the 14-item calibration,
the derived ceiling is 28 calls.

The calibration writes an independent append-only record containing source and
base identities, selected items, raw visible requests/responses, v2 rows,
metrics, report, review queue and completion hashes. It supports offline replay.
It never writes a question paper or answer key and must display `no publication`.

This workflow is intentionally not called resume. It does not upgrade, replace
or merge any v1 row.

## 9. Formal v2 publication

After calibration passes, a formal Judge run reviews every retained item from
the generation trace under one `mcq-judge-v2` protocol. A formal publication:

- contains no reused v1 decisions;
- requires every selected item to reach a technical terminal evaluation;
- excludes mandatory failures and human-review-pending items;
- rebalances and mixes the accepted items only after review;
- derives metrics, queue, report and artifacts from raw v2 calls; and
- remains absent for interrupted or technically incomplete runs.

Mixed-protocol publication and publishable targeted resume are out of scope.

## 10. Replay and tamper resistance

V2 replay verifies at least:

- exact manifest, tasks, complete and event envelopes;
- strict integer/float types rather than Python-coercive equality;
- source-generation and optional base-judgment artifact hashes;
- protocol version and digest;
- exact selected indices and derived call ceiling;
- exact request builder, per-attempt token schedule and provider parameters;
- call IDs, sequence, item order, attempt order and terminal retry rules;
- strict JSON response reconstruction and every derived row;
- allowlisted usage and safe errors;
- metrics, report, queue and, for a formal run, publication contents/hashes.

Replay performs no settings, index or provider access. Any unknown field,
duplicate JSON key, non-finite number, symlink, identity mismatch or unsupported
protocol fails closed.

## 11. CLI language

MCQ output must not use the legacy phrase "all three dimensions". Shared CLI
summaries use:

```text
Model positive on every required dimension
```

This wording is accurate for both the legacy three-dimension Judge and the
expanded MCQ protocol.

## 12. Acceptance requirements

| ID | Requirement |
| --- | --- |
| J2-01 | V1 writes stop, but every supported v1 recording remains replayable with its frozen builder and behavior. |
| J2-02 | New MCQ Judge writes identify `mcq-judge-v2` and a recomputable protocol digest. |
| J2-03 | The v2 schema separates citation support from A–D material derivation and makes both mandatory. |
| J2-04 | Provider requests use the frozen structured-output, reasoning and token schedule without recording secrets or hidden reasoning. |
| J2-05 | `length`, schema and transient retries follow the finite state machine and exact derived ceiling. |
| J2-06 | Calibration accepts only explicit valid indices, is replayable and can never publish. |
| J2-07 | Formal publication uses one complete v2 protocol over the full item set; v1/v2 rows are never mixed. |
| J2-08 | Replay reconstructs all conclusions and artifacts from raw visible records and rejects tampering. |
| J2-09 | Default tests cannot construct a real provider client or perform network I/O. |
| J2-10 | Live calls begin only after the user explicitly initiates the relevant gate. |
| J2-11 | V2 uses frozen protocol constants; legacy tuning fields cannot alter v2 and the CLI preflight states the effective schedule. |

## 13. Completion boundary

This document freezes design decisions only. As of 2026-11-04:

- implementation and red tests have not started;
- no v2 provider request has been made;
- no calibration item has been rejudged;
- the saved v1 exploratory judgment remains incomplete and unchanged; and
- no formal judged MCQ publication exists.
