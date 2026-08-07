"""Choosing which chunks to put in front of the model.

Retrieval answers "which chunks match this question?". A mock exam has no
question -- the request is for coverage of a whole unit -- so retrieval is
the wrong instrument for it. Ask a vector store for the five chunks nearest
to "IFN580 machine learning" and it will return five chunks that are near
each other, which is exactly the opposite of what an exam needs.

The right operation is sampling with spread. Two functions here:

* `spread` picks chunks that are far apart from one another, giving an exam
  that touches many topics instead of asking the same thing five ways.
* `mmr` does the same within a set already narrowed by a query, trading
  relevance against redundancy -- the classic Maximal Marginal Relevance.

Both work on vectors that are already stored, so neither costs a model call.
"""

from __future__ import annotations

from dataclasses import dataclass

from overfit.models import EmbeddedChunk, Vector

__all__ = ["Topic", "cluster", "allocate", "spread", "mmr", "similarity"]


@dataclass(frozen=True, slots=True)
class Topic:
    """A region of the course, and how much of the unit is spent on it.

    Cluster size is information we were computing and throwing away. It is
    also the closest thing this pipeline has to a measure of emphasis: a
    subject the lecturer covers in slides, then drills in a tutorial, then
    applies in a practical, occupies more of the corpus than one mentioned in
    passing -- and is correspondingly more likely to be examined.
    """

    representative: EmbeddedChunk
    members: list[EmbeddedChunk]

    @property
    def size(self) -> int:
        """Number of chunks. A weak signal on its own -- see `weight`."""
        return len(self.members)

    @property
    def sources(self) -> set[str]:
        return {item.chunk.source for item in self.members}

    @property
    def weight(self) -> int:
        """How many distinct files touch this topic.

        Deliberately *not* the chunk count. Raw counts reward length and
        duplication: a worksheet and its solutions file hold nearly identical
        text, so anything they cover would score double for no pedagogical
        reason, and a single long handout would outrank a concept the unit
        genuinely returns to. Counting documents asks a better question --
        how many times did the unit come back to this from a different
        angle? -- and is inherently resistant to one file repeating itself.
        """
        return len(self.sources)

    @property
    def label(self) -> str:
        """A short human-readable stand-in, taken from the representative."""
        text = " ".join(self.representative.chunk.text.split())
        return text[:70] + ("..." if len(text) > 70 else "")


def similarity(a: Vector, b: Vector) -> float:
    """Cosine similarity. Vectors are stored normalised, so this is a dot."""
    return sum(x * y for x, y in zip(a, b))


# Chunks shorter than this carry too little to build a question from, even
# though they are perfectly valid index entries. Used only when choosing what
# to generate from -- never when deciding what to store.
_SUBSTANTIAL_CHARS = 400

# Below this a chunk is excluded from generation entirely. Set against real
# course material, where the median chunk runs to about 400 characters and
# everything under ~150 turned out to be a title, a divider or boilerplate.
_MIN_SELECTABLE_CHARS = 200

_LLOYD_ITERATIONS = 6


def spread(candidates: list[EmbeddedChunk], count: int) -> list[EmbeddedChunk]:
    """Pick `count` chunks that between them cover the whole corpus.

    Implemented as clustering, and the reason is worth recording, because the
    obvious alternative fails in an interesting way.

    Farthest-point sampling -- repeatedly take whatever lies furthest from
    everything chosen so far -- gives excellent coverage and terrible
    material. By construction it seeks outliers, and in a corpus of lecture
    notes the outliers are the acknowledgement of country, the "CPU is like a
    motorcycle" aside, the stray caption: passages that are unlike everything
    else precisely because they are not about the subject. Coverage was never
    the whole requirement. Each selected chunk also has to be *representative*
    of its neighbourhood.

    So: partition the corpus into `count` groups, then return the most
    central member of each. Groups give the coverage; centrality gives
    material worth asking a question about.

    Seeding is by farthest-point, which is a good use of it -- spread-out
    starting positions -- and it keeps the result deterministic, so the same
    command twice produces the same exam.
    """
    return [topic.representative for topic in cluster(candidates, count)]


