"""Gymnasium environment for Common Lisp macro generation.

Agent observes a codebase pattern that repeats across multiple places,
and must generate a defmacro that abstracts that pattern.

Action: defmacro source code (string)
Observation: kata description + pattern examples (string)
Reward: correctness of macro expansion on test cases (float 0-1)
"""

import json
from pathlib import Path
from typing import Any

import gymnasium as gym

from .sbcl import get_service, shutdown_service, SBCLService


KATAS_DIR = Path(__file__).parent.parent / "katas"


def list_katas() -> list[str]:
    """List available kata IDs."""
    return sorted(
        d.name for d in KATAS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def load_kata(kata_id: str) -> dict:
    """Load kata metadata and observation."""
    kata_dir = KATAS_DIR / kata_id
    if not kata_dir.is_dir():
        raise ValueError(f"Kata '{kata_id}' not found in {KATAS_DIR}")

    setup = (kata_dir / "setup.lisp").read_text()
    tests_file = kata_dir / "tests.lisp"

    # Count test cases
    import re
    tests_text = tests_file.read_text()
    n_tests = len(re.findall(r'\(\(with-', tests_text))

    return {
        "id": kata_id,
        "setup": setup,
        "n_tests": n_tests,
    }


def _build_observation(kata: dict, prev_results: list | None = None) -> str:
    """Build the observation string that the agent sees."""
    parts = [
        f";; Kata: {kata['id']}",
        f";; Test cases: {kata['n_tests']}",
        f"",
        f";; Codebase pattern found in {kata['n_tests']} locations:",
        kata['setup'],
        f"",
        f";; Task: write a defmacro that abstracts this pattern.",
        f";; The macro is expected at {kata['n_tests']} call-sites.",
        f";; Use gensym for any temporary variables.",
    ]

    if prev_results:
        parts.append("\n;; Previous attempt results:")
        for r in prev_results:
            status = "PASS" if r.get(':pass') else "FAIL"
            parts.append(f";; [{status}] input: {r.get(':input', '?')}")
            if not r.get(':pass'):
                parts.append(f";;   expected: {r.get(':expected', '?')}")
                parts.append(f";;   actual:   {r.get(':actual', '?')}")

    return "\n".join(parts)


class MacroEnv(gym.Env):
    """Gymnasium environment for CL macro generation.

    Agent writes a defmacro, environment expands and tests it on
    held-out call sites, returns reward based on expansion correctness.
    """

    metadata = {"render_modes": ["ansi"]}

    def __init__(self, kata_id: str | None = None, max_steps: int = 5):
        super().__init__()

        self.kata_id = kata_id
        self.max_steps = max_steps

        # Action: the defmacro source code
        self.action_space = gym.spaces.Text(
            max_length=2048,
            min_length=0,
        )

        # Observation: kata description + pattern + previous results
        self.observation_space = gym.spaces.Text(
            max_length=8192,
            min_length=0,
        )

        self._kata: dict = {}
        self._step_count = 0
        self._prev_results: list | None = None
        self._last_reward = 0.0
        self._sbcl: SBCLService | None = None

    def reset(
        self,
        seed: int | None = None,
        options: dict | None = None,
    ) -> tuple[str, dict]:
        super().reset(seed=seed)

        if options and "kata_id" in options:
            self.kata_id = options["kata_id"]

        if self.kata_id is None:
            katas = list_katas()
            self.kata_id = katas[self.np_random.integers(0, len(katas))]

        self._kata = load_kata(self.kata_id)
        self._step_count = 0
        self._prev_results = None
        self._last_reward = 0.0

        obs = _build_observation(self._kata, None)
        return obs, {"kata_id": self.kata_id}

    def step(self, action: str) -> tuple[str, float, bool, bool, dict]:
        self._step_count += 1

        # Strip whitespace, ensure it's a defmacro form
        action = action.strip()

        # Per-instance SBCL — each MacroEnv has its own subprocess so
        # ThreadPoolExecutor in the reward fn can parallelise macro
        # evaluation across the host's cores (was singleton, bottleneck).
        if self._sbcl is None:
            self._sbcl = SBCLService()
            self._sbcl.start()
        result = self._sbcl.eval_macro(self.kata_id, action)

        reward = float(result.get(":reward", 0.0))
        done = bool(result.get(":done", False))
        passed = int(result.get(":passed", 0))
        total = int(result.get(":total", 0))
        error = result.get(":error")
        self._prev_results = result.get(":results", [])
        self._last_reward = reward

        # Truncate if we exceeded max steps
        truncated = self._step_count >= self.max_steps and not done

        # Build next observation with feedback
        obs = _build_observation(self._kata, self._prev_results)
        if error:
            obs += f"\n;; Error: {error}"

        info = {
            "kata_id": self.kata_id,
            "passed": passed,
            "total": total,
            "step": self._step_count,
            "error": error,
        }

        return obs, reward, done, truncated, info

    def render(self) -> str:
        """Render current state as ANSI string."""
        parts = [
            f"Kata: {self.kata_id}",
            f"Step: {self._step_count}/{self.max_steps}",
            f"Last reward: {self._last_reward:.2f}",
        ]
        return "\n".join(parts)

    def close(self):
        # Per-instance teardown — only stop OUR SBCL, not the global one.
        if self._sbcl is not None:
            try:
                self._sbcl.stop()
            except Exception:
                pass
            self._sbcl = None


# Register with gymnasium
def make_env(kata_id: str | None = None, **kwargs):
    """Create a MacroEnv instance."""
    return MacroEnv(kata_id=kata_id, **kwargs)
