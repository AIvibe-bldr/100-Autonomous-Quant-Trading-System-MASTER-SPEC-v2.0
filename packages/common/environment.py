"""Environment separation (MASTER SPEC §73).

Research / Paper / Live are fully isolated: credentials, DB namespace and
execution endpoints are selected by this enum, and mixing environments in one
component graph is rejected at construction time.
"""
from __future__ import annotations

import enum


class Environment(str, enum.Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def is_live(self) -> bool:
        return self is Environment.LIVE

    def namespace(self, table: str) -> str:
        """DB namespace prefix per environment (§73)."""
        return f"{self.value.lower()}_{table}"


class EnvironmentMismatchError(RuntimeError):
    """Raised when components from different environments are wired together."""


def require_same_environment(*envs: Environment) -> Environment:
    unique = set(envs)
    if len(unique) != 1:
        raise EnvironmentMismatchError(
            f"components from different environments wired together: {sorted(e.value for e in unique)}"
        )
    return envs[0]
