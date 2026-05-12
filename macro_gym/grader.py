"""Public verifier API: :class:`MacroGrader`.

This is the surface trainers and eval pipelines consume. It wraps an
:class:`~macro_gym.pool.SBCLPool` with:

* a stable :class:`Result` TypedDict shape (snake_case keys, no Lisp colons),
* :meth:`MacroGrader.grade` and :meth:`MacroGrader.grade_batch`,
* a TRL/verl-compatible :meth:`MacroGrader.reward_fn` that returns scalar
  rewards in input order, and
* a lazy module-level singleton (:func:`get_grader`, :func:`shutdown_grader`)
  for ergonomic one-liners.

Configuration precedence (documented contract): **constructor args win over
environment variables.** The recognized env vars are:

* ``MACRO_GYM_POOL_SIZE`` — int, default ``6``.
* ``MACRO_GYM_HEAP_MB`` — int, default ``384`` (per worker).
* ``MACRO_GYM_SBCL_PATH`` — explicit binary path; otherwise ``find_sbcl()``
  searches ``$PATH``.

Env vars are read only when the corresponding constructor argument is left at
its default sentinel (``None``); pass any explicit constructor value to
override the environment.
"""

from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple, TypedDict, Union

from .pool import PoolBusyError, PoolUnhealthyError, SBCLPool

__all__ = [
    "MacroGrader",
    "Result",
    "ErrorDetail",
    "get_grader",
    "shutdown_grader",
]


# ---------------------------------------------------------------------------
# Result schema


class ErrorDetail(TypedDict, total=False):
    type: str
    message: str
    lisp_condition: Optional[str]
    stderr_tail: str


class Result(TypedDict, total=False):
    reward: float
    passed: int
    total: int
    results: List[dict]
    error: Optional[ErrorDetail]
    done: bool
    semantic_eq_score: Optional[float]


