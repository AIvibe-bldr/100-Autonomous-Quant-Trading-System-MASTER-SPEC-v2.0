"""Windows has no system tz database: without the `tzdata` package the trading calendar cannot even be imported
(found by the Windows CI job; the operator's PC runs Windows). This guards the declaration on any OS."""
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_tzdata_is_a_dependency_on_windows_only():
    deps = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    tz = [d for d in deps if d.startswith("tzdata")]
    assert len(tz) == 1 and "win32" in tz[0], deps


def _text_io_without_encoding(root: Path) -> list[str]:
    """Text-mode file reads/writes in production code that do not name an encoding. On Windows the default is
    cp1252, which cannot read the UTF-8 files we write (Japanese text) and would stop the dashboard, the kill
    switch, the incident log or the intent inbox."""
    import ast
    found = []
    for top in ("packages", "services", "apps", "scripts"):
        for py in sorted((root / top).rglob("*.py")):
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or any(k.arg == "encoding" for k in node.keywords):
                    continue
                f, first = node.func, (node.args[0] if node.args else None)
                is_mode = isinstance(first, ast.Constant) and isinstance(first.value, str)
                if isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
                    bad = True
                elif isinstance(f, ast.Attribute) and f.attr == "open":      # Path.open("a"), not obj.open(id, ...)
                    bad = is_mode and "b" not in first.value
                elif isinstance(f, ast.Name) and f.id == "open":
                    mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else "r"
                    bad = "b" not in str(mode)
                else:
                    bad = False
                if bad:
                    found.append(f"{py.relative_to(root)}:{node.lineno}")
    return found


def test_production_text_files_always_name_their_encoding():
    assert _text_io_without_encoding(PYPROJECT.parent) == []
