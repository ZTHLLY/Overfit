# Mock MCQ v1 Spec

> Version 0.1 · 2026-11-04 · status: **contract frozen; Gate A complete; exploratory full run recorded**
>
> This document defines the implemented MCQ v1 Generator contract. Offline code,
> fixtures, tests, trace/replay, and explicit Judge plumbing are complete. The
> controlled four-case Generator Gate A has run against the configured provider.
> A separately authorized out-of-order 30-question generation completed, but its
> Judge run was incomplete; formal Gates B–E and any broader semantic acceptance
> claim remain outstanding.

Implementation order is defined in
[MOCK-MCQ-PLAN.md](../plans/MOCK-MCQ-PLAN.md). The implementation remains
governed by this frozen contract. Historical short-answer trace/replay and Judge
records retain their legacy read contracts.

## 1. Purpose

Replace newly generated short-answer mock exams with grounded, single-best-answer
multiple-choice questions:

```text
course material
  → coverage/topic selection
  → MCQ Generator
  → deterministic structure/citation validation
  → optional independent MCQ Judge
  → question paper + explanatory answer key
```

Each new question must contain exactly four options labelled A–D, exactly one
declared correct option, concise feedback for all four options, and one
course-material citation per feedback item. The question paper is optimized for
taking the exam; the answer key is optimized for learning from every option.

This work restores the original product direction that was simplified during the
local-model MVP. It is a new formal contract: the repository has never shipped an
A–D Generator schema before.

## 2. Product decisions

### 2.1 New output is MCQ-only

After the migration:

- every newly generated `mock` item uses `generation_contract=mcq-v1`;
- `mock` exposes no short-answer generation switch;
- historical short-answer traces and Judge artifacts remain readable and replayable;
- the default no-topic request remains 30 questions;
- fewer valid questions are an honest result and are not automatically replaced.

The old short-answer format remains a legacy read contract, not a new write path.

### 2.2 Whole-course and topic behavior

| Invocation | Question count | Material behavior |
| --- | --- | --- |
| `mock --course COURSE` | default 30 unless explicitly overridden | whole-course coverage, generated in batches |
| `mock --course COURSE --questions N` | explicit N | whole-course coverage, generated in batches |
| `mock --course COURSE --topic TEXT` | invalid: `--questions` is required | fail before provider/key/index work |
| `mock --course COURSE --topic TEXT --questions N` | explicit N | dense Top 40 → rerank → topic material selection |

The topic rule avoids silently asking for 30 variants from the current default of
at most 10 topic passages. An explicit question count always represents user
intent; the Generator may still return fewer.

### 2.3 Judge is explicit and additional

Ordinary `mock` does not call a Judge model. Its terminal/report status must say:

```text
MCQ structure validated
Semantic uniqueness and distractor correctness: not judged
```

`mock --judge` explicitly adds independent Judge model calls after local
validation. Judge does not generate or rewrite questions. It only reviews and
gates them.

## 3. Provider wire contract

The Generator continues to request portable `json_object` output; it does not
reintroduce provider `json_schema`. The top-level wire envelope remains:

```json
{"questions": []}
```

A non-empty item has this logical shape:

```json
{
  "topic": "Overfitting",
  "question": "Which statement best describes overfitting?",
  "options": {
    "A": {
      "text": "The model is too simple to fit its training data.",
      "explanation": "Incorrect. This describes underfitting; overfitting instead learns training-specific patterns that generalise poorly.",
      "source": "week3/supervised_ml_basic.pdf",
      "page": 17,
      "page_end": null
    },
    "B": {
      "text": "The model learns training-specific patterns that do not generalise.",
      "explanation": "Correct. Overfitting can produce strong training performance but weak performance on unseen data.",
      "source": "week3/supervised_ml_basic.pdf",
      "page": 17,
      "page_end": null
    },
    "C": {
      "text": "The model necessarily has high bias and low variance.",
      "explanation": "Incorrect. High bias and low variance are associated with underfitting; overfitting is associated with high variance.",
      "source": "week4/tutorial_solutions.pdf",
      "page": 4,
      "page_end": null
    },
    "D": {
      "text": "The model performs equally well on all datasets.",
      "explanation": "Incorrect. Overfitting is revealed by a gap between training performance and performance on unseen data.",
      "source": "week4/practical_exercises.pdf",
      "page": 4,
      "page_end": null
    }
  },
  "correct_option": "B"
}
```

