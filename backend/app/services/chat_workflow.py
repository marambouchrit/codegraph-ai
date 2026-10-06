"""The chat workflow as a LangGraph state graph: one node per step, one shared state.

    START
      │
    retrieve_context      GraphRAGService: Qdrant hits expanded in Neo4j        (Phase 9)
      │
      ├─ nothing retrieved ──▶ no_context ──▶ END      the "insufficient" answer, no LLM call
      │
    build_prompt          PromptBuilder: context -> prompt, numbered sources   (Phase 10)
      │
    generate              LLMProvider: prompt -> text
      │
    verify_citations      the answer's [n] checked against the numbered sources
      │
     END

Each node reads the state, does one thing through an existing service, and returns the keys
it adds. The graph only decides the order and the branch: no Cypher, no Qdrant call, no
prompt text and no provider SDK here.

The model has no tools and the graph has no cycle: every question runs each step at most
once. An error raised by a node (Qdrant down, LLM unavailable...) stops the run and
propagates unchanged.
"""

from collections.abc import Callable
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graphrag.models import GraphRAGContext
from app.llm.generator import LLMGenerationService
from app.llm.models import AssistantResponse, LLMCompletion, Prompt


class ContextRetriever(Protocol):
    """What the chat needs from GraphRAGService."""

    def build_context(self, project_id: str, query: str) -> GraphRAGContext: ...


class ChatState(TypedDict, total=False):
    """What the nodes share. The first two keys are the input; each node adds the next."""

    project_id: str
    question: str
    context: GraphRAGContext  # retrieve_context
    prompt: Prompt  # build_prompt
    completion: LLMCompletion  # generate
    response: AssistantResponse  # verify_citations, or no_context


def build_chat_workflow(
    graphrag: Callable[[], ContextRetriever],
    generator: Callable[[], LLMGenerationService],
) -> CompiledStateGraph:
    """The compiled graph. The services are factories, called by the nodes that need them."""

    def retrieve_context(state: ChatState) -> ChatState:
        return {"context": graphrag().build_context(state["project_id"], state["question"])}

    def route(state: ChatState) -> Literal["no_context", "build_prompt"]:
        return "build_prompt" if state["context"].vector_results else "no_context"

    def no_context(state: ChatState) -> ChatState:
        return {"response": generator().nothing_retrieved(state["context"])}

    def build_prompt(state: ChatState) -> ChatState:
        return {"prompt": generator().prompt_builder.build(state["context"])}

    def generate(state: ChatState) -> ChatState:
        prompt = state["prompt"]
        return {"completion": generator().provider.generate(prompt.system, prompt.user)}

    def verify_citations(state: ChatState) -> ChatState:
        response = generator().respond(state["context"], state["prompt"], state["completion"])
        return {"response": response}

    workflow = StateGraph(ChatState)
    workflow.add_node("retrieve_context", retrieve_context)
    workflow.add_node("no_context", no_context)
    workflow.add_node("build_prompt", build_prompt)
    workflow.add_node("generate", generate)
    workflow.add_node("verify_citations", verify_citations)

    workflow.add_edge(START, "retrieve_context")
    workflow.add_conditional_edges("retrieve_context", route, ["no_context", "build_prompt"])
    workflow.add_edge("no_context", END)
    workflow.add_edge("build_prompt", "generate")
    workflow.add_edge("generate", "verify_citations")
    workflow.add_edge("verify_citations", END)
    return workflow.compile()
