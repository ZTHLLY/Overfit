# Generator / Judge Challenge Evidence

> Updated 2026-11-03. This page is an artifact index and concise interpretation;
> it intentionally does not duplicate raw provider responses.

## Scope and boundaries

Two controlled experiments are indexed here:

1. an isolated Generator fixture using ordered, snapshotted chunks; and
2. Judge evaluation of its retained items plus the separate control/mutant Judge suite.

The Generator fixture did **not** open the production index and did not invoke
retrieval or the reranker. Its results therefore measure behavior on supplied
material, not end-to-end retrieval quality. Human annotations were not model-visible.
Costs were not returned for the Generator or Judge runs and are recorded as unknown,
not zero.

## Generator challenge

Parent run:
report (local artifact: `outputs/eval/generator-challenges/9a28bfe3-2b6f-4631-9b97-f1d704817796/report.md`) ·
structured results (local artifact: `outputs/eval/generator-challenges/9a28bfe3-2b6f-4631-9b97-f1d704817796/results.json`) ·
completion marker (local artifact: `outputs/eval/generator-challenges/9a28bfe3-2b6f-4631-9b97-f1d704817796/complete.json`)

| Measure | Result |
| --- | ---: |
| Cases completed | 4/4 |
| Provider calls / hard budget | 8/20 |
| Total tokens | 11,392 |
| Initial `json_schema` provider/fallback errors | 4 |
| Validation errors | 0 |
| Cost | unknown |

Each case made one unsuccessful structured-output request and one successful explicit
fallback request. These provider-format errors consumed budget but did not become
validation failures.

| Case | Observed result | Generation trace | Replay |
| --- | --- | --- | --- |
| unrelated-only | 0 items; `model_empty` | `a78e15d0…` (local artifact: `outputs/eval/a78e15d0-95c1-4d24-9772-a42b5d0cff4d/report.md`) | complete (local artifact: `outputs/eval/a78e15d0-95c1-4d24-9772-a42b5d0cff4d/replays/ff377873-b8c2-48df-9c00-513694c00e8d/report.md`) |
| partial-context | requested 2, retained 1 narrowly supported few-shot item; no fine-tuning/RLHF invention | `693b58d2…` (local artifact: `outputs/eval/693b58d2-6a70-44de-9e7c-01d066ee0647/report.md`) | complete (local artifact: `outputs/eval/693b58d2-6a70-44de-9e7c-01d066ee0647/replays/e35ca232-8d43-43b1-a6fe-be8869877d05/report.md`) |
| gold-hard-negative | retained 2/2; both cite the trusted course chunk, zero distractor citations | `ce08002d…` (local artifact: `outputs/eval/ce08002d-5430-4c37-a5ee-5e5b143ffb27/report.md`) | complete (local artifact: `outputs/eval/ce08002d-5430-4c37-a5ee-5e5b143ffb27/replays/6be234f9-c7ec-4ac0-a461-18318a5cd094/report.md`) |
| multi-source | generated 3/3 single-citation questions; the first was later judged material-supported but citation `partially_supported` | `cd342956…` (local artifact: `outputs/eval/cd342956-55d0-4839-a312-d05c7609511f/report.md`) | complete (local artifact: `outputs/eval/cd342956-55d0-4839-a312-d05c7609511f/replays/fea5668e-ccff-4877-a3f6-bbcaf6bf429a/report.md`) |

All four generation replays completed.

## Judge follow-up on generated items

The three non-empty traces supplied six retained items. Their explicit Judge runs
completed 12/12 calls and responses with 33,977 tokens, zero retries, and zero
technical errors. Four items received all-positive model opinions. One
hard-negative item was `not_answerable` because its supplied materials conflicted,
while its answer remained supported. One multi-source formula item had supported
material but a `partially_supported` citation and remains pending human review; the
other generated outputs were supported. Cost is unknown.

| Source trace | Items | Judge run | Judge replay |
| --- | ---: | --- | --- |
| partial-context | 1 | `0e79039a…` (local artifact: `outputs/eval/693b58d2-6a70-44de-9e7c-01d066ee0647/judgments/0e79039a-f21b-45af-9e5b-65f3dee708a4/report.md`) | complete (local artifact: `outputs/eval/693b58d2-6a70-44de-9e7c-01d066ee0647/judgments/0e79039a-f21b-45af-9e5b-65f3dee708a4/replays/d547cbea-3802-4a87-b60c-f72d8572ffd4/report.md`) |
| gold-hard-negative | 2 | `21c044fd…` (local artifact: `outputs/eval/ce08002d-5430-4c37-a5ee-5e5b143ffb27/judgments/21c044fd-7183-46f3-8386-232b8ab17f8b/report.md`) | complete (local artifact: `outputs/eval/ce08002d-5430-4c37-a5ee-5e5b143ffb27/judgments/21c044fd-7183-46f3-8386-232b8ab17f8b/replays/b5dae7c4-5f61-46b3-b02e-10d32b7e4515/report.md`) |
| multi-source | 3 | `00b95604…` (local artifact: `outputs/eval/cd342956-55d0-4839-a312-d05c7609511f/judgments/00b95604-da0c-49ea-9252-ad202dd99f10/report.md`) | complete (local artifact: `outputs/eval/cd342956-55d0-4839-a312-d05c7609511f/judgments/00b95604-da0c-49ea-9252-ad202dd99f10/replays/aa3d6d4f-8ed7-4dde-8b84-0c53ce63c284/report.md`) |

All three Judge replays completed.

## Controlled Judge suite

Current run:
challenge-v2 / judge-v4 report (local artifact: `outputs/eval/challenges/87378834-46cb-42dd-8e1e-e558cc95353e/report.md`) ·
metrics (local artifact: `outputs/eval/challenges/87378834-46cb-42dd-8e1e-e558cc95353e/metrics.json`) ·
completion marker (local artifact: `outputs/eval/challenges/87378834-46cb-42dd-8e1e-e558cc95353e/complete.json`)

| Measure | Result |
| --- | ---: |
| Cases | 7 (3 controls + 4 mutants) |
| Calls / responses | 14 / 14 |
| Retry / technical errors | 0 / 0 |
| Total tokens | 58,434 |
| Human-review pending | 2 cases |
| Cost | unknown |

All three controls received all-positive model opinions. Manual comparison with the
human-designed targets found that every mutant had an expected-direction judgment
in at least one designed target dimension; case-004 citation nevertheless remained
`supported`. This is a qualitative reading of model outputs, not an automatically
computed accuracy, calibrated golden score, or production-reliability claim.

The historical
challenge-v1 / judge-v3 run (local artifact: `outputs/eval/challenges/439cafe9-26f7-4b1b-b261-689ba9126033/report.md`)
is retained unchanged. Its case-004 was `material=partially_supported` plus
`citation=partially_supported`; the current v4 result is
`material=partially_supported` plus `citation=supported`. This is consistent with
the current rubric's separate interpretation of content support and citation
location, but it may also be single-run model variation. These two runs cannot
establish a version-caused difference or version superiority.

## Integrity note

Every generation replay and Judge replay listed above is complete. A scan for the
exact configured secret values returned `false`; no exact configured secret value
was found in the checked artifacts. This statement concerns exact-value leakage,
not a general proof that arbitrary output text can never contain sensitive-looking
content.
