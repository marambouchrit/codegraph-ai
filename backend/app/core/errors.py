"""Application errors that carry an HTTP status code.

Services raise these errors; `main.py` converts them into JSON error responses.
This keeps the business logic independent of FastAPI.
"""


class AppError(Exception):
    """Base class for expected, user-facing errors."""

    status_code: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProjectNotFoundError(AppError):
    status_code = 404


class InvalidGitHubUrlError(AppError):
    status_code = 400


class RepositoryCloneError(AppError):
    status_code = 400


class InvalidArchiveError(AppError):
    status_code = 400


class UnsafeArchiveError(AppError):
    status_code = 400


class ContentTooLargeError(AppError):
    status_code = 413


class NoSourceFilesError(AppError):
    status_code = 422


class UnsupportedLanguageError(AppError):
    status_code = 422


class SourceFileError(AppError):
    """A source file cannot be parsed safely (missing, too large, binary, symlink...)."""

    status_code = 422


class GraphDatabaseError(AppError):
    """A Neo4j operation failed (a transaction, a query, a constraint...)."""

    status_code = 500


class GraphDatabaseUnavailableError(GraphDatabaseError):
    """Neo4j cannot be reached (not started, wrong host, network problem)."""

    status_code = 503


class GraphDatabaseConfigError(GraphDatabaseError):
    """Neo4j is reachable but the configuration is wrong (credentials, URI, database name)."""

    status_code = 503
