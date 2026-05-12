# MacroGym

A batch verifier for training language models to write **Common Lisp macros** — code that writes code, scored against SBCL's macroexpand.

## Quickstart

Install from source (not yet on PyPI):

```bash
git clone git@github.com:jborkowski/macro-gym.git
cd macro-gym
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Install SBCL (>=2.0):

| Platform        | Command                                                    |
|-----------------|------------------------------------------------------------|
| Debian / Ubuntu | `apt install sbcl`                                         |
| Fedora / RHEL   | `dnf install sbcl`                                         |
| Arch            | `pacman -S sbcl`                                           |
| macOS           | `brew install sbcl`                                        |
| Docker          | `docker run --rm -it daewok/sbcl:latest`                   |

Verify your install:

```bash
python -m macro_gym.doctor
```

Hello world:

```python
from macro_gym import MacroGrader

grader = MacroGrader(pool_size=6)
src = "(defmacro with-logging (n &body b) `(progn (log-enter ,n) ,@b))"
print(grader.grade("with-logging", src))  # {'reward': ..., 'passed': ..., ...}
```

## Why CL macros?

- **A macro is a function AST → AST** — the model generates a transformation, not final output.
- **Deterministic evaluation** — `macroexpand-1` always produces the same result, zero flakiness.
- **Cheap to test** — no need to run the code, expansion alone is enough.
- **Narrow domain** — just `defmacro` forms, not the entire language.
- **Self-labeling data** — extract `(input, expansion)` pairs from real CL repos automatically.

Variables are structurally normalized (`let` / `handler-case` / `destructuring-bind` bindings → `:V1`, `:V2`), so reward measures structural correctness, not variable naming.

Reward scale: `-0.1` syntax error · `0.0` no expansions match · `0.1`–`0.9` partial · `1.0` all correct.

## GRPO integration

Drop-in compatible with `trl.GRPOTrainer`:

```python
from macro_gym import MacroGrader
from trl import GRPOTrainer

grader = MacroGrader(pool_size=6)

def reward_fn(prompts, completions, **kwargs):
    # kata_ids ride along on the dataset, NOT parsed from the prompt
    return grader.reward_fn(prompts, completions, kata_ids=kwargs["kata_ids"])

trainer = GRPOTrainer(
    model=policy,
    reward_funcs=[reward_fn],
    train_dataset=ds,   # each row: {"prompt": ..., "kata_ids": "with-logging"}
)
trainer.train()
```

`prompts` may be `None`, `list[str]`, or `list[dict]` (chat format) — TRL emits any of these and `reward_fn` accepts them all. `kata_ids` is a **required keyword** so the model can't hallucinate a kata header to game routing.

## API at a glance

```python
class MacroGrader:
    def __init__(self, *, pool_size=6, heap_mb=384, recycle_after=200,
                 default_timeout=10.0, sbcl_path=None, kata_dirs=None): ...

    def grade(self, kata_id: str, macro_src: str, *,
              timeout: float | None = None) -> Result: ...

    def grade_batch(self, items: list[tuple[str, str]],
                    max_workers: int | None = None) -> list[Result]: ...

    def reward_fn(self,
                  prompts: list[str] | list[dict] | None,
                  completions: list[str],
                  *,
                  kata_ids: list[str],
                  **kwargs) -> list[float]: ...

    def close(self) -> None: ...
```

`Result` is a TypedDict with snake_case keys: `reward`, `passed`, `total`, `results`, `error`, `done`.

## Katas

`macro-gym` ships with ~12 katas under `katas/<id>/`:

```
with-logging  with-retry  with-timing  with-transaction  unless-let
aif           with-resource  when-debug  do-while  dovector
defmemo       with-mutex-held  case-of  with-capture-output
```

Each kata is two files:

- `setup.lisp` — preloaded code that simulates the "codebase" the agent observes.
- `tests.lisp` — an alist of `(input-form . expected-expansion)` pairs.

See `docs/kata-authoring.md` for the authoring guide.

## Threat model

The grader trusts the **OS process boundary**, not the Lisp environment. `*read-eval*` is bound to `nil` before the agent's source is read, which blocks `#.(...)` read-time RCE. Package isolation prevents symbol collisions between katas. **However**, a `defmacro` body still runs under `eval` at expand time, and SBCL has full filesystem and network access via `cl:open`, `sb-ext:run-program`, etc. Treat each worker as untrusted code. For real isolation, run the grader under `seccomp-bpf` / `bwrap` / a container with no network and a read-only FS.

## Migration from v0.2

`from macro_gym import MacroEnv` still works — it now emits a `DeprecationWarning` and proxies to a shared `MacroGrader`. See [MIGRATION.md](MIGRATION.md) for the full diff.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ MacroGrader              (no gym dependency)            │
│   .grade(kata_id, src)   .grade_batch([...])            │
│   .reward_fn(prompts, completions, *, kata_ids=...)     │
│                                                         │
│   ┌──────────────────────────────────────────────────┐  │
│   │ SBCLPool                                         │  │
│   │   queue.Queue of N persistent workers            │  │
│   │   kata cache: in-memory + in-SBCL                │  │
│   │   health check + auto-restart + RSS / count TTL  │  │
│   └──────────────────────────────────────────────────┘  │
└──────────────┬─────────────────────────┬────────────────┘
               │                         │
               ▼                         ▼
     ┌──────────────────┐      ┌────────────────────────┐
     │ Batch API        │      │ MacroEnv (compat shim) │
     │ for GRPO / verl  │      │ reset / step over the  │
     │ trainers, eval   │      │ shared grader          │
     └──────────────────┘      └────────────────────────┘
                         │
                         ▼
                ┌──────────────────────┐
                │  Worker → SBCL       │
                │  framed stdin/stdout │
                │  (length-prefixed)   │
                └──────────────────────┘
```

## License

MIT
