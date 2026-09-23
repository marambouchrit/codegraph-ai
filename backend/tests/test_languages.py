import pytest

from app.ingestion.languages import Language, detect_language


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("main.py", Language.PYTHON),
        ("stubs/types.pyi", Language.PYTHON),
        ("src/UserService.java", Language.JAVA),
        ("index.js", Language.JAVASCRIPT),
        ("App.jsx", Language.JAVASCRIPT),
        ("server.mjs", Language.JAVASCRIPT),
        ("config.cjs", Language.JAVASCRIPT),
        ("api.ts", Language.TYPESCRIPT),
        ("Component.tsx", Language.TYPESCRIPT),
        ("types.d.ts", Language.TYPESCRIPT),
        ("UPPER.PY", Language.PYTHON),
    ],
)
def test_detects_supported_languages(path: str, expected: Language) -> None:
    assert detect_language(path) == expected


@pytest.mark.parametrize("path", ["README.md", "styles.css", "Makefile", "image.png", "script.py.bak"])
def test_returns_none_for_unsupported_files(path: str) -> None:
    assert detect_language(path) is None
