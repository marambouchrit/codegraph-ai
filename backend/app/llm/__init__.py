"""LLM assistant (Phase 10): turn a GraphRAG context into a grounded, cited answer.

    GraphRAGContext (Phase 9) -> PromptBuilder -> LLMProvider -> AssistantResponse

Generation only: nothing here queries Neo4j or Qdrant, embeds text or retrieves code.
Retrieval stays in GraphRAGService; this package only reads its result.
"""
