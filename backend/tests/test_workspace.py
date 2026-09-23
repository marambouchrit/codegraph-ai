import os
import stat
from pathlib import Path

import pytest

from app.core.errors import ProjectNotFoundError
from app.ingestion.workspace import Workspace, directory_size, new_project_id, remove_tree


@pytest.mark.parametrize("bad_id", ["..", "../../etc", "abc", "A" * 32, "not-a-valid-project-id"])
def test_rejects_invalid_project_ids(tmp_path: Path, bad_id: str) -> None:
    workspace = Workspace(tmp_path)

    with pytest.raises(ProjectNotFoundError):
        workspace.project_dir(bad_id)


def test_creates_and_lists_project_dirs(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    project_id = new_project_id()

    workspace.create_project_dir(project_id)
    (tmp_path / "unrelated-folder").mkdir()

    assert workspace.project_ids() == [project_id]


def test_project_ids_when_workspace_does_not_exist(tmp_path: Path) -> None:
    assert Workspace(tmp_path / "missing").project_ids() == []


def test_delete_project_dir_is_safe_when_missing(tmp_path: Path) -> None:
    Workspace(tmp_path).delete_project_dir(new_project_id())  # must not raise


def test_remove_tree_deletes_read_only_files(tmp_path: Path) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()
    read_only = folder / "locked.txt"
    read_only.write_text("x")
    os.chmod(read_only, stat.S_IREAD)

    remove_tree(folder)

    assert not folder.exists()


def test_directory_size(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_bytes(b"123")

    assert directory_size(tmp_path) == 8
