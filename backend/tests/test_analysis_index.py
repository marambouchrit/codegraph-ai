"""Phase 14: content hashes, change detection and the persisted analysis index."""

import hashlib
from pathlib import Path

from app.analysis.changes import FileChanges, detect_changes, file_sha256, hash_files
from app.analysis.index import (
    AnalysisIndex,
    AnalysisIndexStore,
    ChunkRecord,
    EdgeRecord,
    FileRecord,
    chunk_content_hash,
    fingerprint,
)
from app.extraction.service import EntityExtractionService
from app.graph.builder import GraphDiff, diff_graph, edge_fingerprint, node_fingerprint
from app.graph.models import GraphEdge, GraphNode
from app.ingestion.languages import Language
from app.ingestion.scanner import ScannedFile
from app.ingestion.workspace import Workspace, new_project_id
from app.parsing.service import ParserService
from app.relationships.service import RelationshipExtractionService

# ----- File hashes -----


def test_the_hash_is_the_sha256_of_the_content(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_bytes(b"def a():\n    return 1\n")

    assert file_sha256(path) == hashlib.sha256(b"def a():\n    return 1\n").hexdigest()


def test_the_hash_depends_on_content_only(tmp_path: Path) -> None:
    first, second, other = tmp_path / "a.py", tmp_path / "b.py", tmp_path / "c.py"
    first.write_bytes(b"x = 1\n")
    second.write_bytes(b"x = 1\n")  # another name and date, same bytes
    other.write_bytes(b"x = 1 \n")  # one more space

    assert file_sha256(first) == file_sha256(second) != file_sha256(other)


def test_large_files_are_hashed_in_blocks(tmp_path: Path) -> None:
    content = b"0123456789" * 300_000  # 3 MB: several blocks
    path = tmp_path / "big.py"
    path.write_bytes(content)

    assert file_sha256(path) == hashlib.sha256(content).hexdigest()


def test_hash_files_uses_the_scanned_paths(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_bytes(b"a")
    files = [ScannedFile("pkg/a.py", Language.PYTHON, 1)]

    assert hash_files(tmp_path, files) == {"pkg/a.py": hashlib.sha256(b"a").hexdigest()}


# ----- Change detection -----


def test_every_file_is_added_modified_unchanged_or_deleted() -> None:
    previous = {"same.py": "1", "edited.py": "2", "removed.py": "3"}
    current = {"same.py": "1", "edited.py": "22", "new.py": "4"}

    changes = detect_changes(previous, current)

    assert changes == FileChanges(added=("new.py",), modified=("edited.py",),
                                  unchanged=("same.py",), deleted=("removed.py",))  # fmt: skip
    assert changes.to_analyze == {"new.py", "edited.py"} and changes.any


def test_a_first_analysis_adds_everything_and_no_change_is_no_change() -> None:
    hashes = {"b.py": "2", "a.py": "1"}

    first = detect_changes({}, hashes)
    again = detect_changes(hashes, hashes)

    assert first.added == ("a.py", "b.py") and first.to_analyze == {"a.py", "b.py"}  # sorted
    assert again.unchanged == ("a.py", "b.py") and not again.any and not again.to_analyze


def test_a_renamed_file_is_a_deletion_and_an_addition() -> None:
    changes = detect_changes({"old.py": "1"}, {"new.py": "1"})

    assert (changes.added, changes.deleted, changes.unchanged) == (("new.py",), ("old.py",), ())


# ----- Fingerprints -----


def test_fingerprints_ignore_key_order_and_see_every_value() -> None:
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert fingerprint({"a": None}) != fingerprint({})


def test_a_chunk_hash_depends_on_the_model_and_the_text() -> None:
    assert chunk_content_hash("m", "code") == chunk_content_hash("m", "code")
    assert chunk_content_hash("m", "code") != chunk_content_hash("m", "code ")
    assert chunk_content_hash("m", "code") != chunk_content_hash("other", "code")


# ----- Graph differences -----


def node(name: str, line: int = 1) -> GraphNode:
    return GraphNode(name, "Function", {"id": name, "start_line": line})


def edge(source: str, target: str) -> GraphEdge:
    edge_id = f"CALLS:{source}->{target}"
    return GraphEdge(edge_id, "CALLS", source, target, {"id": edge_id})


def test_a_graph_diff_writes_what_differs_and_deletes_what_is_gone() -> None:
    old_nodes = [node("a"), node("b"), node("gone")]
    old_edges = [edge("a", "b"), edge("a", "gone")]
    previous_nodes = {n.id: node_fingerprint(n) for n in old_nodes}
    previous_edges = {e.id: (edge_fingerprint(e), e.source_id) for e in old_edges}

    diff = diff_graph(
        [node("a"), node("b", line=9), node("new")],  # b moved, "gone" removed, "new" added
        [edge("a", "b"), edge("new", "a")],
        previous_nodes, previous_edges,
    )  # fmt: skip

    assert [n.id for n in diff.nodes_to_write] == ["b", "new"]  # "a" is untouched
    assert [e.id for e in diff.edges_to_write] == ["CALLS:new->a"]
    assert diff.nodes_to_delete == ("gone",)
    assert diff.edges_to_delete == (("CALLS:a->gone", "a"),)


def test_an_identical_graph_has_an_empty_diff() -> None:
    nodes, edges = [node("a"), node("b")], [edge("a", "b")]

    diff = diff_graph(nodes, edges, {n.id: node_fingerprint(n) for n in nodes},
                      {e.id: (edge_fingerprint(e), e.source_id) for e in edges})  # fmt: skip

    assert diff == GraphDiff((), (), (), ())


# ----- The persisted index -----


def analyzed_file(root: Path, project_id: str) -> FileRecord:
    """A real file, parsed by the real extraction and reference collection."""
    (root / "auth.py").write_text(
        "from models import User\n\n\nclass Auth(User):\n    def login(self, name):\n"
        "        user = User()\n        return user.save(name)\n", encoding="utf-8",
    )  # fmt: skip
    extraction = EntityExtractionService(ParserService(max_file_bytes=1_000_000))
    relationships = RelationshipExtractionService(extraction)
    parse_result = extraction.parser_service.parse_file(root, "auth.py", Language.PYTHON)
    entities = extraction.extract(parse_result, project_id)
    return FileRecord(
        sha256="abc",
        entities=entities,
        references=relationships.collect(parse_result, entities, project_id),
        chunks={"chunk|1": ChunkRecord("content", "payload", "method")},
    )


def test_the_index_survives_a_save_and_load(tmp_path: Path) -> None:
    """Entities, references (imports, calls, variable types) and fingerprints come back equal."""
    workspace = Workspace(tmp_path)
    project_id = new_project_id()
    workspace.create_project_dir(project_id)
    record = analyzed_file(tmp_path, project_id)
    assert record.references and record.references.imports and record.references.variables
    index = AnalysisIndex(
        embedding_model="test/model", collection="chunks", chunk_max_chars=2000,
        chunk_overlap_lines=3,
        files={"auth.py": record, "bad.py": FileRecord(sha256="def", failure="Cannot be parsed.")},
        nodes={"n1": "h1"}, edges={"e1": EdgeRecord("h2", "n1")},
    )  # fmt: skip
    store = AnalysisIndexStore(workspace)

    store.save(project_id, index)
    loaded = store.load(project_id)

    assert loaded == index
    assert loaded.file_hashes == {"auth.py": "abc", "bad.py": "def"}
    assert loaded.chunks == {"chunk|1": ChunkRecord("content", "payload", "method")}
    # Types are restored, not just their text.
    restored = loaded.files["auth.py"]
    assert restored.entities is not None and restored.references is not None
    assert restored.entities.language is Language.PYTHON
    assert isinstance(restored.references.references[0].parts, tuple)


def test_a_missing_unreadable_or_foreign_index_is_no_index(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    project_id, other = new_project_id(), new_project_id()
    for created in (project_id, other):
        workspace.create_project_dir(created)
    store = AnalysisIndexStore(workspace)
    assert store.load(project_id) is None  # missing

    workspace.analysis_index_file(project_id).write_text("{broken")
    assert store.load(project_id) is None

    store.save(other, AnalysisIndex("m", "c", 2000, 3))
    workspace.analysis_index_file(project_id).write_text(
        workspace.analysis_index_file(other).read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert store.load(project_id) is None  # written for another project
    assert store.load(other) is not None

    store.clear(other)
    store.clear(other)  # clearing twice is fine
    assert store.load(other) is None
