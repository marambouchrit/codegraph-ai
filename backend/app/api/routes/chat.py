"""Chat endpoint: ask a question about an imported project, get a grounded, cited answer.

The route only translates HTTP to a service call and back: validation of the body is
done by the ChatRequest schema, the workflow by ChatService, and every error by the
global AppError handler (app/main.py).
"""

from fastapi import APIRouter, Depends, Path

from app.api.dependencies import get_chat_service
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

router = APIRouter(prefix="/projects", tags=["chat"])

ERRORS = {
    404: {"description": "Unknown project, or an invalid project ID"},
    422: {"description": "Invalid body: missing, empty or too long question, unknown field"},
    502: {"description": "The LLM returned no usable answer (empty or declined)"},
    503: {"description": "Qdrant, the embedding model or the LLM is unavailable or misconfigured"},
}


# A plain `def`: retrieval and the LLM call are blocking, FastAPI runs it in a thread pool.
@router.post(
    "/{project_id}/chat",
    response_model=ChatResponse,
    summary="Ask a question about a project",
    responses=ERRORS,  # type: ignore[arg-type]
)
def chat(
    request: ChatRequest,
    project_id: str = Path(description="ID returned when the project was imported"),
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    """Answer a natural-language question about the project's code.

    The code relevant to the question is found by semantic search (Qdrant), connected
    through the knowledge graph (Neo4j), and given to the LLM, which answers only from it
    and cites its sources as [id]. Each source gives the file and line range.

    The project must have been analyzed and indexed first; otherwise the answer says the
    available context is insufficient. If Neo4j is down, the answer relies on semantic
    search only and `graph_status` says so.
    """
    return ChatResponse.from_assistant(service.ask(project_id, request.question))
