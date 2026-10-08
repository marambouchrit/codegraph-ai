"""The chat workflow (LangGraph): the order of the steps, the branch, the citation check.

No server and no model: retrieval is a ready context, the LLM is a fake.
"""

from typing import Any

import pytest

from app.core.errors import LLMUnavailableError, VectorStoreUnavailableError
from app.llm.generator import LLMGenerationService
from app.services.chat_workflow import build_chat_workflow
from tests.helpers import PROJECT_ID, FakeLLM, FakeRetrieval, sample_context

QUESTION = {"project_id": PROJECT_ID, "question": "How is authentication implemented?"}


def run(retrieval: FakeRetrieval, llm: FakeLLM) -> tuple[list[str], dict[str, Any]]:
    """Run the graph: the nodes that ran, in order, and the state they produced."""
    workflow = build_chat_workflow(lambda: retrieval, lambda: LLMGenerationService(llm))
    nodes: list[str] = []
    state: dict[str, Any] = {}
    for step in workflow.stream(QUESTION, stream_mode="updates"):
        for node, update in step.items():
            nodes.append(node)
            state.update(update)
    return nodes, state


def test_the_graph_has_five_nodes_and_one_conditional_branch() -> None:
    graph = build_chat_workflow(lambda: FakeRetrieval(), lambda: LLMGenerationService(FakeLLM())).get_graph()

    assert {(edge.source, edge.target, edge.conditional) for edge in graph.edges} == {
        ("__start__", "retrieve_context", False),
        ("retrieve_context", "build_prompt", True),
        ("retrieve_context", "no_context", True),
        ("build_prompt", "generate", False),
        ("generate", "verify_citations", False),
        ("verify_citations", "__end__", False),
        ("no_context", "__end__", False),
    }


def test_a_question_runs_every_step_once_in_order() -> None:
    retrieval, llm = FakeRetrieval(), FakeLLM("`login` checks the password [2].")

    nodes, state = run(retrieval, llm)

    assert nodes == ["retrieve_context", "build_prompt", "generate", "verify_citations"]
    assert retrieval.calls == 1 and len(llm.calls) == 1  # one retrieval, one LLM call
    assert state["response"].answer == "`login` checks the password [2]."
    assert state["response"].cited == (2,)


def test_nothing_retrieved_ends_without_calling_the_llm() -> None:
    llm = FakeLLM()

    nodes, state = run(FakeRetrieval(sample_context(hits=0)), llm)

    assert nodes == ["retrieve_context", "no_context"]
    assert llm.calls == []
    assert state["response"].answer.startswith("The available repository context is insufficient")
    assert state["response"].model is None


def test_a_citation_of_a_source_that_does_not_exist_is_reported() -> None:
    _, state = run(FakeRetrieval(), FakeLLM("See [1] and [99]."))  # only sources 1 and 2 exist

    assert state["response"].cited == (1,)
    assert state["response"].warnings == ("The answer cites [99], which match no retrieved source.",)


def test_an_answer_saying_the_context_is_insufficient_gets_a_warning() -> None:
    answer = "The available repository context is insufficient: nothing here handles payments."

    _, state = run(FakeRetrieval(), FakeLLM(answer))

    assert state["response"].answer == answer
    assert state["response"].warnings == (
        "The retrieved code did not contain what this question needs: the answer may be incomplete.",
    )


@pytest.mark.parametrize("retrieval_error, llm_error", [
    (VectorStoreUnavailableError("Qdrant is not reachable."), None),
    (None, LLMUnavailableError("The LLM provider is unavailable.")),
])  # fmt: skip
def test_an_error_in_a_step_stops_the_workflow_and_is_raised_unchanged(
    retrieval_error: Exception | None, llm_error: Exception | None
) -> None:
    workflow = build_chat_workflow(
        lambda: FakeRetrieval(error=retrieval_error),
        lambda: LLMGenerationService(FakeLLM(error=llm_error)),
    )

    with pytest.raises(type(retrieval_error or llm_error)) as raised:
        workflow.invoke(QUESTION)

    assert raised.value is (retrieval_error or llm_error)
