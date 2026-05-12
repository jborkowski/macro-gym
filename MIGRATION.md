# Migrating from v0.2 → v0.3

v0.3 promotes the verifier-first `MacroGrader` to the public API and
demotes `MacroEnv` to a compat shim over a shared grader. Worker
subprocesses are now pooled (default 6, configurable) instead of one
SBCL per env.

## Hard breaks

**None.** Every v0.2 entry point still works.

## Soft breaks

- `from macro_gym import MacroEnv` emits a `DeprecationWarning` the
  first time the symbol is touched. The shim is functionally identical.

  Either silence the warning, switch to the explicit compat import:

  ```python
  from macro_gym.compat import MacroEnv   # no warning
  ```

  …or migrate to `MacroGrader` (recommended for any new code).

- **Result keys lost their `:` prefix.** Internal IPC frames used to
  surface as `{':reward': 1.0, ':passed': 3, ...}`; the grader now
  normalizes these at the IPC boundary and Python callers see
  `{'reward': 1.0, 'passed': 3, ...}`. This only matters if you were
  inspecting raw grader output rather than using `info[...]` keys from
  the env, which were already stripped on the way out.

## New API: `MacroGrader`

```python
from macro_gym import MacroGrader

grader = MacroGrader(pool_size=6)

# single grade
r = grader.grade("with-logging", "(defmacro with-logging ...)")
print(r["reward"], r["passed"], r["total"])

# batch grade — the hot path for GRPO
results = grader.grade_batch([
    ("with-logging", src1),
    ("with-logging", src2),
    ("with-retry",   src3),
])

# TRL/verl-compatible scalar reward
rewards = grader.reward_fn(prompts, completions, kata_ids=kata_ids)

grader.close()
```

## Unchanged

- `examples/agent.py --show-solution` runs unchanged against shipped
  katas.
- Kata format (`setup.lisp` + `tests.lisp`) is unchanged.
- Reward scale (`-0.1` / `0.0` / partial / `1.0`) is unchanged.
- The Gymnasium contract for `MacroEnv.reset` / `step` / `render` /
  `close` is preserved.

## Removed

Nothing. The `SBCLService` singleton and `get_service` / `shutdown_service`
helpers from v0.2 still exist as thin shims around the pool for one
minor version; they will be removed in v0.4.

## Why this refactor

See `thoughts/plans/macro-grader-refactor.md` for the long version.
Short version: GRPO wants `reward_fn(prompts, completions) -> list[float]`
backed by a shared worker pool, not 64 oversubscribed Gymnasium envs on
a 10 vCPU box.