### 3.1 Exact structural rules

- `options` has exactly the keys `A`, `B`, `C`, and `D`.
- Each canonical option has exactly the fields `text`, `explanation`, `source`,
  `page`, and `page_end`: the text fields are non-empty, `page` is 1-based, and
  `page_end` is either a canonical integer or `null`.
- `correct_option` is exactly one of `A|B|C|D`.
- Option texts are distinct after Unicode, case, and whitespace normalization.
- An item may not contain `All of the above` or `None of the above`.
- The provider does not duplicate top-level `answer/source/page`. Compatibility
  projections are derived locally from the correct option.
- No `difficulty` field is part of MCQ v1.

### 3.2 Meaning of option citations

Each option has one citation. For the correct option it supports the correct
answer and explanation. For an incorrect option it supports the corrected
knowledge in the explanation; it does **not** claim that the distractor text is
true.

A question and its correct answer focus on one knowledge unit. Incorrect options
may draw on any supplied course material. They do not have to be highly
confusable or belong to the same narrow topic, but they must:

- use concepts available in the supplied material;
- be unambiguously wrong for the question;
- not create a second acceptable answer; and
- have an explanation that states what is wrong and what the correct knowledge is.

MCQ v1 does not classify distractors as misconception/related/unrelated and does
not enforce a relevance quota.

### 3.3 Concision and question style

- Question stems are normally 1–3 sentences.
- Option text is normally one sentence and at most two sentences.
- A correct-option explanation is brief, normally 1–2 sentences.
- Any option explanation is at most four sentences.
- Stems are positive single-best-answer questions. `NOT`, `EXCEPT`, “which
  statement is false”, and similar reversal games are prohibited.
- Numerical calculation questions are prohibited in MCQ v1.
- Formula meaning, variable interpretation, and qualitative trend questions are
  allowed when no numerical calculation is required.
- If the material cannot support a clear item within these limits, the model
  returns fewer items.

## 4. Tolerant wire input, strict internal form

Provider formatting variation must not trigger an expensive semantic regeneration
when it can be normalized without changing meaning.

### 4.1 Allowed mechanical normalization

The boundary adapter may normalize:

```text
"page": 17                         → page=17, page_end=None
"page": "17"                       → page=17, page_end=None
"page": "17-18"                    → page=17, page_end=18
"page": 17, "page_end": 18         → unchanged canonical integers
"a" / "b" / "c" / "d" labels       → A / B / C / D
"A. option text" inside option A   → remove the duplicate "A." prefix
surrounding whitespace             → trim
```

Normalization succeeds only when the canonical citation matches material actually
supplied to the Generator. `page_end < page`, non-numeric ranges, unknown sources,
or pages outside supplied material fail.

### 4.2 Forbidden semantic repair

The application never automatically changes:

- which option is correct;
- option meaning;
- an explanation’s factual conclusion;
- the source document chosen by the model;
- an ambiguous or multi-correct question; or
- a computation result.

A critical failure in any option, explanation, or citation invalidates the whole
question. The program does not hide one broken option and publish the remaining
three.

## 5. Batch generation and checkpoint lifecycle

### 5.1 Batch size

- At most 10 requested questions enter one Generator batch.
- The default 30-question whole-course mock therefore plans three batches.
- Each batch receives a bounded material set.
- The initial material-ratio experiment compares 20 chunks for 10 questions
  (2:1) with 30 chunks for 10 questions (3:1).
- The default ratio is not frozen until the controlled A/B gate in section 10.

### 5.2 Attempts and failure

