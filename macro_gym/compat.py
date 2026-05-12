"""Backward-compat Gymnasium shim over the shared MacroGrader.

v0.2 exposed ``MacroEnv`` as the primary API. v0.3 promotes ``MacroGrader``
to that role; ``MacroEnv`` remains as a thin gym.Env adapter so existing
``examples/agent.py`` and any SB3 / CleanRL users keep working without a
code change.

This module owns NOTHING — it does not spawn an SBCL subprocess, does
not manage worker lifecycles, does not maintain a kata cache. All grading
is delegated to a process-wide :class:`MacroGrader` obtained via
:func:`macro_gym.grader.get_grader`, so multiple ``MacroEnv`` instances
share a single worker pool (the whole point of the refactor).

``close()`` is therefore a no-op: the grader outlives the env.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import gymnasium as gym

from .grader import MacroGrader, get_grader


KATAS_DIR = Path(__file__).parent.parent / "katas"


def list_katas() -> list[str]:
    """Return sorted kata ids discovered under the package's ``katas/`` dir."""
    if not KATAS_DIR.is_dir():
        return []
    return sorted(
        d.name for d in KATAS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def load_kata(kata_id: str) -> dict:
    """Load kata setup text + test count for observation rendering."""
    kata_dir = KATAS_DIR / kata_id
    if not kata_dir.is_dir():
        raise ValueError(f"Kata '{kata_id}' not found in {KATAS_DIR}")
    setup = (kata_dir / "setup.lisp").read_text()
    tests_text = (kata_dir / "tests.lisp").read_text()
    # Crude test-case count: a test is `(<input> . <expected>)` at top level.
    # Anchoring on `((` lets simple alists work; fall back to 1 if absent.
    n_tests = len(re.findall(r"^\s*\(\(", tests_text, re.MULTILINE)) or 1
    return {"id": kata_id, "setup": setup, "n_tests": n_tests}


def _build_observation(kata: dict, prev_results: list | None = None,
                       error: str | None = None) -> str:
    parts = [
        f";; Kata: {kata['id']}",
        f";; Test cases: {kata['n_tests']}",
        "",
        f";; Codebase pattern found in {kata['n_tests']} locations:",
        kata["setup"],
        "",
        ";; Task: write a defmacro that abstracts this pattern.",
        f";; The macro is expected at {kata['n_tests']} call-sites.",
        ";; Use gensym for any temporary variables.",
    ]
    if prev_results:
        parts.append("")
        parts.append(";; Previous attempt results:")
        for r in prev_results:
            status = "PASS" if r.get("pass") else "FAIL"
            parts.append(f";; [{status}] input: {r.get('input', '?')}")
            if not r.get("pass"):
                parts.append(f";;   expected: {r.get('expected', '?')}")
                parts.append(f";;   actual:   {r.get('actual', '?')}")
    if error:
        parts.append(f";; Error: {error}")
    return "\n".join(parts)


class MacroEnv(gym.Env):
    """Gymnasium adapter over a shared :class:`MacroGrader`.

    Constructor accepts an optional ``grader`` for tests / advanced wiring;
    when omitted the module-level grader (``get_grader()``) is used so all
    envs in a process share one worker pool.
    """

    metadata = {"render_modes": ["ansi"]}

    def __init__(self, kata_id: str | None = None, max_steps: int = 5,
                 grader: MacroGrader | None = None):
        super().__init__()
        self.kata_id = kata_id
        self.max_steps = max_steps
        self._grader = grader if grader is not None else get_grader()
        self.action_space = gym.spaces.Text(max_length=4096, min_length=0)
        self.observation_space = gym.spaces.Text(max_length=8192, min_length=0)
        self._kata: dict = {}
        self._step_count = 0
        self._prev_results: list | None = None
        self._last_reward = 0.0

    def reset(self, seed: int | None = None,
              options: dict | None = None) -> tuple[str, dict]:
        super().reset(seed=seed)
        if options and "kata_id" in options:
            self.kata_id = options["kata_id"]
        if self.kata_id is None:
            katas = list_katas()
            if not katas:
                raise RuntimeError(f"No katas found in {KATAS_DIR}")
            self.kata_id = katas[int(self.np_random.integers(0, len(katas)))]
        self._kata = load_kata(self.kata_id)
        self._step_count = 0
        self._prev_results = None
        self._last_reward = 0.0
        return _build_observation(self._kata), {"kata_id": self.kata_id}

    def step(self, action: str) -> tuple[str, float, bool, bool, dict]:
        self._step_count += 1
        result = self._grader.grade(self.kata_id, action.strip())
        reward = float(result.get("reward", 0.0))
        done = bool(result.get("done", False))
        passed = int(result.get("passed", 0))
        total = int(result.get("total", 0))
        err = result.get("error")
        err_msg = err.get("message") if isinstance(err, dict) else err
        self._prev_results = result.get("results", []) or []
        self._last_reward = reward
        truncated = self._step_count >= self.max_steps and not done
        obs = _build_observation(self._kata, self._prev_results, err_msg)
        info = {
            "kata_id": self.kata_id,
            "passed": passed,
            "total": total,
            "step": self._step_count,
            "error": err_msg,
            "results": self._prev_results,
        }
        return obs, reward, done, truncated, info

    def render(self) -> str:
        return (
            f"Kata: {self.kata_id}\n"
            f"Step: {self._step_count}/{self.max_steps}\n"
            f"Last reward: {self._last_reward:.2f}"
        )

    def close(self) -> None:
        # Grader is process-wide and shared; the env never owns it.
        return None
