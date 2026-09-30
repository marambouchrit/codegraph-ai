"""CodeChunker: code-aware chunks built from real Phase 3-4 output, in four languages."""

import pytest

from app.rag.chunker import CodeChunker, chunk_id
from app.rag.models import CodeChunk
from tests.relationship_helpers import extraction_service, parser_service

PROJECT = "a" * 32

PYTHON = '''import os

LIMIT = 3


@decorator
def helper(x):
    def inner():
        return x
    return inner


class User:
    """A user."""

    role = "admin"

    def save(self):
        return 1

    def delete(self):
        pass

    def ping(self): return "pong"
'''

JAVA = '''package com.shop;

import java.util.List;

public class User {
    private String name;

    @Override
    public String toString() {
        return name;
    }

    interface Listener { void changed(); }
}
'''

JAVASCRIPT = '''import { get } from "./http";

export function load(id) {
  return get(`/items/${id}`);
}

export const save = (item) => {
  return post("/items", item);
};

class Store {
  constructor() { this.items = []; }
  add(item) {
    this.items.push(item);
  }
}
'''

TYPESCRIPT = '''export interface CartItem {
  sku: string;
}

export class Cart {
  private items: CartItem[] = [];

  total(): number {
    return 0;
  }
}
'''


def chunk(path: str, source: str, chunker: CodeChunker | None = None) -> list[CodeChunk]:
    parse_result = parser_service.parse_source(source.encode(), path)
    file_entities = extraction_service.extract(parse_result, PROJECT)
    return (chunker or CodeChunker()).chunk_file(PROJECT, parse_result.source, file_entities)


def summary(chunks: list[CodeChunk]) -> list[tuple[str, str, int, int]]:
    return [(c.entity_type, c.qualified_name, c.start_line, c.end_line) for c in chunks]


def by_name(chunks: list[CodeChunk], qualified_name: str) -> CodeChunk:
    [found] = [c for c in chunks if c.qualified_name == qualified_name]
    return found


# ----- One chunk per meaningful unit, per language -----


def test_python_chunks() -> None:
    chunks = chunk("src/models/user.py", PYTHON)

    assert summary(chunks) == [
        ("file", "src/models/user.py", 1, 14),
        ("function", "helper", 6, 10),
        ("class", "User", 13, 24),
        ("method", "User.save", 18, 19),
        ("method", "User.delete", 21, 22),
    ]
    # Nested function: inside its function's chunk, not a chunk of its own.
    assert "def inner():" in by_name(chunks, "helper").text
    assert by_name(chunks, "helper").text.startswith("@decorator\ndef helper(x):")


def test_class_chunk_is_a_skeleton_without_method_bodies() -> None:
    user = by_name(chunk("src/models/user.py", PYTHON), "User")

    assert user.text == (
        'class User:\n    """A user."""\n\n    role = "admin"\n\n'
        "    def save(self):\n        ...\n\n    def delete(self):\n        ...\n\n"
        '    def ping(self): return "pong"'  # a one-liner is shown whole, not collapsed
    )


def test_file_chunk_keeps_module_code_and_collapses_definitions() -> None:
    file = chunk("src/models/user.py", PYTHON)[0]

    assert file.text == (
        "import os\n\nLIMIT = 3\n\n\n@decorator\ndef helper(x):\n    ...\n\n\nclass User:\n    ..."
    )
    assert (file.entity_id, file.name) == (f"{PROJECT}:src/models/user.py", "user.py")


def test_java_chunks() -> None:
    chunks = chunk("src/User.java", JAVA)

    assert summary(chunks) == [
        ("file", "src/User.java", 1, 6),
        ("class", "User", 5, 14),
        ("method", "User.toString", 8, 11),
    ]
    assert by_name(chunks, "User.toString").text.startswith("    @Override\n    public String")
    # The one-line interface (and its method) are shown in full inside the class chunk.
    assert "interface Listener { void changed(); }" in by_name(chunks, "User").text
    assert "        ..." in by_name(chunks, "User").text


def test_javascript_chunks() -> None:
    chunks = chunk("web/api.js", JAVASCRIPT)

    assert summary(chunks) == [
        ("file", "web/api.js", 1, 12),
        ("function", "load", 3, 5),
        ("function", "save", 7, 9),
        ("class", "Store", 11, 16),
        ("method", "Store.add", 13, 15),
    ]
    assert chunks[0].text.startswith('import { get } from "./http";')
    assert {c.language for c in chunks} == {"javascript"}


def test_typescript_chunks_skip_a_file_with_only_definitions() -> None:
    chunks = chunk("web/cart.ts", TYPESCRIPT)

    assert summary(chunks) == [
        ("interface", "CartItem", 1, 3),
        ("class", "Cart", 5, 11),
        ("method", "Cart.total", 8, 10),
    ]
    assert {c.language for c in chunks} == {"typescript"}


