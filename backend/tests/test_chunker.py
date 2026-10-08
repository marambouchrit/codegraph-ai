"""Chunking: the code is cut along its structure, not into fixed-size pieces."""

from app.rag.chunker import CodeChunker
from app.rag.models import CodeChunk
from tests.helpers import FILES, PROJECT_ID, extraction_service, parser_service

PATH = "auth/service.py"


def chunk(source: str = FILES[PATH], max_chars: int = 2000) -> dict[str, CodeChunk]:
    """The chunks of the file, by qualified name (a split chunk keeps its first part)."""
    parsed = parser_service.parse_source(source.encode(), PATH)
    entities = extraction_service.extract(parsed, PROJECT_ID)
    chunks = CodeChunker(max_chars=max_chars).chunk_file(PROJECT_ID, source.encode(), entities)
    return {c.qualified_name: c for c in reversed(chunks)}


def test_a_method_is_one_chunk_with_its_whole_source() -> None:
    login = chunk()["AuthService.login"]

    assert (login.entity_type, login.start_line, login.end_line) == ("method", 14, 20)
    assert login.text.startswith("    def login(self, username, password):")
    assert login.text.endswith("return self.create_token(user)")


def test_a_class_is_a_skeleton_its_method_bodies_are_in_their_own_chunks() -> None:
    assert chunk()["AuthService"].text == (
        "class AuthService:\n"
        '    """Authentication service: logs users in and creates their access tokens."""\n\n'
        "    def login(self, username, password):\n        ...\n\n"
        "    def create_token(self, user):\n        ..."
    )


def test_a_chunk_carries_the_id_of_its_entity() -> None:
    """The link between Qdrant and Neo4j: a chunk's entity_id is the ID of a graph node."""
    login = chunk()["AuthService.login"]

    assert login.entity_id == f"{PROJECT_ID}:{PATH}:AuthService.login"
    assert login.id == f"{login.entity_id}|1"  # "<entity id>|<part number>"


def test_the_embedded_text_starts_with_a_header_naming_the_entity() -> None:
    login = chunk()["AuthService.login"]

    assert login.embedding_text == f"python method AuthService.login\nfile: {PATH}\n\n{login.text}"


def test_a_long_function_is_split_into_parts_that_overlap() -> None:
    body = "\n".join(f"    step_{i} = compute({i})" for i in range(40))
    source = f"def long_function():\n{body}\n"
    parsed = parser_service.parse_source(source.encode(), PATH)
    entities = extraction_service.extract(parsed, PROJECT_ID)

    parts = CodeChunker(max_chars=300, overlap_lines=3).chunk_file(PROJECT_ID, source.encode(), entities)

    assert len(parts) > 1 and all(len(part.text) <= 300 for part in parts)
    assert [part.part for part in parts] == list(range(1, len(parts) + 1))
    # The next part starts 3 lines before the end of the previous one.
    assert parts[1].start_line == parts[0].end_line - 2