- A batch permits at most three Generator provider dispatches.
- Every dispatch, including a response repaired through another model call, counts.
- Call intent is recorded before dispatch.
- Three unsuccessful attempts stop the run fail-closed; later batches do not start.
- Completed batches remain in the checkpoint/trace, but an incomplete run does not
  publish a normal complete exam.
- Checkpoint data is designed for development and evaluation recovery. Public
  `mock --resume` UX is not a v1 delivery requirement.
- Real provider calls occur only after explicit user initiation.

### 5.3 Fewer items, deduplication, and no top-up

A batch may honestly return fewer than requested. After all complete batches:

1. validate and atomically retain only complete MCQ items;
2. remove clear cross-batch duplicates that test the same knowledge and have
   materially equivalent correct answers;
3. keep related questions that test different abilities or evidence;
4. prefer the item with more complete evidence/feedback when duplicates compete;
5. do not make additional calls merely to restore the requested count.

Deduplication is conservative. Vector similarity alone cannot delete an item.

## 6. Deterministic option balance and presentation

After validation, optional Judge gating, and cross-batch deduplication, the
application deterministically reorders whole option objects so that:

- option text, explanation, and citation always move together;
- `correct_option` is updated from the same mapping;
- among retained questions, A–D correct-answer counts differ by at most one where
  mathematically possible; and
- the before/after mapping and stable seed identity are traceable.

The question paper:

- numbers questions continuously;
- displays only the stem and A–D option text;
- hides topic headings and citations; and
- uses a deterministic mixed order rather than grouping by topic.

The answer key:

- identifies the correct letter and text;
- gives the concise correct explanation;
- explains each incorrect option and states the corrected knowledge;
- shows each option’s own citation; and
- retains and shows the topic for study/review.

## 7. Local validation

Local validation is deterministic and does not claim semantic correctness. It
checks at least:

- exact A–D keys and legal `correct_option`;
- unique, non-empty option texts;
- required feedback and sentence/length boundaries;
- prohibited all/none and negative-stem patterns;
- canonical, supplied source/page ranges for every option;
- no unsupported wrapper fields after normalization;
- batch and retained counts;
- complete trace events before publishing; and
- correct movement of option feedback/citation during balancing.

Calculation-language detection may produce a warning, but no brittle regex may
silently delete a formula-interpretation question. Semantic confirmation that a
question is non-calculational belongs to the optional Judge.

## 8. Optional MCQ Judge

### 8.1 Input and role

The MCQ Judge sees only the selected question, all four option objects,
`correct_option`, their explanations/citations, and the original generation
materials. It does not see fixture annotations, expected outcomes, sibling cases,
or private reasoning.

It reviews:

1. whether the stem is answerable from the supplied material;
2. whether the declared correct option is supported and best;
3. whether any other option could also be accepted;
4. whether each incorrect option is actually incorrect;
5. whether every explanation gives accurate corrected knowledge;
6. whether each citation supports its explanation/correction; and
7. whether the item violates the no-calculation or positive-stem contract.

Distractor topical relevance is not a pass/fail dimension in MCQ v1.

### 8.2 Publication semantics

- `--judge` is the explicit authorization boundary for Judge calls.
- A finite internal hard ceiling is derived from retained item count, planned
  phases, and configured attempt limits; v1 does not require a
  `--judge-max-calls` option.
- The next dispatch is rejected before exceeding the derived ceiling.
- An item must pass all mandatory MCQ dimensions before judged publication.
- Any failed mandatory dimension removes the whole item; there is no partial
  option repair.
- `human_review_pending` items remain in trace/review artifacts but are not
  published in the formal judged exam.
- Timeout, invalid response, budget exhaustion, recording failure, or incomplete
  phases makes the judged run `incomplete`; unevaluated items are never treated
  as passed.
- Ordinary unjudged output remains usable but explicitly says semantic uniqueness
  and distractor correctness were not evaluated.

## 9. Trace, replay, and legacy compatibility

New traces record `generation_contract=mcq-v1` in immutable run identity. They
must preserve:

