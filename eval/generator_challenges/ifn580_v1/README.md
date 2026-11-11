# IFN580 Generator challenge v1

This frozen, evaluation-only suite isolates Generator behavior from retrieval and reranking.

- `inputs.json` is the only fixture material visible to the Generator.
- `annotations.json` is human-authored evaluation metadata and must never enter a model request or trace event.
- Case order is fixed: unrelated-only, partial-context, gold + hard-negative, multi-source.
- The human annotations identify the fabricated hard-negative; its model-visible ID,
  same-week source path, and section use neutral course-like labels so they do not
  leak whether the passage is trusted or distracting.

Prepare and validate without settings, credentials, or network:

```console
overfit eval generator-challenge eval/generator_challenges/ifn580_v1
```

Execution is intentionally explicit and requires a hard provider-call budget:

```console
overfit eval generator-challenge eval/generator_challenges/ifn580_v1 \
  --execute --max-calls 8
```

Judge preparation/execution remains a separate command against each child trace.