# ---------------------------------------------------------------------------
# Internal helpers


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _coerce_error_detail(value: Any) -> Optional[ErrorDetail]:
    """Take whatever the Lisp side returned for ``:error`` and normalize it.

    The legacy server emitted a plain string here; the new server emits a
    plist that the pool normalizer has already converted to a dict with
    snake_case keys. Either shape is accepted; both come out as an
    :class:`ErrorDetail` (or ``None``).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        out: ErrorDetail = {
            "type": str(value.get("type", "unknown")),
            "message": str(value.get("message", "")),
            "lisp_condition": value.get("lisp_condition"),
            "stderr_tail": str(value.get("stderr_tail", "")),
        }
        return out
    if isinstance(value, str):
        return ErrorDetail(
            type="unknown",
            message=value,
            lisp_condition=None,
            stderr_tail="",
        )
    return ErrorDetail(
        type="unknown",
        message=repr(value),
        lisp_condition=None,
        stderr_tail="",
    )


def _coerce_result(raw: dict, fallback_error: Optional[ErrorDetail] = None) -> Result:
    """Shape-check a normalized response dict into a :class:`Result`."""
    out: Result = {}
    if "reward" in raw:
        try:
            out["reward"] = float(raw["reward"])
        except (TypeError, ValueError):
            out["reward"] = -0.1
    if "passed" in raw:
        try:
            out["passed"] = int(raw["passed"])
        except (TypeError, ValueError):
            out["passed"] = 0
    if "total" in raw:
        try:
            out["total"] = int(raw["total"])
        except (TypeError, ValueError):
            out["total"] = 0
    if "results" in raw and raw["results"] is not None:
        results_val = raw["results"]
        if isinstance(results_val, list):
            out["results"] = [r if isinstance(r, dict) else {"raw": r} for r in results_val]
        else:
            out["results"] = []
    if "done" in raw:
        out["done"] = bool(raw["done"])
    if "semantic_eq_score" in raw:
        val = raw["semantic_eq_score"]
        out["semantic_eq_score"] = None if val is None else float(val)
    # Error field
    if fallback_error is not None:
        out["error"] = fallback_error
    elif "error" in raw:
        out["error"] = _coerce_error_detail(raw["error"])
    return out


def _error_result(
    error_type: str,
    message: str,
    lisp_condition: Optional[str] = None,
    stderr_tail: str = "",
) -> Result:
    return Result(
        reward=-0.1,
        passed=0,
        total=0,
        results=[],
        done=False,
        semantic_eq_score=None,
        error=ErrorDetail(
            type=error_type,
            message=message,
            lisp_condition=lisp_condition,
            stderr_tail=stderr_tail,
        ),
    )


# ---------------------------------------------------------------------------
# MacroGrader


class MacroGrader:
    """Pure verifier: ``(kata_id, macro_src) -> Result``.

    Construction precedence: explicit kwargs win over ``MACRO_GYM_*`` env
    vars. Pass an existing :class:`SBCLPool` via ``pool=`` to share workers
    across multiple graders (e.g. in a multi-tenant eval rig).
    """

    def __init__(
        self,
        *,
        pool: Optional[SBCLPool] = None,
        pool_size: Optional[int] = None,
        heap_mb: Optional[int] = None,
        recycle_after: int = 200,
        default_timeout: float = 10.0,
        sbcl_path: Optional[str] = None,
        kata_dirs: Optional[List[Path]] = None,
        debug_protocol: bool = False,
        checkout_timeout: float = 30.0,
        rss_threshold_mb: float = 768.0,
    ) -> None:
        # Resolve env-var defaults. Constructor arg wins on collision.
        effective_pool_size = pool_size if pool_size is not None else _env_int(
            "MACRO_GYM_POOL_SIZE", 6
        )
        effective_heap_mb = heap_mb if heap_mb is not None else _env_int(
            "MACRO_GYM_HEAP_MB", 384
        )
        effective_sbcl_path = sbcl_path or os.environ.get("MACRO_GYM_SBCL_PATH") or None

        self.default_timeout = float(default_timeout)
        self.kata_dirs = kata_dirs  # currently advisory; the Lisp server reads ./katas
        self._owns_pool = pool is None
        if pool is not None:
            self._pool: SBCLPool = pool
        else:
            self._pool = SBCLPool(
                size=effective_pool_size,
                heap_mb=effective_heap_mb,
                recycle_after=recycle_after,
                rss_threshold_mb=rss_threshold_mb,
                sbcl_path=effective_sbcl_path,
                checkout_timeout=checkout_timeout,
                debug_protocol=debug_protocol,
            )
        self._closed = False

    # ----- single grade ----------------------------------------------------

    def grade(
        self,
        kata_id: str,
        macro_src: str,
        *,
        timeout: Optional[float] = None,
    ) -> Result:
        """Grade one ``(kata_id, macro_src)`` pair, returning a :class:`Result`.

        Never raises on grader failures: protocol errors, timeouts, dead
        workers, and unknown katas are surfaced as a ``-0.1`` Result with
        ``error`` populated. The only exception that escapes is
        :class:`PoolBusyError` (no worker available within the checkout
        timeout) or :class:`PoolUnhealthyError` (every worker is dead).
        """
        if self._closed:
            raise RuntimeError("MacroGrader is closed")
        t = self.default_timeout if timeout is None else float(timeout)
        try:
            raw = self._pool.grade(kata_id, macro_src, timeout=t)
        except (PoolBusyError, PoolUnhealthyError):
            # Surface infrastructure failures to the caller; they're not a
            # property of this particular macro.
            raise
        except TimeoutError as e:
            return _error_result("timeout", str(e))
        except BrokenPipeError as e:
            return _error_result("worker-died", str(e))
        except Exception as e:  # noqa: BLE001 - protocol / parse / etc
            return _error_result("unknown", f"{type(e).__name__}: {e}")
        return _coerce_result(raw)

    # ----- batch -----------------------------------------------------------

    def grade_batch(
        self,
        items: Sequence[Tuple[str, str]],
        max_workers: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> List[Result]:
        """Grade many items in parallel, preserving input order.

        ``max_workers`` defaults to the pool size, which is the right setting
        when this grader owns its pool. If you injected a shared pool, set
        ``max_workers`` to however many you want to dedicate to this call.
        """
        items = list(items)
        if not items:
            return []
        n_workers = max_workers if max_workers is not None else self._pool.size
        n_workers = max(1, min(n_workers, len(items)))

        results: List[Optional[Result]] = [None] * len(items)

        def _one(idx: int, kata_id: str, macro_src: str) -> Tuple[int, Result]:
            return idx, self.grade(kata_id, macro_src, timeout=timeout)

        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futures = [
                ex.submit(_one, i, k, s) for i, (k, s) in enumerate(items)
            ]
            for fut in futures:
                idx, res = fut.result()
                results[idx] = res
        # Every slot is filled by construction.
        return [r if r is not None else _error_result("unknown", "missing result") for r in results]

    # ----- TRL / verl reward_fn -------------------------------------------

    def reward_fn(
        self,
        prompts: Union[None, List[str], List[dict]],
        completions: List[str],
        *,
        kata_ids: List[str],
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> List[float]:
        """Drop-in reward function for ``trl.GRPOTrainer(reward_funcs=[...])``.

        ``prompts`` is accepted in any shape TRL emits (``None``,
        ``list[str]``, or ``list[dict]`` for chat-format) and is IGNORED for
        routing — ``kata_ids`` is authoritative. This is the documented
        anti-footgun: the model cannot hallucinate a ``;; kata: foo`` header
        to game which kata grades its completion.

        Returns a list of scalar rewards aligned to ``completions``. Extra
        kwargs are absorbed silently to remain compatible with TRL's evolving
        reward-function calling convention.
        """
        del prompts, kwargs  # explicitly unused
        if len(completions) != len(kata_ids):
            raise ValueError(
                f"reward_fn: len(completions)={len(completions)} != "
                f"len(kata_ids)={len(kata_ids)}; the trainer must supply one "
                f"kata id per completion."
            )
        results = self.grade_batch(
            list(zip(kata_ids, completions)),
            timeout=timeout,
        )
        return [float(r.get("reward", -0.1)) for r in results]

    # ----- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the underlying pool (only if we own it). Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._owns_pool:
            self._pool.close()

    def __enter__(self) -> "MacroGrader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def stats(self) -> dict:
        return self._pool.stats

    @property
    def pool(self) -> SBCLPool:
        return self._pool


# ---------------------------------------------------------------------------
# Module-level singleton


_DEFAULT: Optional[MacroGrader] = None
_DEFAULT_LOCK = threading.Lock()


def get_grader() -> MacroGrader:
    """Return a process-wide :class:`MacroGrader`, creating it on first call.

    Reads ``MACRO_GYM_*`` env vars at first construction; subsequent calls
    return the same instance regardless of env-var changes. Call
    :func:`shutdown_grader` to reset.

    The singleton is registered with :mod:`atexit` so its SBCL subprocesses
    are reaped at interpreter shutdown even if the caller never calls
    :func:`shutdown_grader` explicitly.
    """
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = MacroGrader()
                atexit.register(shutdown_grader)
    return _DEFAULT


def shutdown_grader() -> None:
    """Close and drop the module-level grader singleton, if any."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is not None:
            try:
                _DEFAULT.close()
            finally:
                _DEFAULT = None
