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

Precedence is: field default < global `.env` (`~/.config/overfit/.env`)
< `.env` in the working directory < environment variable < CLI flag. The
global file is what makes "cd into a course folder and run one command"
work: keys live in one place instead of being copied into every course.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "EmbedSettings",
    "LLMSettings",
    "PdfBackend",
    "RerankSettings",
    "Settings",
    "course_name_for",
    "env_files",
    "get_settings",
    "global_env_file",
    "use_course_directory",
    "use_settings",
]


# Which library turns a PDF into text -- see `parser`. Spelled as a Literal
# rather than a plain string so that a typo fails when settings load, not ten
# minutes into an ingest, and so the set of real options is discoverable
# without reading the parser.
PdfBackend = Literal["pypdf", "docling", "docling+formula"]

_ENV_FILE = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    # `KEY=` in a later file (a copied template, say) must not blank out a
    # key that an earlier layer actually set.
    env_ignore_empty=True,
    extra="ignore",
    protected_namespaces=(),  # we want a field literally called `model`
)


def global_env_file() -> Path:
    """The per-user configuration file, honouring ``XDG_CONFIG_HOME``.

    A relative value is ignored, as the XDG specification requires.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return root / "overfit" / ".env"


# Course-directory mode switches this off. A course folder is often an
# unzipped download; letting a `.env` inside it set EMBED_BASE_URL would send
# the user's global key to whoever wrote that file.
_read_local_env = True


def env_files() -> tuple[Path, ...]:
    """Dotenv files in load order; later files override earlier ones.

    Resolved on every construction rather than frozen at import time, so the
    working directory that counts is the one the command runs in. Missing
    files are skipped by pydantic-settings.
    """
    local = (Path(".env"),) if _read_local_env else ()
    return (global_env_file(), *local)


class _LayeredSettings(BaseSettings):
    """Reads the global `.env`, then the working-directory `.env`.

    An explicit ``_env_file`` (including ``None``) still wins, which is how
    tests and one-off overrides opt out of the user's files.
    """

    def __init__(self, **values) -> None:
        values.setdefault("_env_file", env_files())
        super().__init__(**values)


class LLMSettings(_LayeredSettings):
    """Generation model -- layer 7.

    Swappable at will: changing these values does not invalidate an existing
    index, so local and hosted models can be compared on the same data.
    Any OpenAI-compatible endpoint works (DeepSeek, OpenRouter, Ollama, vLLM).
    The default is DeepSeek's hosted API; the key has no default.
    """

    model_config = _ENV_FILE | SettingsConfigDict(env_prefix="LLM_")

    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-v4-flash"


class EmbedSettings(_LayeredSettings):
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


class RerankSettings(_LayeredSettings):
    """Query-time OpenRouter reranker; irrelevant when no query exists."""

    model_config = _ENV_FILE | SettingsConfigDict(env_prefix="OVERFIT_RERANK_")

    provider: str = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "voyageai/rerank-3"
    timeout: float = 30.0

    def for_query(self) -> RerankSettings:
        """Validate only when a real query path needs the external service."""
        from overfit.errors import RerankConfigurationError

        if self.provider != "openrouter":
            raise RerankConfigurationError(
                "The first reranker provider must be openrouter."
            )
        if not self.base_url.startswith("https://"):
            raise RerankConfigurationError("Reranker base URL must use HTTPS.")
        if not self.model.strip():
            raise RerankConfigurationError("Reranker model must not be empty.")
        if self.timeout <= 0:
            raise RerankConfigurationError("Reranker timeout must be positive.")
        if not self.api_key.get_secret_value().strip():
            raise RerankConfigurationError(
                "Reranker API key is missing. Set OVERFIT_RERANK_API_KEY "
                "or pass --rerank-api-key."
            )
        return self


class Settings(_LayeredSettings):
    """Everything the pipeline needs to run."""

    model_config = _ENV_FILE

    llm: LLMSettings = Field(default_factory=LLMSettings)
    embed: EmbedSettings = Field(default_factory=EmbedSettings)
    rerank: RerankSettings = Field(default_factory=RerankSettings)

    # ---- Paths -----------------------------------------------------------
    # Course material does not have to live inside the repo. Point
    # COURSES_DIR at wherever the files already are -- iCloud, Downloads, a
    # university folder -- and the tool reads them in place. Nothing is
    # copied, so there is one copy of every lecture, not two.
    courses_dir: Path = Path("courses")
    outputs_dir: Path = Path("outputs")
    index_dir: Path = Path("index")
    # Set only by course-directory mode (see `for_course_directory`): the
    # folder that *is* the course, and where the exam files are written.
    course_root: Path | None = None
    exam_dir: Path | None = None

    @field_validator("course_root", "exam_dir", mode="after")
    @classmethod
    def _expand_optional(cls, value: Path | None) -> Path | None:
        return None if value is None else value.expanduser().resolve()

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

    # ---- Loading (layer 1) ----------------------------------------------
    # Which file types to index. Restricting this matters when a folder holds
    # the same lecture twice -- a PDF and a Markdown conversion of it, say.
    # Both would be indexed as separate documents, so a single concept ends
    # up with two near-identical vectors competing for the same top-k slots,
    # and the citation a reader gets depends on which copy happened to win.
    # PDFs also carry real page numbers; a Markdown file has none, so its
    # citations degrade to the file as a whole.
    extensions: str = ".pdf"

    @property
    def extension_list(self) -> tuple[str, ...]:
        return tuple(
            item.strip().lower()
            for item in self.extensions.split(",")
            if item.strip()
        )

    # ---- Parsing (layer 2) ----------------------------------------------
    # Which backend extracts text from a PDF. Only one can be in force for a
    # given index, and the choice is recorded in the store's meta table --
    # two backends produce materially different text from the same file, so
    # an index built by one is not an index built by the other, even though
    # nothing about it looks wrong.
    #
    #   pypdf            fast, pure Python, no models. Flattens tables and
    #                    formulas into scattered short lines.
    #   docling          layout-aware: real table structure, reading order,
    #                    section headings. Roughly an order of magnitude
    #                    slower and pulls in model weights.
    #   docling+formula  as above, plus equation-to-LaTeX enrichment. Slower
    #                    again, and known to space out characters in some
    #                    versions -- worth comparing before trusting.
    #
    # Install the optional dependency before selecting a docling option:
    #   uv sync --extra docling
    pdf_backend: PdfBackend = "pypdf"

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
    top_k: int = Field(default=10, ge=1)

    # ---- Generation (layer 7) -------------------------------------------
    num_questions: int = Field(default=30, ge=1)
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

    def for_course_directory(self, directory: Path) -> Settings:
        """Settings for running inside one course folder.

        The folder is read in place; the index and run records go into a
        hidden `.overfit/` beside the material (the loader skips dot
        directories), and the exam files land in the folder itself, where
        the user is already looking. Model settings are inherited unchanged.
        """
        root = directory.expanduser().resolve()
        state = root / ".overfit"
        return self.model_copy(
            update={
                "course_root": root,
                "exam_dir": root,
                "outputs_dir": state,
                "index_dir": state / "index",
            }
        )

    def course_dir(self, course: str) -> Path:
        """Where the source material for a course lives."""
        if self.course_root is not None:
            return self.course_root
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
        return (self.exam_dir or self.outputs_dir) / f"{_safe_name(course)}_{artifact}"

    def generated_files(self, course: str) -> frozenset[Path]:
        """Files Overfit writes, which must never be indexed as material.

        Only reachable when exams are written inside the course folder and
        Markdown indexing is enabled, but then an exam would otherwise be
        ingested as a lecture on the next run.
        """
        names = ("mock_exam.md", "answers.md")
        current = {self.output_path(course, name).resolve() for name in names}
        if self.exam_dir is None or not self.exam_dir.is_dir():
            return frozenset(current)
        # Exams left behind under an earlier folder name are still ours, but
        # only when the file says so: `week5_answers.md` may be real material.
        stale = {
            path.resolve()
            for name in names
            for path in self.exam_dir.glob(f"*_{name}")
            if path.is_file() and _is_rendered_exam(path)
        }
        return frozenset(current | stale)

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


_EXAM_HEADINGS = (" — Practice Exam", " — Practice Exam Answers")


def _is_rendered_exam(path: Path) -> bool:
    """True when the first line is the heading Overfit's templates write."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline().rstrip()
    except OSError:
        return False
    return first.startswith("# ") and first.endswith(_EXAM_HEADINGS)


