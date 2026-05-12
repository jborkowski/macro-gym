"""Worker pool over persistent SBCL subprocesses.

:class:`Worker` wraps one :class:`~macro_gym.sbcl.SBCLProcess` plus per-worker
state (grade count, dirty flag, in-use lock). :class:`SBCLPool` is a
thread-safe bounded queue of healthy workers with TTL-based recycling.

The IPC payload coming back from SBCL is a Common Lisp plist with keywords
like ``:reward``, ``:passed``, ``:semantic-eq-score``. We normalize keys to
Python snake_case so callers see ``{"reward": ..., "semantic_eq_score": ...}``.
The Result schema is defined in :mod:`macro_gym.grader`.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .sbcl import SBCLProcess, find_sbcl
from .sexp import parse, plist_to_dict

try:  # optional dependency: RSS-based TTL is a no-op without it
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil is optional
    psutil = None  # type: ignore

__all__ = [
    "Worker",
    "SBCLPool",
    "PoolBusyError",
    "PoolUnhealthyError",
]

log = logging.getLogger("macro_gym.pool")


# ---------------------------------------------------------------------------
# Exceptions


class PoolBusyError(RuntimeError):
    """No worker became available within the checkout timeout."""


class PoolUnhealthyError(RuntimeError):
    """The pool has no live workers and cannot recover."""


# ---------------------------------------------------------------------------
# Key normalization


def _normalize_key(key: Any) -> Any:
    """Convert a Lisp ``:keyword-symbol`` string to Python ``keyword_symbol``.

    Strips a single leading colon, downcases, and turns hyphens into
    underscores. Non-string keys are returned unchanged.
    """
    if not isinstance(key, str):
        return key
    if key.startswith(":"):
        key = key[1:]
    return key.lower().replace("-", "_")


def _strip_lisp_string_quotes(s: str) -> str:
    """sexp.py keeps double-quote characters around string literals (e.g.
    `'"timeout"'`). When we treat these as scalar Python strings (error
    type, message, etc.) we want them WITHOUT the quotes."""
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        # Best-effort de-escape of \" and \\ inside the literal
        inner = s[1:-1]
        return inner.replace('\\"', '"').replace('\\\\', '\\')
    return s


def _normalize_value(value: Any) -> Any:
    """Recursively normalize a parsed plist/list payload.

    Dicts get their keys normalized; lists are walked element-wise. Strings
    have their wrapping double-quotes stripped so callers see plain Python
    str values. Numbers and ``None`` pass through. The Lisp side encodes
    per-test-case detail as ``(:input ... :expected ... :actual ... :pass ...)``
    plists nested inside ``:results``, so we recurse into lists looking for
    those.
    """
    if isinstance(value, dict):
        return {_normalize_key(k): _normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        # If the list looks like a plist (starts with a keyword), promote to dict.
        if (
            len(value) >= 2
            and len(value) % 2 == 0
            and isinstance(value[0], str)
            and value[0].startswith(":")
        ):
            d = plist_to_dict(value)
            return {_normalize_key(k): _normalize_value(v) for k, v in d.items()}
        return [_normalize_value(v) for v in value]
    if isinstance(value, str):
        return _strip_lisp_string_quotes(value)
    return value


# ---------------------------------------------------------------------------
# Worker


class Worker:
    """One persistent SBCL subprocess plus pool bookkeeping.

    A worker is checked out of the pool, used for exactly one grade call,
    then returned. The pool inspects :attr:`dirty` and :attr:`grade_count`
    on checkin to decide whether to recycle.
    """

    def __init__(
        self,
        sbcl_path: Optional[str] = None,
        heap_mb: int = 384,
        server_path: Optional[Path] = None,
        cwd: Optional[Path] = None,
        debug_protocol: bool = False,
    ) -> None:
        self.sbcl_path = sbcl_path
        self.heap_mb = heap_mb
        self.server_path = server_path
        self.cwd = cwd
        self.debug_protocol = debug_protocol

        self._proc: Optional[SBCLProcess] = None
        self.grade_count: int = 0
        self.dirty: bool = False
        self.in_use: threading.Lock = threading.Lock()
        self.restart_count: int = 0

    # ----- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            return
        proc = SBCLProcess(
            sbcl_path=self.sbcl_path,
            heap_mb=self.heap_mb,
            server_path=self.server_path,
            cwd=self.cwd,
        )
        proc.start()
        self._proc = proc

    def alive(self) -> bool:
        return self._proc is not None and self._proc.alive()

    @property
    def pid(self) -> Optional[int]:
        if self._proc is None or not self._proc.alive():
            return None
        try:
            return self._proc.pid
        except RuntimeError:
            return None

    def mark_dirty(self) -> None:
        """Flag this worker for restart on its next pool checkin."""
        self.dirty = True

    def restart(self) -> None:
        """Tear down the current SBCL and spawn a fresh one. Resets counters."""
        if self._proc is not None:
            try:
                self._proc.close()
            except Exception:  # noqa: BLE001 - close should never raise but we're paranoid
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None
        self.grade_count = 0
        self.dirty = False
        self.restart_count += 1
        self.start()

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.close()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None

    # ----- diagnostics -----------------------------------------------------

    def rss_mb(self) -> float:
        """Resident set size in MB, or 0 if psutil unavailable / pid gone."""
        if psutil is None or self._proc is None:
            return 0.0
        try:
            pid = self._proc.pid
            return psutil.Process(pid).memory_info().rss / (1024 * 1024)
        except Exception:  # noqa: BLE001 - psutil raises a zoo of process errors
            return 0.0

    def stderr_tail(self) -> str:
        if self._proc is None:
            return ""
        return self._proc.stderr_tail()

    # ----- grading ---------------------------------------------------------

    def grade(self, kata_id: str, macro_src: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Send one ``(grade ...)`` request and return the normalized Result.

        On any IPC error this raises (BrokenPipeError, TimeoutError, etc.) and
        the caller is responsible for marking the worker dirty. Returned dict
        always has snake_case keys.
        """
        if self._proc is None:
            self.start()
        assert self._proc is not None
        request = self._format_request(kata_id, macro_src)
        if self.debug_protocol:
            log.debug("worker pid=%s -> %s", self.pid, request)
        try:
            self._proc.send_request(request)
            payload = self._proc.read_response(timeout=timeout)
        finally:
            self.grade_count += 1
        if self.debug_protocol:
            log.debug("worker pid=%s <- %s", self.pid, payload[:256])
        return self._parse_response(payload)

    # ----- internal helpers ------------------------------------------------

    @staticmethod
    def _format_request(kata_id: str, macro_src: str) -> str:
        escaped_id = kata_id.replace("\\", "\\\\").replace('"', '\\"')
        escaped_src = macro_src.replace("\\", "\\\\").replace('"', '\\"')
        return f'(grade "{escaped_id}" "{escaped_src}")'

    @staticmethod
    def _parse_response(payload_bytes: bytes) -> Dict[str, Any]:
        text = payload_bytes.decode("utf-8")
        parsed = parse(text)
        # parse() returns a dict when the top-level form is a plist; a list
        # otherwise. Normalize both shapes through _normalize_value.
        if isinstance(parsed, dict):
            normalized = {_normalize_key(k): _normalize_value(v) for k, v in parsed.items()}
        elif isinstance(parsed, list):
            normalized = _normalize_value(parsed)
            if not isinstance(normalized, dict):
                # Defensive: wrap a stray non-plist response so callers always
                # see a dict shape. Shouldn't happen with a conforming server.
                normalized = {"raw": normalized}
        else:
            normalized = {"raw": parsed}
        return normalized


