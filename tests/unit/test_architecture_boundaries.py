"""Architecture boundary enforcement (CLAUDE.md §4, §19).

CLAUDE.md's dependency rules are documentation the moment they're not also
a test — this file makes the ones without their own dedicated invariant
test mechanically checked, the same way `tests/unit/test_invariants.py::
test_ai_cannot_reach_broker_imports` already does for "AI cannot reach the
broker" (INV-6). This file does not duplicate that one; it covers the
three other CLAUDE.md §4 boundaries.

Each check walks a directory's `.py` files with `ast` and asserts no
`import`/`from ... import` statement names a forbidden module — the same
mechanism, applied to a different pair of layers.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _imported_module_names(py_file: Path) -> list[str]:
    """Every dotted path an import statement could plausibly be checked
    against — both `import a.b.c` and `from a.b import c` forms.

    `from services.risk import master_controller` sets `ast.ImportFrom.
    module` to `"services.risk"` only; the forbidden name `master_controller`
    lives in `node.names`, not in `module`. Returning just `node.module`
    (as the pre-existing `test_ai_cannot_reach_broker_imports` does) misses
    that import style whenever the forbidden word is the imported symbol
    rather than part of the module path — reproduced and fixed here by
    also yielding `f"{module}.{alias.name}"` for each imported name."""
    tree = ast.parse(py_file.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{a.name}" for a in node.names)
    return names


def _assert_no_import_matching(directory: Path, forbidden_substring: str,
                                rule: str) -> None:
    for py in directory.rglob("*.py"):
        for name in _imported_module_names(py):
            assert forbidden_substring not in name.lower(), (
                f"{py.relative_to(REPO)} imports {name!r}, containing "
                f"{forbidden_substring!r} — violates {rule}")


# CLAUDE.md §4: "apps/api（UI/API層）→ packages/broker_adapters 直接アクセス"
# apps/api is read-only by design (docs/SAFETY_AUDIT.md confirms zero write
# routes); it must also never be ABLE to reach a broker directly, so that
# property survives someone adding a route later without re-deriving it.
def test_api_layer_cannot_reach_broker_adapters():
    _assert_no_import_matching(
        REPO / "apps" / "api", "broker_adapters",
        "CLAUDE.md §4: apps/api → packages/broker_adapters 直接アクセス禁止")


# CLAUDE.md §4: "packages/broker_adapters → services/decision 等の上位ロジックへの依存"
# A low-level adapter package must never import an upper-layer service —
# that would invert the dependency direction CLAUDE.md §4/§5 requires.
def test_broker_adapters_cannot_depend_on_services():
    for py in (REPO / "packages" / "broker_adapters").rglob("*.py"):
        for name in _imported_module_names(py):
            assert not name.startswith("services"), (
                f"{py.relative_to(REPO)} imports {name!r} — "
                f"packages/broker_adapters must not depend on services/* "
                f"(CLAUDE.md §4)")


# CLAUDE.md §4: "services/pdca / services/alpha_factory（Learning）→
# packages/common/risk_config.py や services/risk/master_controller.py の
# 状態を直接書き換え（現状ゼロ件、維持すること）"
# Learning/PDCA may read outcomes, never touch live risk state — matches
# services/alpha_factory/factory.py's existing "Judge recommends, never
# promotes" design; this test keeps that property from eroding.
def test_learning_layers_cannot_import_risk_state():
    for layer in ("pdca", "alpha_factory"):
        for py in (REPO / "services" / layer).rglob("*.py"):
            for name in _imported_module_names(py):
                lowered = name.lower()
                assert "risk_config" not in lowered and "master_controller" not in lowered, (
                    f"{py.relative_to(REPO)} imports {name!r} — "
                    f"services/{layer} must not touch live Risk state "
                    f"(CLAUDE.md §4)")
