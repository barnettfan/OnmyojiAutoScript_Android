"""Small, dependency-free scheduler used by the Server control plane."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class ScheduleRule(str, Enum):
    """Supported control-plane scheduling strategies."""

    FILTER = "Filter"
    FIFO = "FIFO"
    PRIORITY = "Priority"


@dataclass(frozen=True)
class ScheduledLike:
    """The minimal fields the standalone scheduling function needs."""

    command: str
    priority: int
    next_run: object


def schedule_tasks(rule: ScheduleRule, pending: Sequence[ScheduledLike]) -> list[ScheduledLike]:
    """Match OAS FIFO/Priority behavior without importing the OAS runtime.

    Filter's developer-maintained ordering is not available to the standalone
    Server, so it uses stable priority/FIFO ordering as a safe fallback.
    """
    items = list(pending)
    if rule == ScheduleRule.FIFO:
        ordered = sorted(items, key=lambda item: (item.next_run, item.command))
    else:
        ordered = sorted(items, key=lambda item: (item.priority, item.next_run, item.command))
    restart = next((item for item in ordered if item.command == "Restart"), None)
    if restart is not None:
        ordered.remove(restart)
        ordered.insert(0, restart)
    return ordered
