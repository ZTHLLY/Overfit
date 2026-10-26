"""Synthetic recorded material metadata, no PDF or model service."""


def provenance_for(text, *, pages=(3,), total=100, digest="a" * 64, mapping="exact"):
    return {"schema_version": 1, "document_sha256": digest, "page_count": total,
            "extractor": "synthetic/1", "blocks": [
                {"block_id": "paragraph-1", "label": "text", "parent_id": None,
                 "context_id": None, "text": text,
                 "locations": [{"page": page, "charspan": None, "bbox": None} for page in pages],
                 "text_span": [0, len(text)], "block_span": [0, len(text)], "mapping": mapping}]}
