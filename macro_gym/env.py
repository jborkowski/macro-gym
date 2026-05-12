"""Backward-compat alias: ``import macro_gym.env`` keeps working.

v0.2 housed the Gymnasium adapter + kata loaders here. v0.3 moved them
to :mod:`macro_gym.compat` and made :class:`~macro_gym.grader.MacroGrader`
the primary surface. External callers (notably `cl-macro-llm`'s
`grpo_train.py`) still import ``macro_gym.env`` for ``KATAS_DIR`` and
the legacy ``MacroEnv``; this module re-exports those so the old
import path stays stable.

No deprecation warning here — the warning lives at the
``from macro_gym import MacroEnv`` top-level path
(:func:`macro_gym.__getattr__`). The submodule-import shape used by
training scripts is quiet by design.
"""

from __future__ import annotations

from .compat import (
    KATAS_DIR,
    MacroEnv,
    _build_observation,
    list_katas,
    load_kata,
)


def make_env(kata_id: str | None = None, **kwargs):
    """Construct a :class:`MacroEnv` — the v0.2 factory function."""
    return MacroEnv(kata_id=kata_id, **kwargs)


__all__ = [
    "KATAS_DIR",
    "MacroEnv",
    "list_katas",
    "load_kata",
    "make_env",
    "_build_observation",
]
