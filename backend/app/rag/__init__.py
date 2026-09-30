"""Vector RAG (Phase 8): source code -> chunks -> embeddings -> Qdrant -> semantic search.

Independent from the knowledge graph (`app/graph/`): it reads the project's source
files through Phases 2-4 and never talks to Neo4j.
"""