def cluster(candidates: list[EmbeddedChunk], count: int) -> list[Topic]:
    """Partition the corpus into `count` topics, heaviest first.

    Returns the groups rather than only their representatives, because the
    sizes are what tell a reader which parts of a unit carry weight -- and
    that is arguably more useful than the questions generated from them.
    """
    if count <= 0 or not candidates:
        return []

    pool = _substantial(candidates, count)
    if count >= len(pool):
        return sorted(
            (Topic(item, [item]) for item in pool),
            key=lambda topic: -len(topic.representative.chunk.text),
        )

    vectors = [item.embedding for item in pool]
    centres = [vectors[i] for i in _spread_seeds(vectors, count)]

    assignment: list[int] = []
    for _ in range(_LLOYD_ITERATIONS):
        previous = assignment
        assignment = [
            max(range(len(centres)), key=lambda c: similarity(vector, centres[c]))
            for vector in vectors
        ]
        if assignment == previous:
            break
        for index in range(len(centres)):
            members = [v for v, a in zip(vectors, assignment) if a == index]
            if members:
                centres[index] = _centroid(members)

    topics: list[Topic] = []
    for index in range(len(centres)):
        members = [pool[i] for i, a in enumerate(assignment) if a == index]
        if not members:
            continue
        best = max(members, key=lambda item: _representativeness(item, centres[index]))
        topics.append(Topic(representative=best, members=members))

    # Heaviest first: most distinct documents, then most chunks. Ordering by
    # weight is what makes this a revision priority list rather than a bag.
    return sorted(topics, key=lambda topic: (-topic.weight, -topic.size))


def allocate(topics: list[Topic], slots: int) -> list[tuple[Topic, int]]:
    """Decide how many questions each topic deserves.

    Coverage and emphasis pull in opposite directions. Allocating purely by
    weight buries every minor topic under the biggest one; allocating evenly
    pretends a passing remark matters as much as a subject the unit returns
    to four times. So: everyone who fits gets one, and only the remainder is
    distributed by weight.

    Uses largest-remainder apportionment, which is what electoral systems use
    for the same problem -- turning proportions into whole seats without
    letting rounding quietly erase the small ones.
    """
    if slots <= 0 or not topics:
        return []

    if slots <= len(topics):
        # Not enough questions to reach everything: keep the heaviest topics.
        return [(topic, 1) for topic in topics[:slots]]

    shares = {id(topic): 1 for topic in topics}
    remaining = slots - len(topics)

    total_weight = sum(topic.weight for topic in topics) or 1
    exact = {id(topic): remaining * topic.weight / total_weight for topic in topics}

    for topic in topics:
        shares[id(topic)] += int(exact[id(topic)])

    # Hand out what rounding left over, largest fractional part first.
    leftover = slots - sum(shares.values())
    by_remainder = sorted(
        topics, key=lambda topic: -(exact[id(topic)] - int(exact[id(topic)]))
    )
    for topic in by_remainder[:leftover]:
        shares[id(topic)] += 1

    return [(topic, shares[id(topic)]) for topic in topics]


def pick_within(topic: Topic, count: int) -> list[EmbeddedChunk]:
    """Choose `count` chunks from one topic, avoiding near-duplicates.

    Matters because a worksheet and its solutions file often contain the same
    passage verbatim. Handing the model both wastes a question slot on a
    repeat, so members are chosen for mutual distance the same way topics are.
    """
    if count >= len(topic.members):
        return list(topic.members)
    if count <= 1:
        return [topic.representative]
    return [t.representative for t in cluster(topic.members, count)]


def _substantial(candidates: list[EmbeddedChunk], count: int) -> list[EmbeddedChunk]:
    """Drop chunks too slight to build a question from.

    Clustering alone cannot solve this. The worst offenders -- an
    acknowledgement of country, a one-line analogy, a section divider -- are
    unlike everything else in the corpus, so they land in clusters of one,
    and the most representative member of a cluster of one is itself.

    Length is a crude proxy for substance but a reliable one here: a passage
    under a couple of hundred characters rarely contains a claim, and a
    question needs a claim to be about. These chunks stay in the index and
    remain searchable; they are simply not source material for generation.

    The floor is relaxed rather than enforced if it would leave too little to
    choose from, so a genuinely terse corpus still produces output.
    """
    keep = [item for item in candidates if len(item.chunk.text) >= _MIN_SELECTABLE_CHARS]
    if len(keep) >= count:
        return keep
    # Not enough substantial material: take the longest of what there is.
    return sorted(candidates, key=lambda item: -len(item.chunk.text))[
        : max(count, len(keep))
    ]


