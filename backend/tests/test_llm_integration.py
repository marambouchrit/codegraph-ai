"""Optional: the real LLM provider (LLM_PROVIDER, gemini by default). Skipped by a normal
`pytest` run. Free with a free-tier key (Gemini, Groq...); a few cents with Claude.

    pytest -m llm        with LLM_API_KEY (or GEMINI_API_KEY...) in the environment or backend/.env

The GraphRAG context comes from the Phase 9 test world (fake Neo4j, in-memory Qdrant),
so only the LLM call is real. Skipped when no API key is configured.
"""

import pytest

from app.core.config import Settings
from app.core.errors import LLMConfigurationError
from app.llm.generator import LLMGenerationService
from app.llm.provider import create_llm_provider
from tests.test_graphrag import PROJECT_A, QUESTION, World

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def generator() -> LLMGenerationService:
    try:
        provider = create_llm_provider(Settings())
    except LLMConfigurationError as error:
        if "No API key" in error.message:
            pytest.skip(error.message)
        raise
    return LLMGenerationService(provider)


def test_a_grounded_cited_answer(generator: LLMGenerationService) -> None:
    context = World.for_project(PROJECT_A).service().build_context(PROJECT_A, QUESTION)

    response = generator.generate(context)

    assert response.model and "login" in response.answer
    assert response.cited, "the answer cites at least one source"
    assert not any("match no retrieved source" in warning for warning in response.warnings)


def test_an_unrelated_question_is_declared_insufficient(generator: LLMGenerationService) -> None:
    context = World.for_project(PROJECT_A).service().build_context(
        PROJECT_A, "How are uploaded images resized into thumbnails?"
    )

    response = generator.generate(context)

    assert "insufficient" in response.answer.lower()
