"""Phase 11: the chat workflow as a LangGraph state graph, with no server and no model.

Retrieval is the Phase 9 test world (fake Neo4j, in-memory Qdrant); the LLM is the Phase 10
fake provider. The HTTP behavior of the chat is in test_chat_api.py: here, the graph itself.
"""

import dataclasses
from typing import Any

import pytest

from app.core.errors import LLMUnavailableError, VectorStoreUnavailableError
from app.graphrag.models import GraphRAGContext
from app.llm.generator import LLMGenerationService
from app.llm.models import AssistantResponse
from app.services.chat_workflow import build_chat_workflow
from tests.test_graphrag import PROJECT_A, QUESTION, World
from tests.test_llm import FakeLLM

QUESTION_INPUT = {"project_id": PROJECT_A, "question": QUESTION}


class Retrieval:
    """The Phase 9 world's real context, an empty one, or an error."""

    def __init__(self, empty: bool = False, error: Exception | None = None) -> None:
        self.empty = empty
        self.error = error

    def build_context(self, project_id: str, query: str) -> GraphRAGContext:
        if self.error:
            raise self.error
        context = World.for_project(project_id).service().build_context(project_id, query)
        if self.empty:
            return dataclasses.replace(
                context, vector_results=(), seeds=(), entities=(), relationships=(), paths=(),
                sources=(),
            )  # fmt: skip
        return context


def run(retrieval: Retrieval, llm: FakeLLM) -> tuple[list[str], dict[str, Any]]:
    """Run the graph: the nodes that ran, in order, and the state they produced."""
    workflow = build_chat_workflow(lambda: retrieval, lambda: LLMGenerationService(llm))
    nodes: list[str] = []
    state: dict[str, Any] = {}
    for step in workflow.stream(QUESTION_INPUT, stream_mode="updates"):
        for node, update in step.items():
            nodes.append(node)
            state.update(update)
    return nodes, state


def test_the_graph_has_the_documented_nodes_and_edges() -> None:
    graph = build_chat_workflow(lambda: Retrieval(), lambda: LLMGenerationService(FakeLLM())).get_graph()

    assert set(graph.nodes) == {
        "__start__", "retrieve_context", "no_context", "build_prompt", "generate",
        "verify_citations", "__end__",
    }  # fmt: skip
    edges = {(edge.source, edge.target, edge.conditional) for edge in graph.edges}
    assert edges == {
        ("__start__", "retrieve_context", False),
        ("retrieve_context", "build_prompt", True),
        ("retrieve_context", "no_context", True),
        ("build_prompt", "generate", False),
        ("generate", "verify_citations", False),
        ("verify_citations", "__end__", False),
        ("no_context", "__end__", False),
    }


def test_a_question_runs_every_step_once_in_order() -> None:
    llm = FakeLLM("`AuthService.login` checks the password [1], see also [99].")

    nodes, state = run(Retrieval(), llm)

    assert nodes == ["retrieve_context", "build_prompt", "generate", "verify_citations"]
    assert len(llm.calls) == 1 and llm.calls[0] == (state["prompt"].system, state["prompt"].user)
    response: AssistantResponse = state["response"]
    assert response.answer == state["completion"].text
    assert response.sources == state["prompt"].sources and response.cited == (1,)
    assert any("[99]" in warning for warning in response.warnings)


def test_nothing_retrieved_ends_without_calling_the_llm() -> None:
    llm = FakeLLM()

    nodes, state = run(Retrieval(empty=True), llm)

    assert nodes == ["retrieve_context", "no_context"]
    assert llm.calls == [] and "prompt" not in state
    assert state["response"].answer.startswith("The available repository context is insufficient")
    assert state["response"].model is None


def test_the_workflow_gives_the_same_answer_as_the_generation_service() -> None:
    retrieval, llm = Retrieval(), FakeLLM("Tokens are created by `create_token` [2].")
    workflow = build_chat_workflow(lambda: retrieval, lambda: LLMGenerationService(llm))

    response = workflow.invoke(QUESTION_INPUT)["response"]

    context = retrieval.build_context(PROJECT_A, QUESTION)
    assert response == LLMGenerationService(llm).generate(context)


@pytest.mark.parametrize("retrieval_error, llm_error", [
    (VectorStoreUnavailableError("Qdrant is not reachable."), None),
    (None, LLMUnavailableError("The LLM provider is unavailable.")),
])  # fmt: skip
def test_an_error_in_a_node_propagates_unchanged(
    retrieval_error: Exception | None, llm_error: Exception | None
) -> None:
    workflow = build_chat_workflow(
        lambda: Retrieval(error=retrieval_error), lambda: LLMGenerationService(FakeLLM(error=llm_error))
    )

    with pytest.raises(type(retrieval_error or llm_error)) as raised:
        workflow.invoke(QUESTION_INPUT)

    assert raised.value is (retrieval_error or llm_error)
