"""Validate GitHub URLs and clone public repositories.

Security notes:
  * The clone URL is rebuilt from the validated owner/name, so user input is never
    passed to git directly (no option injection such as "--upload-pack=...").
  * Cloning only downloads files. Git hooks are never copied by a clone, submodules
    are not fetched, and symlinks are checked out as plain text files.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import InvalidGitHubUrlError, RepositoryCloneError

_GITHUB_URL_PATTERN = re.compile(
    r"^https://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<name>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        return f"{self.url}.git"


def parse_github_url(url: str) -> GitHubRepository:
    """Validate a public GitHub repository URL such as https://github.com/owner/repo."""
    match = _GITHUB_URL_PATTERN.fullmatch(url.strip())
    if match is None or match["name"] in {".", ".."}:
        raise InvalidGitHubUrlError(
            f"'{url}' is not a valid GitHub repository URL. "
            "Expected a URL like https://github.com/owner/repository"
        )
    return GitHubRepository(owner=match["owner"], name=match["name"])


def clone_repository(clone_url: str, destination: Path, timeout_seconds: int) -> None:
    """Shallow-clone `clone_url` into `destination`, which must not exist yet."""
    command = [
        "git",
        "-c", "core.symlinks=false",  # check out symlinks as plain files
        "-c", "core.longpaths=true",  # allow long paths on Windows
        "-c", "credential.helper=",  # never use or ask for stored credentials
        "clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet",
        "--", clone_url, str(destination),
    ]  # fmt: skip
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # fail instead of prompting for a password
        "GCM_INTERACTIVE": "never",  # same for Git Credential Manager (Windows)
        "GIT_LFS_SKIP_SMUDGE": "1",  # do not download large LFS files
    }

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except FileNotFoundError as error:
        raise RepositoryCloneError("Git is not installed or not available on PATH.") from error
    except subprocess.TimeoutExpired as error:
        raise RepositoryCloneError(
            f"Cloning timed out after {timeout_seconds} seconds. The repository may be too large."
        ) from error

    if completed.returncode != 0:
        git_message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else ""
        raise RepositoryCloneError(
            f"Could not clone {clone_url}. Check that the repository exists and is public. "
            f"Git said: {git_message or 'unknown error'}"
        )
