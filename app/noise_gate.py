"""The noise gate (SPEC2 5.4, plan L4): who may write MEMORY.md and skills.

The 2-hour runs only write daily notes. Long-term memory (MEMORY.md) and skills change in two
places only:
- the weekly consolidation job (`open_gate("consolidation")`), and
- the handler of an approval item the user clicked (`open_gate("approval")`).
Any other caller gets `NoiseGateError` from `require`, so a bug or an injected instruction in a
daily run cannot change what the assistant keeps for good. The gate is a context variable: it is
closed everywhere unless a `with open_gate(...)` block is active on the current thread.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

ALLOWED = ("consolidation", "approval")
_OPEN: ContextVar[str | None] = ContextVar("noise_gate", default=None)


class NoiseGateError(PermissionError):
    """A write to long-term memory or skills from a place that may not do it."""


@contextmanager
def open_gate(who: str) -> Iterator[None]:
    if who not in ALLOWED:
        raise ValueError(f"Unknown gate holder: {who!r}")
    token = _OPEN.set(who)
    try:
        yield
    finally:
        _OPEN.reset(token)


def holder() -> str | None:
    return _OPEN.get()


def require(what: str) -> None:
    """Raise NoiseGateError unless the gate is open."""
    if _OPEN.get() is None:
        raise NoiseGateError(
            f"{what} can only be changed by the weekly consolidation or by an approved item."
        )
