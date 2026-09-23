import io
import stat
import zipfile
from pathlib import Path

import pytest

from app.core.errors import ContentTooLargeError, InvalidArchiveError, UnsafeArchiveError
from app.ingestion import zip_handler
from app.ingestion.zip_handler import extract_zip_safely, find_project_root, safe_member_path
from tests.conftest import MakeZip

LARGE_LIMIT = 10 * 1024 * 1024


def extract(data: bytes, destination: Path, max_total_bytes: int = LARGE_LIMIT, max_files: int = 100) -> None:
    extract_zip_safely(
        io.BytesIO(data), destination, max_total_bytes=max_total_bytes, max_files=max_files
    )


def test_extracts_files_and_folders(tmp_path: Path, make_zip: MakeZip) -> None:
    data = make_zip({"app.py": "print('hi')", "pkg/module.py": "x = 1", "pkg/sub/": ""})

    extract(data, tmp_path / "out")

    assert (tmp_path / "out/app.py").read_text() == "print('hi')"
    assert (tmp_path / "out/pkg/module.py").read_text() == "x = 1"
    assert (tmp_path / "out/pkg/sub").is_dir()


@pytest.mark.parametrize(
    "malicious_name",
    [
        "../evil.py",
        "safe/../../evil.py",
        "..\\evil.py",
        "/etc/evil.py",
        "\\evil.py",
        "C:/Windows/evil.py",
        "C:evil.py",
    ],
)
def test_rejects_path_traversal(tmp_path: Path, make_zip: MakeZip, malicious_name: str) -> None:
    data = make_zip({"ok.py": "x = 1", malicious_name: "import os"})
    destination = tmp_path / "sandbox" / "out"

    with pytest.raises(UnsafeArchiveError):
        extract(data, destination)

    # Nothing may be written, not even the safe file listed before the malicious one.
    assert not (destination / "ok.py").exists()
    assert not (tmp_path / "sandbox" / "evil.py").exists()
    assert not (tmp_path / "evil.py").exists()


def test_safe_member_path_accepts_normal_paths(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert safe_member_path(root, "a/b/./c.py") == root / "a" / "b" / "c.py"


def test_rejects_archive_with_too_many_files(tmp_path: Path, make_zip: MakeZip) -> None:
    data = make_zip({f"file{i}.py": "x" for i in range(5)})

    with pytest.raises(ContentTooLargeError):
        extract(data, tmp_path / "out", max_files=4)


def test_rejects_archive_that_expands_too_much(tmp_path: Path, make_zip: MakeZip) -> None:
    # 1 MB of zeros compresses to about 1 KB: a miniature "zip bomb".
    data = make_zip({"bomb.py": "0" * 1024 * 1024})
    assert len(data) < 10_000

    with pytest.raises(ContentTooLargeError):
        extract(data, tmp_path / "out", max_total_bytes=100_000)


def test_enforces_size_limit_while_writing(
    tmp_path: Path, make_zip: MakeZip, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate an archive whose headers lie: the declared-size check is bypassed,
    # so only the byte counting during extraction can stop it.
    monkeypatch.setattr(zip_handler, "_check_declared_limits", lambda *_args: None)
    data = make_zip({"a.py": "x" * 3000, "b.py": "y" * 3000})

    with pytest.raises(ContentTooLargeError):
        extract(data, tmp_path / "out", max_total_bytes=4000)


def test_rejects_invalid_zip(tmp_path: Path) -> None:
    with pytest.raises(InvalidArchiveError):
        extract(b"this is not a zip file", tmp_path / "out")


def test_skips_symlink_entries(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app.py", "x = 1")
        link = zipfile.ZipInfo("link.py")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")

    extract(buffer.getvalue(), tmp_path / "out")

    assert (tmp_path / "out/app.py").exists()
    assert not (tmp_path / "out/link.py").exists()


def test_skips_macos_metadata_folder(tmp_path: Path, make_zip: MakeZip) -> None:
    data = make_zip({"project/app.py": "x = 1", "__MACOSX/project/._app.py": "junk"})

    extract(data, tmp_path / "out")

    assert not (tmp_path / "out/__MACOSX").exists()


def test_find_project_root_unwraps_single_top_level_folder(tmp_path: Path, make_zip: MakeZip) -> None:
    extract(make_zip({"repo-main/app.py": "x = 1"}), tmp_path / "out")

    assert find_project_root(tmp_path / "out") == tmp_path / "out" / "repo-main"


def test_find_project_root_keeps_folder_with_several_entries(tmp_path: Path, make_zip: MakeZip) -> None:
    extract(make_zip({"app.py": "x = 1", "lib/util.py": "y = 2"}), tmp_path / "out")

    assert find_project_root(tmp_path / "out") == tmp_path / "out"
