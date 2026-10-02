"""A small Java project, analyzed by hand, for the graph retrieval tests.

Written to Neo4j by the real Phase 6 GraphBuilder, it gives answers that can be
read off this map:

    src/model/User.java            class User { save(), delete(), validate() }
    src/model/Admin.java           class Admin extends User { ban() }
    src/service/IService.java      interface IService { run() }
    src/service/UserService.java   class UserService implements IService { run(), login() }
    src/service/AuditService.java  class AuditService implements IService { run() }
    src/util/Log.java              class Log { write() }
    src/util/Config.java           class Config

    CALLS       login -> save, login -> validate, ban -> delete, delete -> save,
                save -> write, validate -> write
    DEPENDS_ON  UserService.java -> User.java -> Log.java -> Config.java
                UserService.java -> IService.java, Admin.java -> User.java,
                AuditService.java -> IService.java
    IMPORTS     the same file pairs, except Log.java -> Config.java

IDs below are short (without the "<project_id>:" prefix): full() adds it.
"""

from collections.abc import Callable
from typing import Any

from app.extraction.models import Entity, EntityType, ExtractionReport, FileEntities
from app.ingestion.languages import Language
from app.relationships.models import (
    Relationship,
    RelationshipReport,
    RelationshipType,
    relationship_id,
)
from app.services.graph_retrieval_service import GraphRetrievalService

FILES: dict[str, list[tuple[EntityType, str]]] = {
    "src/model/User.java": [
        (EntityType.CLASS, "User"),
        (EntityType.METHOD, "User.save"),
        (EntityType.METHOD, "User.delete"),
        (EntityType.METHOD, "User.validate"),
    ],
    "src/model/Admin.java": [(EntityType.CLASS, "Admin"), (EntityType.METHOD, "Admin.ban")],
    "src/service/IService.java": [
        (EntityType.INTERFACE, "IService"),
        (EntityType.METHOD, "IService.run"),
    ],
    "src/service/UserService.java": [
        (EntityType.CLASS, "UserService"),
        (EntityType.METHOD, "UserService.run"),
        (EntityType.METHOD, "UserService.login"),
    ],
    "src/service/AuditService.java": [
        (EntityType.CLASS, "AuditService"),
        (EntityType.METHOD, "AuditService.run"),
    ],
    "src/util/Log.java": [(EntityType.CLASS, "Log"), (EntityType.METHOD, "Log.write")],
    "src/util/Config.java": [(EntityType.CLASS, "Config")],
}

USER_FILE = "src/model/User.java"
ADMIN_FILE = "src/model/Admin.java"
ISERVICE_FILE = "src/service/IService.java"
USER_SERVICE_FILE = "src/service/UserService.java"
AUDIT_FILE = "src/service/AuditService.java"
LOG_FILE = "src/util/Log.java"
CONFIG_FILE = "src/util/Config.java"

USER = f"{USER_FILE}:User"
SAVE = f"{USER_FILE}:User.save"
DELETE = f"{USER_FILE}:User.delete"
VALIDATE = f"{USER_FILE}:User.validate"
ADMIN = f"{ADMIN_FILE}:Admin"
BAN = f"{ADMIN_FILE}:Admin.ban"
ISERVICE = f"{ISERVICE_FILE}:IService"
USER_SERVICE = f"{USER_SERVICE_FILE}:UserService"
LOGIN = f"{USER_SERVICE_FILE}:UserService.login"
AUDIT = f"{AUDIT_FILE}:AuditService"
WRITE = f"{LOG_FILE}:Log.write"

FILE_DEPENDENCIES = [
    (USER_SERVICE_FILE, USER_FILE),
    (USER_SERVICE_FILE, ISERVICE_FILE),
    (ADMIN_FILE, USER_FILE),
    (AUDIT_FILE, ISERVICE_FILE),
    (USER_FILE, LOG_FILE),
]

RELATIONSHIPS: list[tuple[RelationshipType, str, str]] = [
    *[(RelationshipType.IMPORTS, s, t) for s, t in FILE_DEPENDENCIES],
    *[(RelationshipType.DEPENDS_ON, s, t) for s, t in FILE_DEPENDENCIES],
    (RelationshipType.DEPENDS_ON, LOG_FILE, CONFIG_FILE),
    (RelationshipType.CALLS, LOGIN, SAVE),
    (RelationshipType.CALLS, LOGIN, VALIDATE),
    (RelationshipType.CALLS, BAN, DELETE),
    (RelationshipType.CALLS, DELETE, SAVE),
    (RelationshipType.CALLS, SAVE, WRITE),
    (RelationshipType.CALLS, VALIDATE, WRITE),
    (RelationshipType.INHERITS, ADMIN, USER),
    (RelationshipType.IMPLEMENTS, USER_SERVICE, ISERVICE),
    (RelationshipType.IMPLEMENTS, AUDIT, ISERVICE),
    (RelationshipType.USES, LOGIN, USER),
]


