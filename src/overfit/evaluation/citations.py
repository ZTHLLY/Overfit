"""Exact citation membership, not semantic support or product repair."""


def check_citation(item: dict, chunks: list[dict]) -> dict:
    """Check a normalized item against individual supplied page intervals.

    Malformed material is a recording error, not evidence to silently repair.
    Booleans are deliberately not accepted as integer page numbers.
    """
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("Invalid material record")  # noqa: TRY004 - malformed trace data
        start = chunk.get("page")
        end = chunk.get("page_end")
        if (
            not isinstance(chunk.get("source"), str)
            or not isinstance(chunk.get("id"), str)
            or type(start) is not int
            or start < 1
            or (end is not None and (type(end) is not int or end < start))
        ):
            raise ValueError("Invalid material page range or identity")

    page = item.get("page")
    if type(page) is not int or page < 1:
        return {"status": "invalid_page", "chunk_ids": []}
    source = item.get("source")
    matching = [chunk for chunk in chunks if chunk["source"] == source]
    if not matching:
        return {"status": "unknown_source", "chunk_ids": []}
    ids = list(
        dict.fromkeys(
            chunk["id"]
            for chunk in matching
            if chunk["page"] <= page <= (chunk.get("page_end") or chunk["page"])
        )
    )
    return {"status": "valid" if ids else "page_not_supplied", "chunk_ids": ids}
