"""Runtime configuration, loaded from environment variables and `.env`.

Three kinds of values exist in this project and only one of them lives here:

* **Configuration** (this file) -- properties of the *user's environment*:
  where the model server is, how big chunks should be. Stable across runs.
* **CLI arguments** -- properties of *this particular invocation*:
  which course, how many questions. Different every time, so they are
  parsed in `cli.py` and may override the defaults below.
* **Index properties** -- embedding model name, vector dimensionality.
  These belong to the database, not to the user, and are written into the
  store's `meta` table at ingest time. They are deliberately absent here:
  any number a human has to type correctly will eventually be typed wrong.

Precedence is: field default < `.env` < environment variable < CLI flag.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["LLMSettings", "EmbedSettings", "Settings", "get_settings"]

_ENV_FILE = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    protected_namespaces=(),  # we want a field literally called `model`
)


class LLMSettings(BaseSettings):
    """Generation model -- layer 7.

    Swappable at will: changing these values does not invalidate an existing
    index, so local and hosted models can be compared on the same data.
    Any OpenAI-compatible endpoint works (Ollama, vLLM, LM Studio, DeepSeek).
    """

    model_config = _ENV_FILE | SettingsConfigDict(env_prefix="LLM_")

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"  # local servers ignore this but the SDK requires it
    model: str = "qwen3.6:27b"


class EmbedSettings(BaseSettings):
    """Embedding model -- layers 4 and 6.

    NOT swappable. A different model means a different coordinate system, so
    changing this invalidates every vector already stored. The store records
    the model name in its `meta` table and refuses to search when it does not
    match, because the failure is otherwise silent: retrieval simply degrades
    into noise without raising anything.

    Note there is no `dim` field. Dimensionality is probed on first ingest
    and persisted alongside the vectors.
    """

    model_config = _ENV_FILE | SettingsConfigDict(env_prefix="EMBED_")

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "bge-m3"


class Settings(BaseSettings):
    """Everything the pipeline needs to run."""

    model_config = _ENV_FILE

    llm: LLMSettings = Field(default_factory=LLMSettings)
    embed: EmbedSettings = Field(default_factory=EmbedSettings)

    # ---- Paths -----------------------------------------------------------
    # Course material does not have to live inside the repo. Point
    # COURSES_DIR at wherever the files already are -- iCloud, Downloads, a
    # university folder -- and the tool reads them in place. Nothing is
    # copied, so there is one copy of every lecture, not two.
    courses_dir: Path = Path("courses")
    outputs_dir: Path = Path("outputs")
    index_dir: Path = Path("index")

    @field_validator("courses_dir", "outputs_dir", "index_dir", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        """Expand `~` and make the path absolute.

        pydantic will happily accept the literal string "~/Documents" and
        hand back a Path that refers to a directory called "~", which then
        fails much later with a confusing "no PDFs found". Resolving here
        means every layer downstream sees a real, absolute path.
        """
        return value.expanduser().resolve()

    # ---- Chunking (layer 3) ---------------------------------------------
    # Approximate token counts, measured in characters (~4 chars per token
    # for English). Exact tokenisation is not worth an API call here:
    # chunking is a fuzzy craft and only needs roughly even sizes.
    # 250 tokens (~1000 chars) was chosen against real course material: a
    # lecture slide holds 200-300 chars and stays whole, while a prose
    # handout page holds 1400-1800 and gets split in two. A larger target
    # would exceed every page in the corpus, making this layer a no-op.
    chunk_size: int = Field(default=250, ge=50)
    chunk_overlap: int = Field(default=25, ge=0)

    # ---- Retrieval (layer 6) --------------------------------------------
    top_k: int = Field(default=5, ge=1)

    # ---- Generation (layer 7) -------------------------------------------
    num_questions: int = Field(default=10, ge=1)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_retries: int = Field(default=3, ge=0)  # retries on schema validation failure
    # Generous, because the first call to a large local model pays for
    # loading ~17 GB of weights off disk before it generates a single token.
    # A timeout that fires mid-generation wastes everything spent so far,
    # which is worse than waiting.
    request_timeout: float = Field(default=900.0, gt=0)

    @model_validator(mode="after")
    def _check_chunking(self) -> Settings:
        """Overlap must be smaller than the chunk itself.

        Otherwise the chunker never advances and loops forever. Failing here,
        at config load, is far cheaper than failing ten minutes into an ingest.
        """
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) must be smaller than "
                f"chunk_size ({self.chunk_size})"
            )
        return self

    # ---- Derived paths ---------------------------------------------------

    def course_dir(self, course: str) -> Path:
        """Where the source material for a course lives."""
        return self.courses_dir / _safe_name(course)

    def db_path(self, course: str) -> Path:
        """One database file per course, e.g. `index/IFN636.db`.

        Separate indexes rather than one shared table, because a query about
        "overfitting" should not pull chunks out of an unrelated unit. It also
        means a single course can be deleted and rebuilt without touching the
        others, and `compare` across courses simply opens two stores -- which
        is honest, since that genuinely is a cross-index query.
        """
        return self.index_dir / f"{_safe_name(course)}.db"

    def output_path(self, course: str, artifact: str) -> Path:
        """e.g. output_path("IFN636", "mock_exam.md") -> outputs/IFN636_mock_exam.md"""
        return self.outputs_dir / f"{_safe_name(course)}_{artifact}"

    def ensure_dirs(self) -> None:
        """Create the directories we write into. Never creates courses_dir:
        its absence means the user pointed us somewhere wrong, and silently
        making an empty folder would hide that."""
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_name(name: str) -> str:
    """Make a course code safe to use as a path component.

    Course codes come from the command line, so they must never be able to
    escape the directory we intend to write in.
    """
    cleaned = _UNSAFE.sub("_", name.strip())
    if not cleaned or cleaned.strip(".") == "":
        raise ValueError(f"invalid course name: {name!r}")
    return cleaned


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that every layer sees the same values and `.env` is read once.
    Call `get_settings.cache_clear()` in tests that need a fresh load.
    """
    return Settings()