def full(project_id: str, short_id: str) -> str:
    return f"{project_id}:{short_id}"


def short(entity_id: str) -> str:
    return entity_id.split(":", 1)[1]


def java_report(project_id: str) -> RelationshipReport:
    """The analysis of the project above, as Phase 5 would return it."""
    extraction = ExtractionReport(project_id=project_id)
    for path, members in FILES.items():
        file_id = full(project_id, path)
        entities = [
            Entity(file_id, EntityType.FILE, path.rsplit("/", 1)[1], path, path,
                   Language.JAVA, 1, 1, 40, 1)
        ]  # fmt: skip
        for line, (entity_type, qualified_name) in enumerate(members, start=2):
            parent = qualified_name.rsplit(".", 1)[0] if "." in qualified_name else None
            entities.append(
                Entity(
                    id=full(project_id, f"{path}:{qualified_name}"),
                    type=entity_type,
                    name=qualified_name.rsplit(".", 1)[-1],
                    qualified_name=qualified_name,
                    file_path=path,
                    language=Language.JAVA,
                    start_line=line * 5,
                    start_column=5,
                    end_line=line * 5 + 3,
                    end_column=6,
                    parent_id=full(project_id, f"{path}:{parent}") if parent else file_id,
                )
            )
        extraction.files.append(FileEntities(path, Language.JAVA, False, entities))
    relationships = [
        Relationship(
            id=relationship_id(type_, full(project_id, source), full(project_id, target)),
            type=type_,
            source_id=full(project_id, source),
            target_id=full(project_id, target),
            file_path=source.split(":")[0],
            line=index + 1,
            column=9,
        )
        for index, (type_, source, target) in enumerate(RELATIONSHIPS)
    ]
    return RelationshipReport(project_id, extraction, relationships)


# Questions asked to the project, for comparing two databases (fake and real Neo4j).
Question = Callable[[GraphRetrievalService, str], Any]

QUESTIONS: dict[str, Question] = {
    "find by name": lambda s, p: s.find_entities(p, "run"),
    "find partial": lambda s, p: s.find_entities(p, "User", partial=True),
    "find by type": lambda s, p: s.find_entities(p, "User", partial=True, entity_types=["class"]),
    "entity": lambda s, p: s.get_entity(p, full(p, SAVE)),
    "context": lambda s, p: s.get_entity_context(p, full(p, SAVE)),
    "methods": lambda s, p: s.get_contained_entities(p, full(p, USER)),
    "nested": lambda s, p: s.get_contained_entities(p, full(p, USER_FILE), max_depth=2),
    "callers": lambda s, p: s.get_callers(p, full(p, SAVE)),
    "callees": lambda s, p: s.get_callees(p, full(p, LOGIN)),
    "imports": lambda s, p: s.get_imports(p, full(p, USER_SERVICE_FILE)),
    "importers": lambda s, p: s.get_importers(p, full(p, USER_FILE)),
    "dependencies": lambda s, p: s.get_dependencies(p, full(p, USER_SERVICE_FILE)),
    "transitive": lambda s, p: s.get_transitive_dependencies(p, full(p, USER_SERVICE_FILE)),
    "depth 2": lambda s, p: s.get_transitive_dependencies(
        p, full(p, USER_SERVICE_FILE), max_depth=2
    ),
    "dependents": lambda s, p: s.get_dependents(p, full(p, CONFIG_FILE), max_depth=5),
    "parents": lambda s, p: s.get_parents(p, full(p, ADMIN)),
    "subclasses": lambda s, p: s.get_subclasses(p, full(p, USER)),
    "interfaces": lambda s, p: s.get_implemented_interfaces(p, full(p, USER_SERVICE)),
    "implementations": lambda s, p: s.get_implementations(p, full(p, ISERVICE)),
    "path": lambda s, p: s.find_paths(p, full(p, BAN), full(p, WRITE)),
    "paths": lambda s, p: s.find_paths(p, full(p, LOGIN), full(p, WRITE)),
    "undirected": lambda s, p: s.find_paths(
        p, full(p, SAVE), full(p, VALIDATE), directed=False, relationship_types=["CALLS"]
    ),
    "no path": lambda s, p: s.find_paths(p, full(p, WRITE), full(p, LOGIN)),
    "project graph": lambda s, p: s.get_project_graph(p),
    "project graph, truncated": lambda s, p: s.get_project_graph(p, limit=3),
}
