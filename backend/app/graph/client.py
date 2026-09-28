"""Connection to Neo4j: driver, sessions, transactions and error translation.

`Neo4jClient` is the only module that talks to the Neo4j driver directly. It has
no Cypher and no business logic: the repository gives it functions to run inside
a transaction.

    with Neo4jClient.from_settings(settings) as client:
        client.verify_connectivity()
        client.write(lambda tx: tx.run("RETURN 1").data())

Driver exceptions never leave this module: `neo4j_errors()` turns them into the
application's `GraphDatabaseError`s, whose messages never contain the password.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Self, TypeVar
from urllib.parse import urlsplit

import neo4j
from neo4j import exceptions as neo4j_exceptions

from app.core.config import Settings
from app.core.errors import (
    GraphDatabaseConfigError,
    GraphDatabaseError,
    GraphDatabaseUnavailableError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
Transaction = neo4j.ManagedTransaction
DriverFactory = Callable[..., Any]  # neo4j.GraphDatabase.driver, or a fake in tests

DATABASE_NOT_FOUND = "Neo.ClientError.Database.DatabaseNotFound"


class Neo4jClient:
    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str,
        driver_factory: DriverFactory = neo4j.GraphDatabase.driver,
    ) -> None:
        self.uri = uri
        self.database = database
        self._username = username
        self._password = password  # only given to the driver; never logged
        self._driver_factory = driver_factory
        self._driver: Any = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "Neo4jClient":
        return cls(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password.get_secret_value(),
            database=settings.neo4j_database,
        )

    def __repr__(self) -> str:  # never show the password, even when debugging
        return f"Neo4jClient(uri={safe_uri(self.uri)!r}, database={self.database!r})"

    # ----- Lifecycle -----

    @property
    def driver(self) -> Any:
        """The Neo4j driver, created on first use (creating it does not connect yet)."""
        if self._driver is None:
            with self.neo4j_errors():
                self._driver = self._driver_factory(
                    self.uri, auth=(self._username, self._password)
                )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # ----- Running work -----

    def verify_connectivity(self) -> None:
        """Check that Neo4j is reachable, the credentials work and the database exists.

        The driver's own check fails fast; a transaction alone would first be retried for
        up to 30 seconds when the server is down.
        """
        with self.neo4j_errors():
            self.driver.verify_connectivity()
        self.read(lambda tx: tx.run("RETURN 1 AS ok").data())  # the database exists
        logger.info("Connected to Neo4j at %s (database %s)", safe_uri(self.uri), self.database)

    def write(self, work: Callable[[Transaction], T]) -> T:
        """Run `work` in a write transaction (committed at the end, rolled back on error).

        Managed transactions are retried automatically by the driver when Neo4j reports a
        temporary problem (a deadlock, a leader switch...), so `work` must be repeatable:
        MERGE queries are.
        """
        with self.neo4j_errors(), self.driver.session(database=self.database) as session:
            return session.execute_write(work)

    def read(self, work: Callable[[Transaction], T]) -> T:
        with self.neo4j_errors(), self.driver.session(database=self.database) as session:
            return session.execute_read(work)

    @contextmanager
    def neo4j_errors(self) -> Iterator[None]:
        """Translate Neo4j driver exceptions into application errors."""
        location = safe_uri(self.uri)
        try:
            yield
        except (neo4j_exceptions.AuthError, neo4j_exceptions.AuthConfigurationError) as error:
            raise GraphDatabaseConfigError(
                "Neo4j rejected the credentials: check NEO4J_USERNAME and NEO4J_PASSWORD."
            ) from error
        except neo4j_exceptions.ConfigurationError as error:
            raise GraphDatabaseConfigError(
                f"Invalid Neo4j configuration: check NEO4J_URI ({location})."
            ) from error
        except (
            neo4j_exceptions.ServiceUnavailable,
            neo4j_exceptions.SessionExpired,
            neo4j_exceptions.TransientError,  # still failing after the driver's retries
        ) as error:
            raise GraphDatabaseUnavailableError(
                f"Neo4j is not reachable at {location}. Is it running? "
                "Start it with: docker compose up -d neo4j"
            ) from error
        except neo4j_exceptions.ConstraintError as error:
            raise GraphDatabaseError(
                "A Neo4j constraint was violated: two nodes would share the same ID."
            ) from error
        except neo4j_exceptions.Neo4jError as error:
            if error.code == DATABASE_NOT_FOUND:
                raise GraphDatabaseConfigError(
                    f"The Neo4j database '{self.database}' does not exist: check NEO4J_DATABASE."
                ) from error
            logger.error("Neo4j error %s: %s", error.code, error.message)
            raise GraphDatabaseError("A Neo4j transaction failed.") from error
        except neo4j_exceptions.DriverError as error:
            logger.error("Neo4j driver error: %s", type(error).__name__)
            raise GraphDatabaseError("A Neo4j transaction failed.") from error


def safe_uri(uri: str) -> str:
    """The URI without any "user:password@" part, safe to log or show."""
    try:
        parts = urlsplit(uri)
        host, port = parts.hostname or "", parts.port
    except ValueError:  # e.g. a port that is not a number
        return "<invalid URI>"
    if not parts.scheme:
        return "<invalid URI>"
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"