# ---------------------------------------------------------------------------
# Pool


class SBCLPool:
    """Thread-safe bounded pool of persistent SBCL workers.

    Workers are spawned lazily on first checkout. After each grade the worker
    is inspected: if ``dirty`` or ``grade_count >= recycle_after``, it is
    restarted in a background thread before being returned to the queue, so
    the caller's hot path is never blocked on SBCL startup.
    """

    def __init__(
        self,
        size: int = 6,
        heap_mb: int = 384,
        recycle_after: int = 200,
        rss_threshold_mb: float = 768.0,
        sbcl_path: Optional[str] = None,
        server_path: Optional[Path] = None,
        cwd: Optional[Path] = None,
        checkout_timeout: float = 30.0,
        debug_protocol: bool = False,
    ) -> None:
        if size < 1:
            raise ValueError(f"pool size must be >= 1, got {size}")
        self.size = int(size)
        self.heap_mb = int(heap_mb)
        self.recycle_after = int(recycle_after)
        self.rss_threshold_mb = float(rss_threshold_mb)
        self.sbcl_path = sbcl_path or find_sbcl()
        self.server_path = server_path
        self.cwd = cwd
        self.checkout_timeout = float(checkout_timeout)
        self.debug_protocol = debug_protocol

        self._available: "queue.Queue[Worker]" = queue.Queue()
        self._all_workers: List[Worker] = []
        self._in_use: Set[Worker] = set()
        self._restart_threads: Set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._spawned = False

        # Aggregate stats (independent of worker churn).
        self._total_grades = 0
        self._total_restarts = 0

    # ----- lazy spawn ------------------------------------------------------

    def _ensure_spawned(self) -> None:
        with self._lock:
            if self._spawned or self._closed:
                return
            for _ in range(self.size):
                w = Worker(
                    sbcl_path=self.sbcl_path,
                    heap_mb=self.heap_mb,
                    server_path=self.server_path,
                    cwd=self.cwd,
                    debug_protocol=self.debug_protocol,
                )
                w.start()
                self._all_workers.append(w)
                self._available.put(w)
            self._spawned = True

    # ----- public API ------------------------------------------------------

    def grade(self, kata_id: str, macro_src: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Checkout a worker, grade, return result (or raise).

        Failure handling:
        * On any exception inside ``worker.grade()`` we mark the worker dirty
          and re-raise. The pool restarts dirty workers asynchronously.
        * If all workers die and can't restart we raise ``PoolUnhealthyError``.
        """
        if self._closed:
            raise RuntimeError("SBCLPool is closed")
        self._ensure_spawned()

        worker = self._checkout()
        exc: Optional[BaseException] = None
        result: Optional[Dict[str, Any]] = None
        try:
            result = worker.grade(kata_id, macro_src, timeout=timeout)
        except BaseException as e:  # noqa: BLE001 - we propagate after cleanup
            exc = e
            worker.mark_dirty()
        finally:
            with self._lock:
                self._total_grades += 1
            self._checkin(worker)
        if exc is not None:
            raise exc
        assert result is not None
        return result

    def close(self) -> None:
        """Close all workers. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = list(self._all_workers)
            self._all_workers.clear()
            pending = list(self._restart_threads)
        # Wait for in-flight restarts so their freshly spawned SBCLs are
        # captured by the close loop below — otherwise a daemon thread can
        # race past us and leak the new subprocess.
        for t in pending:
            t.join(timeout=10.0)
        # Drain queue so future checkouts don't hand out a stale worker.
        try:
            while True:
                self._available.get_nowait()
        except queue.Empty:
            pass
        for w in workers:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            alive = sum(1 for w in self._all_workers if w.alive())
            return {
                "size": self.size,
                "alive": alive,
                "in_use": len(self._in_use),
                "total_grades": self._total_grades,
                "total_restarts": self._total_restarts,
                "closed": self._closed,
            }

    def __enter__(self) -> "SBCLPool":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----- checkout / checkin ---------------------------------------------

    def _checkout(self) -> Worker:
        try:
            worker = self._available.get(timeout=self.checkout_timeout)
        except queue.Empty as e:
            raise PoolBusyError(
                f"No worker available within {self.checkout_timeout}s "
                f"(pool size={self.size}, in_use={len(self._in_use)})"
            ) from e
        with self._lock:
            self._in_use.add(worker)
        # If the queued worker happens to be dead (crashed while idle), repair.
        if not worker.alive():
            try:
                worker.restart()
                with self._lock:
                    self._total_restarts += 1
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._in_use.discard(worker)
                raise PoolUnhealthyError(
                    f"Failed to restart a dead worker on checkout: {e}"
                ) from e
        return worker

    def _checkin(self, worker: Worker) -> None:
        with self._lock:
            self._in_use.discard(worker)
            if self._closed:
                # Pool was closed mid-grade; just drop the worker.
                try:
                    worker.close()
                except Exception:  # noqa: BLE001
                    pass
                return

        needs_restart = (
            worker.dirty
            or worker.grade_count >= self.recycle_after
            or not worker.alive()
            or (psutil is not None and worker.rss_mb() >= self.rss_threshold_mb)
        )
        if not needs_restart:
            self._available.put(worker)
            return

        # Restart in the background so the caller is unblocked immediately.
        t = threading.Thread(
            target=self._restart_and_requeue,
            args=(worker,),
            name=f"sbcl-pool-restart-{worker.pid}",
            daemon=True,
        )
        with self._lock:
            self._restart_threads.add(t)
        t.start()

    def _restart_and_requeue(self, worker: Worker) -> None:
        try:
            # If the pool was closed before we got scheduled, don't spawn a
            # new SBCL — just tear the worker down.
            if self._closed:
                try:
                    worker.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                worker.restart()
                with self._lock:
                    self._total_restarts += 1
            except Exception as e:  # noqa: BLE001
                log.error("Worker restart failed: %s", e, exc_info=True)
                # If every worker is dead, future checkouts will raise
                # PoolUnhealthyError via the checkout-path repair branch.
                with self._lock:
                    if worker in self._all_workers and not any(
                        w.alive() for w in self._all_workers
                    ):
                        log.error("All workers dead; pool is unhealthy")
                # Still re-queue so the checkout path can attempt to recover.
            if self._closed:
                # close() raced past us between the check above and now;
                # reap the freshly-spawned SBCL instead of leaking it.
                try:
                    worker.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            self._available.put(worker)
        finally:
            with self._lock:
                self._restart_threads.discard(threading.current_thread())


def default_pool_size(env_size: Optional[int] = None) -> int:
    """Compute a sensible default pool size: ``min(6, max(1, cpu_count - 2))``."""
    if env_size is not None:
        return max(1, int(env_size))
    import os as _os

    cpus = _os.cpu_count() or 4
    return min(6, max(1, cpus - 2))