def course_name_for(directory: Path) -> str:
    """The course name used in course-directory mode: the folder's name."""
    return _safe_name(directory.expanduser().resolve().name)


_active: Settings | None = None


@lru_cache(maxsize=1)
def _load_settings() -> Settings:
    return Settings()


def use_settings(settings: Settings | None) -> None:
    """Make `settings` what every layer's `get_settings()` returns.

    ``None`` restores normal loading, including the working-directory `.env`.
    """
    global _active, _read_local_env
    _active = settings
    if settings is None and not _read_local_env:
        _read_local_env = True
        _load_settings.cache_clear()


def use_course_directory(directory: Path) -> Settings:
    """Install settings for running inside one course folder.

    Course-directory mode derives its paths at runtime, after the CLI has
    seen that no `--course` was given; installing them here means the store,
    trace writer and renderer all agree without threading settings through
    every call. Only the global `.env` and real environment variables are
    read in this mode -- never a `.env` inside the course folder.
    """
    global _read_local_env
    _read_local_env = False
    _load_settings.cache_clear()
    settings = Settings().for_course_directory(directory)
    use_settings(settings)
    return settings


def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that every layer sees the same values and `.env` is read once.
    Call `get_settings.cache_clear()` in tests that need a fresh load.
    """
    return _active if _active is not None else _load_settings()


def _cache_clear() -> None:
    use_settings(None)
    _load_settings.cache_clear()


get_settings.cache_clear = _cache_clear  # type: ignore[attr-defined]
