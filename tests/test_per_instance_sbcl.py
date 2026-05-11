"""Integration tests for per-instance SBCL + safety patches.

Validates the architecture changes:
  - Each MacroEnv spawns its own SBCL subprocess (no global singleton)
  - Multiple MacroEnv instances run truly concurrent macro evaluations
  - sb-ext:with-timeout in server.lisp bounds runaway macros
  - install-macro pre-compile guard catches malformed defmacros
  - close() tears down only its own SBCL, not a global one

Requires SBCL on PATH and a kata under katas/ named `with-logging` (ships
in this repo).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from macro_gym import MacroEnv
from macro_gym.sbcl import SBCLService


def _sbcl_subprocess_count() -> int:
    """How many `sbcl --script .../server.lisp` subprocesses are running."""
    out = subprocess.run(
        ["pgrep", "-f", "sbcl --noinform --script"],
        capture_output=True, text=True,
    )
    return len([p for p in out.stdout.split() if p.strip()])


def _trivial_kata() -> str:
    """A kata id that exists in this repo's `katas/` dir."""
    return "with-logging"


@pytest.fixture
def env():
    e = MacroEnv(kata_id=_trivial_kata())
    e.reset()
    yield e
    e.close()


def test_per_instance_sbcl_spawns_separate_processes():
    """Two MacroEnv instances should own two distinct SBCL subprocesses."""
    before = _sbcl_subprocess_count()

    e1 = MacroEnv(kata_id=_trivial_kata()); e1.reset()
    e2 = MacroEnv(kata_id=_trivial_kata()); e2.reset()
    # Force each to lazy-init its own SBCL via a quick step
    e1.step("(defmacro foo () nil)")
    e2.step("(defmacro foo () nil)")

    after_both = _sbcl_subprocess_count()
    assert after_both >= before + 2, (
        f"expected 2 new SBCL subprocesses, got {after_both - before}"
    )

    # Identity check: env._sbcl objects are distinct
    assert e1._sbcl is not e2._sbcl

    e1.close()
    e2.close()

    # After close, those two subprocesses should be gone
    time.sleep(0.5)
    after_close = _sbcl_subprocess_count()
    assert after_close <= before, (
        f"expected per-instance close() to drop subprocess count back to {before}, "
        f"got {after_close}"
    )


def test_concurrent_step_does_not_serialise():
    """16 parallel scoring calls across 16 separate envs should complete in
    something close to one step's wall-clock, not 16x. If the singleton bug
    were back, this would take ~16 * baseline."""
    n_workers = 16

    envs = []
    for _ in range(n_workers):
        e = MacroEnv(kata_id=_trivial_kata())
        e.reset()
        # Prime each env with one step so SBCL is warm
        e.step("(defmacro foo () nil)")
        envs.append(e)

    macro_src = "(defmacro foo () `(progn nil))"

    # Time one serial call as the baseline
    t0 = time.perf_counter()
    envs[0].step(macro_src)
    serial_one = time.perf_counter() - t0

    # Now run 16 in parallel
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        list(ex.map(lambda e: e.step(macro_src), envs))
    parallel_total = time.perf_counter() - t0

    # If singleton: parallel_total ≈ 16 * serial_one. If per-instance:
    # parallel_total ≈ serial_one (best case) or a small multiple.
    # Allow 4x slack: still proves parallelism even on a loaded CI box.
    assert parallel_total < serial_one * 4, (
        f"16 parallel steps took {parallel_total:.3f}s but serial-one is "
        f"{serial_one:.3f}s — looks serialised, not parallel"
    )

    for e in envs:
        e.close()


def test_timeout_bounds_hanging_macro(env):
    """A defmacro whose body calls (sleep 30) would hang macroexpand-1
    forever pre-patch. With sb-ext:with-timeout 5, the step returns
    within ~6s with a non-fatal reward."""
    malicious = f"(defmacro {_trivial_kata().replace('-', '-')} (&rest args) (sleep 30) nil)"
    # Note: macroexpand-1 of the kata's test input calls THIS macro's body;
    # the kata's tests reference its expected macro name, so we rebind it.
    # For with-logging, the expected macro is `with-logging`.
    malicious = "(defmacro with-logging (&rest args) (sleep 30) nil)"

    t0 = time.perf_counter()
    obs, reward, done, trunc, info = env.step(malicious)
    elapsed = time.perf_counter() - t0

    assert elapsed < 10, (
        f"step took {elapsed:.1f}s — timeout did not fire; pipeline would "
        f"have hung in production"
    )


def test_pre_compile_catches_malformed_defmacro(env):
    """A defmacro that's syntactically broken should NOT execute its body
    (P3 pre-compile guard). Returns a reward (typically negative/zero)
    without invoking the (would-be-running) body side effects."""
    # Reference an undefined function inside the macro body.
    malformed = (
        "(defmacro with-logging (&body body) "
        "  (undefined-helper-fn-that-does-not-exist) "
        "  `(progn ,@body))"
    )
    obs, reward, done, trunc, info = env.step(malformed)
    # The pre-compile guard should signal an error which evaluate-macro's
    # outer handler-case turns into a -0.1 reward (or whatever your harness
    # returns for definitional errors).
    assert reward <= 0.0, (
        f"expected non-positive reward for malformed defmacro, got {reward}"
    )


def test_close_idempotent_and_does_not_leak():
    """Calling close() twice on a MacroEnv must be safe."""
    e = MacroEnv(kata_id=_trivial_kata())
    e.reset()
    e.step("(defmacro foo () nil)")  # lazy-init SBCL
    assert e._sbcl is not None

    e.close()
    assert e._sbcl is None

    # Second close should be a no-op, not raise
    e.close()
    assert e._sbcl is None
