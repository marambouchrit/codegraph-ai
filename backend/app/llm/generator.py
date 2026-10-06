"""Turn a GraphRAG context into the assistant's answer: prompt -> LLM -> checked response.

    GraphRAGService (Phase 9)  retrieval: vector search + graph expansion
            ↓ GraphRAGContext
    LLMGenerationService       this module: generation only
            ├── PromptBuilder  context -> system + user prompt, numbered sources
            └── LLMProvider    prompt -> text (Claude, or any other provider)
            ↓
    AssistantResponse          answer, numbered sources, citations, status, warnings

No retrieval here: no Neo4j, no Qdrant, no embeddings. The question is the context's own
query, so the answer can never be about another question than the evidence.

Citations are checked, not trusted: the answer's "[n]" markers are matched against the
numbered sources; a number that points at nothing is reported in the warnings. When
nothing was retrieved, no model is called: the answer says the context is insufficient.
LLM errors propagate (LLM*Error): a failure is never replaced by a made-up answer.
"""

import logging
import re

from app.graphrag.models import GraphRAGContext
from app.llm.models import AssistantResponse, LLMCompletion, NumberedSource, Prompt
from app.llm.prompts import PromptBuilder
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

# "[1]", "[2][3]" and "[2, 3]" all cite sources.
CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

# The system prompt asks the model to start with these words when the context does not answer
# the question (prompts.py, rule 2).
INSUFFICIENT = "The available repository context is insufficient"

NOTHING_RETRIEVED = (
    f"{INSUFFICIENT} to answer this question: no code related to it was found in the "
    "indexed project."
)


class LLMGenerationService:
    def __init__(self, provider: LLMProvider, prompt_builder: PromptBuilder | None = None) -> None:
        self.provider = provider
        self.prompt_builder = prompt_builder or PromptBuilder()

    def generate(self, context: GraphRAGContext) -> AssistantResponse:
        """A grounded answer to `context.query`, from `context` only.

        The chat runs the same steps as separate nodes of its workflow
        (app/services/chat_workflow.py).
        """
        if not context.vector_results:
            return self.nothing_retrieved(context)
        prompt = self.prompt_builder.build(context)
        completion = self.provider.generate(prompt.system, prompt.user)
        return self.respond(context, prompt, completion)

    def nothing_retrieved(self, context: GraphRAGContext) -> AssistantResponse:
        """The answer when no code was retrieved: no model is called."""
        warnings = (*context.warnings, "Nothing relevant was retrieved, so no LLM was called.")
        return AssistantResponse(
            question=context.query, answer=NOTHING_RETRIEVED, sources=(), cited=(),
            graph_status=context.graph_status, warnings=warnings, model=None,
        )  # fmt: skip

    def respond(
        self, context: GraphRAGContext, prompt: Prompt, completion: LLMCompletion
    ) -> AssistantResponse:
        """The model's text with its citations checked against the prompt's sources."""
        warnings = list(context.warnings)
        cited, unknown = check_citations(completion.text, prompt.sources)
        if unknown:
            listed = ", ".join(f"[{number}]" for number in unknown)
            warnings.append(f"The answer cites {listed}, which match no retrieved source.")
        if completion.truncated:
            warnings.append("The answer reached the output limit (LLM_MAX_TOKENS) and may be cut.")
        if completion.text.lstrip().startswith(INSUFFICIENT):
            warnings.append(
                "The retrieved code did not contain what this question needs: the answer may be "
                "incomplete."
            )
        logger.info(
            "Answer for project %s: %d sources, %d cited, model %s",
            context.project_id, len(prompt.sources), len(cited), completion.model,
        )  # fmt: skip
        return AssistantResponse(
            question=context.query,
            answer=completion.text,
            sources=prompt.sources,
            cited=cited,
            graph_status=context.graph_status,
            warnings=tuple(warnings),
            model=completion.model,
        )


def check_citations(
    answer: str, sources: tuple[NumberedSource, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """(numbers cited that are sources, numbers cited that are not), in order, no repeats."""
    valid = {item.number for item in sources}
    cited: list[int] = []
    unknown: list[int] = []
    for match in CITATION.finditer(answer):
        for number in (int(part) for part in match.group(1).split(",")):
            bucket = cited if number in valid else unknown
            if number not in bucket:
                bucket.append(number)
    return tuple(cited), tuple(unknown)