- planned batches and material identities;
- provider call intent/finish/validation events;
- raw response hash and normalized item representation;
- mechanical normalization actions;
- per-item citation checks;
- deduplication decisions;
- pre/post option-balance mappings;
- Judge selection/status when explicitly enabled; and
- final artifact hashes and terminal completeness.

Historical short-answer traces retain their historical contract and must continue
to pass offline replay and existing Judge/replay compatibility tests. Replay may
dispatch by recorded contract version; it must never invent A–D options for a
legacy item. Old records are not rewritten.

Secrets, authorization headers, private provider metadata, and environment values
never enter trace, reports, fixtures, exceptions, or cache keys.

## 10. Material-ratio A/B gate

The 2:1 and 3:1 batch material ratios are compared on the same controlled inputs,
model configuration, prompt version, and requested question counts. Selection
order is frozen per track.

The decision order is:

1. proportion judged as having one best answer;
2. proportion with all four feedback explanations supported;
3. cross-batch duplicate rate;
4. retained valid question count;
5. if quality is materially tied, lower token/call cost wins.

No ratio is declared superior from one anecdotal output. The chosen default and
evidence are recorded in the plan/report after authorized testing; the losing
ratio remains an explicit evaluation track rather than an invisible fallback.

## 11. Failure and security boundary

- Missing generation/Judge configuration fails before network dispatch.
- Provider SDK implicit retries remain disabled; project counters are authoritative.
- Generator and Judge call ceilings are finite and recorded.
- Shutdown/interrupt propagates immediately and records an interrupted state.
- No failed/incomplete run is presented as complete.
- Real APIs are never called by default tests, CI, documentation generation,
  prepare mode, or an agent without the user explicitly initiating that live gate.
- Only minimal question/material content required by each provider is sent.
- API keys are accepted from the existing protected configuration/CLI paths and are
  never printed or persisted.

## 12. Acceptance requirements

| ID | Requirement |
| --- | --- |
| MCQ-01 | All newly generated items use `mcq-v1`; no new short-answer write path exists. |
| MCQ-02 | Every item has exact A–D option objects and one legal `correct_option`. |
| MCQ-03 | Every option has concise feedback and one valid supplied citation. |
| MCQ-04 | Wire page variants normalize locally; semantic fields are never auto-repaired. |
| MCQ-05 | No-topic defaults to 30; topic requires explicit `--questions`. |
| MCQ-06 | Batches request at most 10 questions and attempt at most three provider calls. |
| MCQ-07 | Batch failure is fail-closed and checkpointed; no incomplete exam is published. |
| MCQ-08 | Conservative cross-batch dedup removes clear duplicates and never tops up. |
| MCQ-09 | Final A–D correct-answer counts differ by at most one where possible. |
| MCQ-10 | Question output hides topics; answer output includes per-option feedback/citations. |
| MCQ-11 | Negative stems, all/none options, numerical calculation, and difficulty metadata are absent. |
| MCQ-12 | Ordinary mock explicitly reports “not judged”; `--judge` is an additional finite gated review. |
| MCQ-13 | Judged publication excludes failures and human-review-pending items. |
| MCQ-14 | Legacy short-answer trace/replay behavior remains readable without synthetic options. |
| MCQ-15 | 2:1 versus 3:1 is decided by the frozen quality-first metric order. |
| MCQ-16 | Default tests are offline, secrets are absent, and real calls require explicit user initiation. |

## 13. Non-goals

MCQ v1 does not implement:

- mixed short-answer/applied/MCQ generation;
- a short-answer compatibility generation switch;
- automatic semantic question-count planning;
- numerical calculation or deterministic mathematics verification;
- difficulty labeling or difficulty quotas;
- negative/EXCEPT question styles;
- multiple citations for one option feedback;
- guaranteed high-confusability distractors or distractor relevance scoring;
- automatic semantic repair;
- public production-grade resume UX;
- automatic Judge calls in ordinary `mock`;
- embedding/reranker replacement; or
- rewriting historical artifacts.

