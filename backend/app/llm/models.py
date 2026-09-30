"""What the LLM layer exchanges: a prompt, a provider's completion, the assistant's answer.

Independent from FastAPI and from any provider SDK: plain frozen dataclasses.
"""

from dataclasses import dataclass

from app.graphrag.models import GraphStatus, Source


@dataclass(frozen=True)
class NumberedSource:
    """A GraphRAG source with the number the prompt gave it: cited as "[number]"."""

    number: int
    source: Source

    @property
    def label(self) -> str:
        """e.g. "[1] AuthService.login — app/auth/service.py:14-20"."""
        return f"[{self.number}] {self.source.qualified_name} — {self.source.location}"


@dataclass(frozen=True)
class Prompt:
    system: str  # instructions only: fixed, never contains repository content
    user: str  # the question and the repository context, as delimited data
    sources: tuple[NumberedSource, ...]  # the numbering used in `user`


@dataclass(frozen=True)
class LLMCompletion:
    """What a provider returns: the generated text and a little metadata."""

    text: str
    model: str  # the model that actually answered
    truncated: bool = False  # the output limit was reached: the answer may be cut
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class AssistantResponse:
    """The assistant's result for one question, ready for an API or a UI (Phase 11)."""

    question: str
    answer: str
    # Every source given to the model, numbered as in the prompt. Built from the GraphRAG
    # context, never from the model's text: the model can only point at these numbers.
    sources: tuple[NumberedSource, ...]
    # The numbers the answer actually cites that exist in `sources`, in order of appearance.
    cited: tuple[int, ...]
    graph_status: GraphStatus
    warnings: tuple[str, ...]
    model: str | None  # None when no model was called (nothing relevant was retrieved)

    @property
    def cited_sources(self) -> tuple[NumberedSource, ...]:
        by_number = {item.number: item for item in self.sources}
        return tuple(by_number[number] for number in self.cited)
