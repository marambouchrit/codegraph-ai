"""Cut source files into code-aware chunks, using the entities of Phase 4.

A fixed-size text splitter would cut functions in half and glue unrelated code
together. Here chunks follow the code structure found by Phases 3-4:

    File src/auth/service.py
    ├── Class AuthService           -> 1 "skeleton" chunk: the class with each method
    │   ├── Method login               body replaced by "..." (fields, signatures, docs)
    │   └── Method create_token     -> 1 chunk per method: its full source
    ├── Function hash_password      -> 1 chunk: its full source
    └── (imports, constants...)     -> 1 file chunk: module-level code, with each class
                                       and function collapsed to its first line

Every line of code is in exactly one chunk body; only first lines (signatures) are
repeated as context. Functions or classes defined *inside* a function stay in that
function's chunk. A chunk longer than `max_chars` is split on line boundaries into
parts that repeat `overlap_lines` lines, so no part loses its context entirely.

Line numbers are those of Phase 4: 1-based and inclusive. The chunker only reads
the source text it is given; it never runs anything.
"""

from collections import defaultdict
from dataclasses import dataclass

from app.extraction.models import Entity, EntityType, FileEntities
from app.rag.models import CodeChunk

CALLABLES = frozenset({EntityType.FUNCTION, EntityType.METHOD})
TYPES = frozenset({EntityType.CLASS, EntityType.INTERFACE})

DEFAULT_MAX_CHARS = 2000
DEFAULT_OVERLAP_LINES = 3
MIN_MAX_CHARS = 200
MAX_OVERLAP_LINES = 50

Line = tuple[int, str]  # (line number in the file, text)


@dataclass(frozen=True)
class _Unit:
    """An entity that gets its own chunk, and the child units collapsed inside it."""

    entity: Entity
    collapsed: tuple[Entity, ...]


class CodeChunker:
    def __init__(
        self, max_chars: int = DEFAULT_MAX_CHARS, overlap_lines: int = DEFAULT_OVERLAP_LINES
    ) -> None:
        if max_chars < MIN_MAX_CHARS:
            raise ValueError(f"max_chars must be at least {MIN_MAX_CHARS}")
        if not 0 <= overlap_lines <= MAX_OVERLAP_LINES:
            raise ValueError(f"overlap_lines must be from 0 to {MAX_OVERLAP_LINES}")
        self.max_chars = max_chars
        self.overlap_lines = overlap_lines

    def chunk_file(
        self, project_id: str, source: bytes, file_entities: FileEntities
    ) -> list[CodeChunk]:
        """The chunks of one file, in source order (a file with no code gives none)."""
        # Split on "\n" only, like Tree-sitter: str.splitlines() would also split on
        # characters such as \x0c or   and shift every line number after them.
        lines = [line.removesuffix("\r") for line in source.decode("utf-8", "replace").split("\n")]
        chunks: list[CodeChunk] = []
        for unit in _units(file_entities, lines):
            if unit.entity.type == EntityType.FILE and not _has_module_code(lines, unit):
                continue  # only definitions, each in its own chunk: nothing more to say
            numbered = _collapse(lines, unit)
            chunks.extend(self._chunks(project_id, file_entities, unit.entity, numbered))
        return chunks

    def _chunks(
        self, project_id: str, file: FileEntities, entity: Entity, numbered: list[Line]
    ) -> list[CodeChunk]:
        numbered = _trim_blank(numbered)
        if not numbered:
            return []
        parts = _windows(_split_long_lines(numbered, self.max_chars), self.max_chars,
                         self.overlap_lines)  # fmt: skip
        return [
            CodeChunk(
                id=chunk_id(entity.id, index),
                project_id=project_id,
                file_path=file.path,
                language=file.language.value,
                entity_id=entity.id,
                entity_type=entity.type.value,
                name=entity.name,
                qualified_name=entity.qualified_name,
                start_line=part[0][0],
                end_line=part[-1][0],
                text="\n".join(text for _, text in part),
                part=index,
                part_count=len(parts),
            )
            for index, part in enumerate(parts, start=1)
        ]


def chunk_id(entity_id: str, part: int) -> str:
    """The chunk ID "<entity_id>|<part>": stable while the entity keeps its name and file."""
    return f"{entity_id}|{part}"


# ----- Which entities become chunks -----