def _representativeness(item: EmbeddedChunk, centre: Vector) -> float:
    """How well one chunk stands in for its cluster.

    Centrality alone would still favour a six-word slide title sitting near
    the middle of a topic. Weighting by length breaks that tie towards the
    passage that actually says something, without pushing towards the longest
    chunk available -- the weight saturates once a chunk is substantial.
    """
    weight = min(len(item.chunk.text) / _SUBSTANTIAL_CHARS, 1.0)
    return similarity(item.embedding, centre) * weight


def _spread_seeds(vectors: list[Vector], count: int) -> list[int]:
    """Farthest-point initialisation: start apart, so clusters do not collapse."""
    centre = _centroid(vectors)
    first = max(range(len(vectors)), key=lambda i: similarity(vectors[i], centre))
    seeds = [first]
    nearest = [similarity(vector, vectors[first]) for vector in vectors]

    while len(seeds) < count:
        taken = set(seeds)
        best = min(
            (i for i in range(len(vectors)) if i not in taken),
            key=lambda i: nearest[i],
        )
        seeds.append(best)
        for i, vector in enumerate(vectors):
            nearest[i] = max(nearest[i], similarity(vector, vectors[best]))
    return seeds


def mmr(
    candidates: list[EmbeddedChunk],
    query_vector: Vector,
    count: int,
    *,
    diversity: float = 0.6,
) -> list[EmbeddedChunk]:
    """Maximal Marginal Relevance: relevant, but not repetitive.

    Plain top-k has a failure mode that is easy to miss -- when a document
    repeats itself, or two files overlap (a worksheet and its solutions, say),
    the top five can be five copies of one passage. Every slot spent on a
    duplicate is a slot not spent on something the exam could have covered.

    Both terms are rescaled across the candidate pool before being combined.
    This matters more than it looks. Raw cosine scores sit in a narrow, high
    band -- an unrelated shopping list still scores around 0.35 against a
    technical definition -- so a genuinely large difference in meaning shows
    up as a difference of hundredths. Fed to the formula unscaled, relevance
    always dwarfs redundancy and `diversity` silently does nothing. Rescaling
    puts both on [0, 1], so the parameter means the same thing on any corpus.

    Args:
        diversity: 0 reproduces plain top-k; 1 ignores the query entirely.
            Defaults high, because for an exam a near-duplicate question is a
            worse outcome than a slightly less central one.
    """
    if count >= len(candidates):
        return list(candidates)
    if count <= 0 or not candidates:
        return []

    relevance = _rescale([similarity(item.embedding, query_vector) for item in candidates])
    chosen: list[int] = [max(range(len(candidates)), key=lambda i: relevance[i])]
    nearest = [
        similarity(item.embedding, candidates[chosen[0]].embedding)
        for item in candidates
    ]

    while len(chosen) < count:
        picked = set(chosen)
        scaled = _rescale(nearest)
        best = max(
            (i for i in range(len(candidates)) if i not in picked),
            key=lambda i: (1 - diversity) * relevance[i] - diversity * scaled[i],
        )
        chosen.append(best)
        for i, item in enumerate(candidates):
            nearest[i] = max(
                nearest[i], similarity(item.embedding, candidates[best].embedding)
            )

    return [candidates[i] for i in chosen]


def _rescale(values: list[float]) -> list[float]:
    """Min-max onto [0, 1]. A flat list becomes all zeros, not a divide by nought."""
    low, high = min(values), max(values)
    span = high - low
    if span < 1e-12:
        return [0.0] * len(values)
    return [(value - low) / span for value in values]


def _centroid(vectors: list[Vector]) -> Vector:
    dim = len(vectors[0])
    total = [0.0] * dim
    for vector in vectors:
        for i, value in enumerate(vector):
            total[i] += value
    count = len(vectors)
    return [value / count for value in total]
