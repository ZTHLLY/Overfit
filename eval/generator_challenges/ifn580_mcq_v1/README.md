# IFN580 Generator MCQ challenge v1

This frozen, evaluation-only suite isolates the `mcq-v1` Generator from retrieval and reranking.

- `inputs.json` is the only fixture material visible to the Generator.
- `annotations.json` is human-authored evaluation metadata and must never enter a model request or trace event.
- Case order is fixed: unrelated-only, partial-context, trusted + contradictory, multi-source.
- Every retained output must use exact A–D option objects, one `correct_option`, and option-level feedback/citations.
- Empty or fewer-than-requested output is valid when the visible material cannot support a clear item.
- The multi-source case asks for definition and interpretation only; numerical calculation is outside MCQ v1.
- The human annotations identify the fabricated contradictory passage; its model-visible ID, same-week source path, and section use neutral course-like labels so they do not leak its annotation.

Prepare and validate without settings, credentials, or network:

```console
overfit eval generator-challenge eval/generator_challenges/ifn580_mcq_v1
```

Execution is intentionally explicit and requires a hard provider-call budget:

```console
overfit eval generator-challenge eval/generator_challenges/ifn580_mcq_v1 \
  --execute --max-calls 8
```

Judge preparation/execution remains a separate, explicit command against each child `mcq-v1` trace. Planned live gates remain unexecuted until the user initiates them.

The historical [`ifn580_v1`](../ifn580_v1/README.md) short-answer suite remains
unchanged as read-only baseline evidence. It can still be loaded and prepared for
inspection, but execution must use this versioned `mcq-v1` suite.