# ----- Metadata and IDs -----


def test_metadata_is_complete() -> None:
    save = by_name(chunk("src/models/user.py", PYTHON), "User.save")

    assert save == CodeChunk(
        id=f"{PROJECT}:src/models/user.py:User.save|1",
        project_id=PROJECT,
        file_path="src/models/user.py",
        language="python",
        entity_id=f"{PROJECT}:src/models/user.py:User.save",
        entity_type="method",
        name="save",
        qualified_name="User.save",
        start_line=18,
        end_line=19,
        text="    def save(self):\n        return 1",
    )
    assert save.embedding_text == (
        "python method User.save\nfile: src/models/user.py\n\n    def save(self):\n        return 1"
    )


def test_chunk_ids_are_deterministic_and_unique() -> None:
    first = chunk("src/models/user.py", PYTHON)
    again = chunk("src/models/user.py", PYTHON)
    moved = chunk("src/models/user.py", "\n\n\n" + PYTHON)  # same code, other lines

    assert [c.id for c in first] == [c.id for c in again]
    assert [c.id for c in moved] == [c.id for c in first]  # IDs don't depend on lines
    assert len({c.id for c in first}) == len(first)
    assert chunk_id("p:a.py:f", 2) == "p:a.py:f|2"


def test_every_chunk_id_is_its_entity_id_and_part() -> None:
    for c in chunk("web/api.js", JAVASCRIPT):
        assert c.id == f"{c.entity_id}|{c.part}"
        assert c.entity_id.startswith(f"{PROJECT}:{c.file_path}")


# ----- Edge cases -----


def test_empty_and_blank_files_give_no_chunks() -> None:
    assert chunk("src/empty.py", "") == []
    assert chunk("src/blank.py", "\n\n   \n") == []


def test_file_without_definitions_is_one_chunk() -> None:
    [only] = chunk("scripts/run.py", "import sys\n\nprint(sys.argv)\n")

    assert (only.entity_type, only.start_line, only.end_line) == ("file", 1, 3)


def test_syntax_errors_still_give_chunks() -> None:
    broken = "def ok():\n    return 1\n\ndef broken(:\n    pass\n\nclass Fine:\n    def run(self):\n        return 2\n"
    chunks = chunk("src/broken.py", broken)

    names = {c.qualified_name for c in chunks}
    assert {"ok", "Fine.run"} <= names
    assert all(c.text.strip() for c in chunks)


def test_large_entity_is_split_into_overlapping_parts() -> None:
    body = "".join(f"    total = total + {i}  # step {i}\n" for i in range(200))
    source = f"def big():\n    total = 0\n{body}    return total\n"
    chunks = chunk("src/big.py", source, CodeChunker(max_chars=500, overlap_lines=2))

    assert len(chunks) > 5
    assert all(len(c.text) <= 500 for c in chunks)
    assert [c.part for c in chunks] == list(range(1, len(chunks) + 1))
    assert {c.part_count for c in chunks} == {len(chunks)}
    assert {c.entity_id for c in chunks} == {f"{PROJECT}:src/big.py:big"}
    assert [c.id for c in chunks] == [f"{PROJECT}:src/big.py:big|{n}" for n in range(1, len(chunks) + 1)]
    assert chunks[0].start_line == 1 and chunks[-1].end_line == 203
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.start_line == previous.end_line - 1  # 2 lines of overlap
        assert previous.text.splitlines()[-2:] == current.text.splitlines()[:2]
    assert "(part 2/" in chunks[1].embedding_text


def test_very_long_lines_are_cut() -> None:
    source = "DATA = [" + ", ".join(str(i) for i in range(2000)) + "]\n"
    chunks = chunk("src/data.py", source, CodeChunker(max_chars=300, overlap_lines=0))

    assert len(chunks) > 1
    assert all(len(c.text) <= 300 and c.start_line == 1 for c in chunks)
    assert "".join(c.text for c in chunks) == source.rstrip("\n")


def test_windows_line_endings_and_odd_characters_keep_line_numbers() -> None:
    source = "# page\x0cbreak\r\ndef a():\r\n    return 1\r\n"
    [a] = chunk("src/crlf.py", source)[1:]

    assert (a.start_line, a.end_line, a.text) == (2, 3, "def a():\n    return 1")


def test_chunker_limits_are_validated() -> None:
    with pytest.raises(ValueError):
        CodeChunker(max_chars=10)
    with pytest.raises(ValueError):
        CodeChunker(overlap_lines=-1)
    with pytest.raises(ValueError):
        CodeChunker(overlap_lines=500)
