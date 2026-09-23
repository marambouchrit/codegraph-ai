import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.errors import InvalidGitHubUrlError, RepositoryCloneError
from app.ingestion import github
from app.ingestion.github import clone_repository, parse_github_url

# ----- URL validation -----


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/pallets/flask",
        "https://github.com/pallets/flask/",
        "https://github.com/pallets/flask.git",
        "https://www.github.com/pallets/flask",
        "  https://github.com/pallets/flask  ",
    ],
)
def test_accepts_valid_github_urls(url: str) -> None:
    repository = parse_github_url(url)

    assert repository.owner == "pallets"
    assert repository.name == "flask"
    assert repository.clone_url == "https://github.com/pallets/flask.git"


def test_accepts_repository_names_with_dots_and_dashes() -> None:
    repository = parse_github_url("https://github.com/my-org/my.repo_name-2")

    assert repository.name == "my.repo_name-2"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "http://github.com/pallets/flask",  # only https
        "https://gitlab.com/pallets/flask",
        "https://github.com/pallets",
        "https://github.com/pallets/flask/tree/main",
        "https://github.com.evil.com/pallets/flask",
        "https://github.com/-bad/flask",
        "https://github.com/pallets/..",
        "https://github.com/pallets/flask;rm -rf /",
        "https://github.com/pallets/--upload-pack=touch",
        "git@github.com:pallets/flask.git",
        "file:///etc/passwd",
    ],
)
def test_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(InvalidGitHubUrlError):
        parse_github_url(url)


# ----- Cloning (error handling, no network) -----


def test_clone_reports_missing_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("git")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    with pytest.raises(RepositoryCloneError, match="Git is not installed"):
        clone_repository("https://github.com/a/b.git", tmp_path / "dest", timeout_seconds=5)


def test_clone_reports_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    with pytest.raises(RepositoryCloneError, match="timed out"):
        clone_repository("https://github.com/a/b.git", tmp_path / "dest", timeout_seconds=5)


def test_clone_reports_git_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 128, "", "fatal: repository not found\n")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    with pytest.raises(RepositoryCloneError, match="repository not found"):
        clone_repository("https://github.com/a/b.git", tmp_path / "dest", timeout_seconds=5)


def test_clone_command_is_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    clone_repository("https://github.com/a/b.git", tmp_path / "dest", timeout_seconds=5)

    command = captured["command"]
    assert command[command.index("--") + 1] == "https://github.com/a/b.git"  # URL after "--"
    assert "--depth" in command
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


# ----- Cloning with real git (local repository, no network) -----

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@requires_git
def test_clones_a_local_repository(tmp_path: Path) -> None:
    source = tmp_path / "origin"
    source.mkdir()
    (source / "app.py").write_text("print('hello')")
    git = ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run([*git, "init", "--quiet"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "--quiet", "-m", "init"], check=True)

    destination = tmp_path / "clone"
    clone_repository(source.as_uri(), destination, timeout_seconds=60)

    assert (destination / "app.py").read_text() == "print('hello')"


@requires_git
def test_clone_of_missing_repository_fails(tmp_path: Path) -> None:
    missing = (tmp_path / "does-not-exist").as_uri()

    with pytest.raises(RepositoryCloneError):
        clone_repository(missing, tmp_path / "clone", timeout_seconds=60)


@pytest.mark.network
def test_clones_a_real_public_github_repository(tmp_path: Path) -> None:
    repository = parse_github_url("https://github.com/octocat/Hello-World")

    clone_repository(repository.clone_url, tmp_path / "clone", timeout_seconds=120)

    assert (tmp_path / "clone" / "README").exists()