def _units(file_entities: FileEntities, lines: list[str]) -> list[_Unit]:
    """The file, then every class/interface/function/method with a chunk of its own.

    An entity gets no chunk of its own when its parent's chunk already shows all of
    it: when it is defined inside a function (the function's chunk is its full
    source), or when it fits on its signature lines (`def ping(self): return 1`,
    `interface Listener { void changed(); }`), which a collapsed parent keeps.
    """
    entities = file_entities.entities
    by_id = {entity.id: entity for entity in entities}
    children: dict[str, list[Entity]] = defaultdict(list)
    for entity in entities:
        if entity.parent_id is not None:
            children[entity.parent_id].append(entity)

    def shown_by_parent(entity: Entity) -> bool:
        if entity.end_line <= _signature_end(lines, entity):
            return True
        parent = by_id.get(entity.parent_id) if entity.parent_id else None
        while parent is not None:
            if parent.type in CALLABLES or parent.end_line <= _signature_end(lines, parent):
                return True
            parent = by_id.get(parent.parent_id) if parent.parent_id else None
        return False

    chunked = [
        entity
        for entity in entities[1:]
        if (entity.type in CALLABLES or entity.type in TYPES) and not shown_by_parent(entity)
    ]
    chunked_ids = {entity.id for entity in chunked}
    units = []
    for entity in [file_entities.file, *chunked]:
        if entity.type in CALLABLES:
            units.append(_Unit(entity, ()))  # full source, nested definitions included
        else:  # file, class, interface: their chunked children are collapsed
            collapsed = tuple(c for c in children[entity.id] if c.id in chunked_ids)
            units.append(_Unit(entity, collapsed))
    return units


# ----- Building the text -----


SIGNATURE_MAX_LINES = 5


def _has_module_code(lines: list[str], unit: _Unit) -> bool:
    """Is there a non-blank line outside the file's collapsed definitions (imports...)?"""
    covered = set()
    for child in unit.collapsed:
        covered.update(range(child.start_line, child.end_line + 1))
    return any(
        text.strip() for number, text in enumerate(lines, start=1) if number not in covered
    )


def _collapse(lines: list[str], unit: _Unit) -> list[Line]:
    """The unit's lines, each collapsed child reduced to its signature and "...".

    A file unit covers the whole file; the others cover their entity's lines.
    """
    entity = unit.entity
    first = 1 if entity.type == EntityType.FILE else entity.start_line
    last = len(lines) if entity.type == EntityType.FILE else min(entity.end_line, len(lines))
    # First line of a collapsed child -> (last line kept, last line of the child).
    collapsed: dict[int, tuple[int, int]] = {}
    for child in unit.collapsed:
        kept = _signature_end(lines, child)
        previous_kept, previous_end = collapsed.get(child.start_line, (0, 0))
        collapsed[child.start_line] = (max(kept, previous_kept), max(child.end_line, previous_end))

    numbered: list[Line] = []
    line_number = first
    while line_number <= last:
        if line_number not in collapsed:
            numbered.append((line_number, lines[line_number - 1]))
            line_number += 1
            continue
        kept, end = collapsed[line_number]
        signature = [(n, lines[n - 1]) for n in range(line_number, min(kept, last) + 1)]
        numbered.extend(signature)
        if end > kept:
            text = signature[-1][1]
            indent = text[: len(text) - len(text.lstrip())]
            numbered.append((kept + 1, f"{indent}    ..."))
        line_number = max(end, kept) + 1
    return numbered


def _signature_end(lines: list[str], entity: Entity) -> int:
    """The line naming the entity: decorators and annotations above it are kept too."""
    last = min(entity.end_line, entity.start_line + SIGNATURE_MAX_LINES - 1, len(lines))
    for line_number in range(entity.start_line, last + 1):
        if entity.name in lines[line_number - 1]:
            return line_number
    return entity.start_line


def _trim_blank(numbered: list[Line]) -> list[Line]:
    start, end = 0, len(numbered)
    while start < end and not numbered[start][1].strip():
        start += 1
    while end > start and not numbered[end - 1][1].strip():
        end -= 1
    return numbered[start:end]


def _split_long_lines(numbered: list[Line], max_chars: int) -> list[Line]:
    """Cut lines longer than max_chars (minified or generated code) into pieces."""
    result: list[Line] = []
    for line_number, text in numbered:
        if len(text) <= max_chars:
            result.append((line_number, text))
        else:
            result.extend(
                (line_number, text[start : start + max_chars])
                for start in range(0, len(text), max_chars)
            )
    return result


def _windows(numbered: list[Line], max_chars: int, overlap: int) -> list[list[Line]]:
    """Consecutive groups of lines of at most max_chars characters, overlapping by
    `overlap` lines. A text that fits gives exactly one group."""
    windows: list[list[Line]] = []
    start = 0
    while start < len(numbered):
        size, end = 0, start
        while end < len(numbered):
            line_size = len(numbered[end][1]) + 1  # + the newline
            if end > start and size + line_size > max_chars:
                break
            size += line_size
            end += 1
        windows.append(numbered[start:end])
        if end >= len(numbered):
            break
        start = max(end - overlap, start + 1)  # always move forward
    return windows
