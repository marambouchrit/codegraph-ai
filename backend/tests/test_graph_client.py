"""Neo4jClient: configuration, sessions, and translation of Neo4j errors."""

from typing import Any

import pytest
from neo4j import exceptions as neo4j_exceptions
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import (
    GraphDatabaseConfigError,
    GraphDatabaseError,
    GraphDatabaseUnavailableError,
)
from app.graph.client import Neo4jClient, safe_uri
from tests.graph_fakes import FakeDriver, FakeNeo4j, fake_driver_factory

PASSWORD = "s3cr3t-password"


def make_client(database: FakeNeo4j | None = None, **kwargs: Any) -> Neo4jClient:
    factory = kwargs.pop("driver_factory", fake_driver_factory(database or FakeNeo4j()))
    return Neo4jClient(
        kwargs.pop("uri", "bolt://localhost:7687"), "neo4j", PASSWORD,
        kwargs.pop("database_name", "codegraph"), driver_factory=factory,
    )  # fmt: skip


def failing_client(error: Exception) -> Neo4jClient:
    database = FakeNeo4j()

    def fail(_query: str, _parameters: dict[str, Any]) -> None:
        raise error

    database.before_query = fail
    return make_client(database)


def hydrated(code: str, message: str = "error") -> Exception:
    """A Neo4j server error, built the way the driver builds them from a server response."""
    return neo4j_exceptions.Neo4jError._hydrate_neo4j(code=code, message=message)


# ----- Configuration and lifecycle -----


def test_settings_have_neo4j_defaults_and_hide_the_password() -> None:
    settings = Settings(neo4j_password=SecretStr(PASSWORD))

    assert settings.neo4j_uri == "bolt://localhost:7687"
    assert (settings.neo4j_username, settings.neo4j_database) == ("neo4j", "neo4j")
    assert PASSWORD not in repr(settings)
    assert PASSWORD not in str(settings.model_dump())


def test_from_settings_passes_the_credentials_to_the_driver_only(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    def factory(uri: str, auth: tuple[str, str]) -> FakeDriver:
        created.update(uri=uri, auth=auth)
        return FakeDriver(FakeNeo4j())

    settings = Settings(neo4j_uri="neo4j://graph:7687", neo4j_username="app",
                        neo4j_password=SecretStr(PASSWORD), neo4j_database="code")  # fmt: skip
    client = Neo4jClient.from_settings(settings)
    client._driver_factory = factory

    client.verify_connectivity()

    assert created == {"uri": "neo4j://graph:7687", "auth": ("app", PASSWORD)}
    assert client.driver.sessions == ["code"]  # every session uses NEO4J_DATABASE
    assert PASSWORD not in repr(client)


def test_driver_is_created_lazily_and_closed() -> None:
    database = FakeNeo4j()
    with make_client(database) as client:
        assert client._driver is None  # creating the client does not connect
        client.verify_connectivity()
        driver = client.driver

    assert driver.closed
    assert client._driver is None


def test_write_and_read_run_in_transactions() -> None:
    database = FakeNeo4j()
    client = make_client(database)

    assert client.write(lambda tx: tx.run("RETURN 1 AS ok").data()) == [{"ok": 1}]
    assert client.read(lambda tx: tx.run("RETURN 1 AS ok").data()) == [{"ok": 1}]
    assert database.transactions == 2


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("bolt://localhost:7687", "bolt://localhost:7687"),
        ("neo4j://user:pass@graph.example.com:7687", "neo4j://graph.example.com:7687"),
        ("neo4j+s://abc.databases.neo4j.io", "neo4j+s://abc.databases.neo4j.io"),
        ("localhost", "<invalid URI>"),
        ("bolt://host:notaport", "<invalid URI>"),
    ],
)
def test_safe_uri_never_shows_credentials(uri: str, expected: str) -> None:
    assert safe_uri(uri) == expected


# ----- Errors -----


@pytest.mark.parametrize(
    ("error", "expected_type", "message"),
    [
        (neo4j_exceptions.ServiceUnavailable("down"), GraphDatabaseUnavailableError, "not reachable"),
        (neo4j_exceptions.SessionExpired("gone"), GraphDatabaseUnavailableError, "not reachable"),
        (hydrated("Neo.TransientError.General.DatabaseUnavailable"), GraphDatabaseUnavailableError,
         "not reachable"),
        (hydrated("Neo.ClientError.Security.Unauthorized"), GraphDatabaseConfigError, "credentials"),
        (hydrated("Neo.ClientError.Database.DatabaseNotFound"), GraphDatabaseConfigError,
         "database 'codegraph' does not exist"),
        (hydrated("Neo.ClientError.Schema.ConstraintValidationFailed"), GraphDatabaseError,
         "constraint"),
        (hydrated("Neo.ClientError.Statement.SyntaxError"), GraphDatabaseError, "transaction failed"),
        (neo4j_exceptions.ResultConsumedError(None, "consumed"), GraphDatabaseError,
         "transaction failed"),
    ],
)  # fmt: skip
def test_neo4j_errors_become_application_errors(
    error: Exception, expected_type: type[GraphDatabaseError], message: str
) -> None:
    client = failing_client(error)

    with pytest.raises(expected_type, match=message) as raised:
        client.write(lambda tx: tx.run("RETURN 1 AS ok").data())

    assert type(raised.value) is expected_type
    assert PASSWORD not in raised.value.message


def test_unreachable_server_gives_a_clear_error() -> None:
    # A real driver pointing at a closed port: nothing listens on port 1.
    client = Neo4jClient("bolt://127.0.0.1:1", "neo4j", PASSWORD, "neo4j")

    with pytest.raises(GraphDatabaseUnavailableError) as raised:
        client.verify_connectivity()

    assert raised.value.status_code == 503
    assert "bolt://127.0.0.1:1" in raised.value.message
    assert "docker compose up -d neo4j" in raised.value.message
    client.close()


def test_invalid_uri_is_a_configuration_error() -> None:
    client = Neo4jClient("http://localhost:7474", "neo4j", PASSWORD, "neo4j")

    with pytest.raises(GraphDatabaseConfigError, match="NEO4J_URI"):
        client.verify_connectivity()
