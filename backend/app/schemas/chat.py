"""API models (request and response bodies) for the chat endpoint.

The client sends a natural-language question, nothing else: unknown fields are
rejected, so no Cypher, Qdrant filter, file path, model or system prompt can be passed.
The response is built from the Phase 10 `AssistantResponse`; internal details
(entity IDs, vector scores, graph queries) are not exposed.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.graphrag.models import GraphStatus, SourceReason
from app.llm.models import AssistantResponse

# Same limit as the vector search the question goes to (VectorRetrievalService).
MAX_QUESTION_CHARS = 2000


class ChatRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"question": "How is authentication implemented?"}]},
    )

    question: str = Field(
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="A question about the project's code, in natural language.",
    )

    @field_validator("question")
    @classmethod
    def not_blank(cls, question: str) -> str:
        question = question.strip()
        if not question:
            raise ValueError("The question must not be empty.")
        return question


class ChatSource(BaseModel):
    id: int = Field(description="The number the answer cites as [id]")
    entity: str = Field(description="Qualified name, e.g. AuthService.login")
    entity_type: str = Field(description="file, class, interface, function or method")
    file: str = Field(description="Path in the repository")
    start_line: int
    end_line: int
    found_by: Literal["semantic_search", "graph"] = Field(
        description="semantic_search: a code chunk matching the question; "
        "graph: connected to one in the knowledge graph"
    )
    cited: bool = Field(description="Whether the answer cites this source")


class ChatResponse(BaseModel):
    question: str
    answer: str = Field(description="Markdown, citing sources as [id]")
    sources: list[ChatSource] = Field(description="Every source given to the model, by id")
    cited: list[int] = Field(description="Source ids cited by the answer, in order")
    graph_status: GraphStatus = Field(
        description="complete, or partial / unavailable when Neo4j failed during retrieval "
        "(the answer then relies on semantic search only)"
    )
    warnings: list[str]
    model: str | None = Field(description="The model that answered; null if none was called")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{
                "question": "How is authentication implemented?",
                "answer": "`AuthService.login` checks the password [1] after loading the user "
                "with `UserRepository.find_user` [2].",
                "sources": [
                    {"id": 1, "entity": "AuthService.login", "entity_type": "method",
                     "file": "app/auth/service.py", "start_line": 14, "end_line": 20,
                     "found_by": "semantic_search", "cited": True},
                    {"id": 2, "entity": "UserRepository.find_user", "entity_type": "method",
                     "file": "app/repository/user.py", "start_line": 7, "end_line": 9,
                     "found_by": "graph", "cited": True},
                ],
                "cited": [1, 2],
                "graph_status": "complete",
                "warnings": [],
                "model": "gemini-flash-lite-latest",
            }]
        }
    )  # fmt: skip

    @classmethod
    def from_assistant(cls, response: AssistantResponse) -> "ChatResponse":
        cited = set(response.cited)
        return cls(
            question=response.question,
            answer=response.answer,
            sources=[
                ChatSource(
                    id=item.number,
                    entity=item.source.qualified_name,
                    entity_type=item.source.entity_type,
                    file=item.source.file_path,
                    start_line=item.source.start_line,
                    end_line=item.source.end_line,
                    found_by="semantic_search"
                    if item.source.reason == SourceReason.VECTOR
                    else "graph",
                    cited=item.number in cited,
                )
                for item in response.sources
            ],
            cited=list(response.cited),
            graph_status=response.graph_status,
            warnings=list(response.warnings),
            model=response.model,
        )
