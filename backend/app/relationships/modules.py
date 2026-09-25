"""Find the project file an import refers to.

Each language finds modules its own way:

    Python       from ..models.user import User   relative: ../models/user.py
                 from models.user import User     absolute: any "models/user.py" whose
                                                  folder is an ancestor of the importing
                                                  file (project root, src/, backend/...)
    JavaScript/  import ... from "./models/User"  ./models/User.ts, .tsx, .js...,
    TypeScript                                    or ./models/User/index.ts...
                 import ... from "react"          a package: always outside the project
    Java         import com.example.User;         the files whose `package` is com.example

Lookups only use the list of scanned file paths: nothing is read from disk.
Every result is either a project path or an `UnresolvedReason`.
"""

import posixpath
from collections import defaultdict
from collections.abc import Iterable

from app.ingestion.languages import Language
from app.relationships.models import UnresolvedReason

# A package (folder with __init__.py) takes precedence over a module with the same name,
# as in Python itself; .pyi stub files are only used when there is no .py file.
PYTHON_MODULE_FILES = ("{}/__init__.py", "{}.py", "{}/__init__.pyi", "{}.pyi")

# The order TypeScript and bundlers try: TypeScript sources first, then JavaScript.
SCRIPT_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
JAVASCRIPT_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")
# Files that bundlers let JavaScript import but that are not source code.
ASSET_EXTENSIONS = frozenset(
    {".css", ".scss", ".sass", ".less", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
)

SCRIPT_LANGUAGES = frozenset({Language.JAVASCRIPT, Language.TYPESCRIPT})

ModuleResult = str | UnresolvedReason


class ModuleIndex:
    def __init__(
        self, files: Iterable[tuple[str, Language]], java_packages: dict[str, str | None]
    ) -> None:
        self._python: set[str] = set()
        self._scripts: set[str] = set()
        # "models/user.py" -> every Python file whose path ends with it, for absolute imports.
        self._python_by_suffix: dict[str, list[str]] = defaultdict(list)
        self._java_packages: dict[str, list[str]] = defaultdict(list)
        for path, language in files:
            if language == Language.PYTHON:
                self._python.add(path)
                parts = path.split("/")
                for start in range(len(parts)):
                    self._python_by_suffix["/".join(parts[start:])].append(path)
            elif language in SCRIPT_LANGUAGES:
                self._scripts.add(path)
            elif language == Language.JAVA:
                self._java_packages[java_packages.get(path) or ""].append(path)

    # ----- Python -----

    def python_module(self, importer: str, module: str) -> ModuleResult:
        """The file of `module` ("a.b", ".b", "..a.b" or ".") imported by `importer`."""
        dots = len(module) - len(module.lstrip("."))
        names = [name for name in module[dots:].split(".") if name]
        if dots:
            return self._relative_python_module(importer, dots, names)

        relative_path = "/".join(names)
        # (source root, path): e.g. ("src/", "src/models/user.py") for "models.user".
        # A source root must be an ancestor folder of the importing file, otherwise
        # `import os` would match any project file named os.py.
        roots: dict[str, str] = {}
        for pattern in PYTHON_MODULE_FILES:
            candidate = pattern.format(relative_path)
            for path in self._python_by_suffix.get(candidate, []):
                root = path[: len(path) - len(candidate)]
                if importer.startswith(root):
                    roots.setdefault(root, path)  # first pattern wins within one root
        if not roots:
            return UnresolvedReason.EXTERNAL  # a library, the standard library...
        if len(roots) > 1:
            return UnresolvedReason.AMBIGUOUS  # e.g. both models/user.py and src/models/user.py
        return next(iter(roots.values()))

    def _relative_python_module(self, importer: str, dots: int, names: list[str]) -> ModuleResult:
        # One dot is the importer's own package; each extra dot goes one folder up.
        folder = posixpath.dirname(importer).split("/") if "/" in importer else []
        if dots - 1 > len(folder):
            return UnresolvedReason.NOT_FOUND  # goes above the project root
        base = folder[: len(folder) - (dots - 1)]
        relative_path = "/".join([*base, *names])
        if not names:  # `from . import x`: the package itself
            candidates = [posixpath.join(*base, "__init__.py") if base else "__init__.py"]
        else:
            candidates = [pattern.format(relative_path) for pattern in PYTHON_MODULE_FILES]
        return next((c for c in candidates if c in self._python), UnresolvedReason.NOT_FOUND)

    # ----- JavaScript / TypeScript -----

    def script_module(self, importer: str, specifier: str) -> ModuleResult:
        """The file of `specifier` ("./models/User", "../utils", "react"...)."""
        if not (specifier.startswith(("./", "../")) or specifier in (".", "..")):
            return UnresolvedReason.EXTERNAL  # an npm package (or a path alias, unsupported)
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
        if base == ".." or base.startswith("../"):
            return UnresolvedReason.NOT_FOUND  # goes above the project root
        base = "" if base == "." else base

        candidates = [base] if base else []
        stem, extension = posixpath.splitext(base)
        if extension.lower() in ASSET_EXTENSIONS:
            return UnresolvedReason.EXTERNAL  # a style sheet, data or image: not code
        if extension in JAVASCRIPT_EXTENSIONS:
            # TypeScript ESM code writes "./user.js" to import user.ts.
            candidates += [stem + ext for ext in SCRIPT_EXTENSIONS]
        candidates += [base + ext for ext in SCRIPT_EXTENSIONS]
        index = posixpath.join(base, "index") if base else "index"
        candidates += [index + ext for ext in SCRIPT_EXTENSIONS]
        return next((c for c in candidates if c in self._scripts), UnresolvedReason.NOT_FOUND)

    # ----- Java -----

    def java_package(self, package: str) -> list[str]:
        """The files declaring `package` ("" is the default package)."""
        return self._java_packages.get(package, [])

    def is_java_package(self, package: str) -> bool:
        return package in self._java_packages
