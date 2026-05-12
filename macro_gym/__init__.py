"""Macro Gym — verifier-first grader for Common Lisp macro generation.

Primary surface: :class:`MacroGrader` (see :mod:`macro_gym.grader`).

Legacy compatibility: ``from macro_gym import MacroEnv`` continues to work
through a ``DeprecationWarning``; the implementation lives in
:mod:`macro_gym.compat`. New code should use :class:`MacroGrader` directly.
"""

from __future__ import annotations

import warnings

from .grader import (
    ErrorDetail,
    MacroGrader,
    Result,
    get_grader,
    shutdown_grader,
)
from .pool import PoolBusyError, PoolUnhealthyError, SBCLPool
from .compat import list_katas


def __getattr__(name: str):
    """Backward compat: top-level ``MacroEnv`` import emits a DeprecationWarning."""
    if name == "MacroEnv":
        warnings.warn(
            "from macro_gym import MacroEnv is deprecated; "
            "use macro_gym.compat.MacroEnv (or migrate to MacroGrader).",
            DeprecationWarning,
            stacklevel=2,
        )
        from .compat import MacroEnv  # noqa: WPS433 - lazy by design

        return MacroEnv
    raise AttributeError(name)


__all__ = [
    "MacroGrader",
    "Result",
    "ErrorDetail",
    "SBCLPool",
    "PoolBusyError",
    "PoolUnhealthyError",
    "get_grader",
    "shutdown_grader",
    "list_katas",
    "MacroEnv",  # via __getattr__, DeprecationWarning on access
]
