# MacroGym

A Gymnasium environment for training language models to generate Common Lisp macros. Instead of fine-tuning a model to write code, you fine-tune it to write **code that writes code** — macros that transform AST patterns.

## Why CL macros?

- **Macro is a function AST → AST** — the model generates a transformation, not final output
- **Deterministic evaluation** — `macroexpand-1` always produces the same result, zero flakiness
- **Cheap to test** — no need to run the code, expansion alone is enough
- **Narrow domain** — just `defmacro` forms, not the entire language
- **Self-labeling data** — extract `(input, expansion)` pairs from real CL repos automatically

## How it works

```
Agent sees:                Agent writes:            Environment:
───────────────────────────────────────────────────────────────
;; Pattern repeated        (defmacro with-logging    SBCL: macroexpand-1
;; across 3+ call sites    (name &body body)              on each call site
;; (log-enter "x")          (let ((r (gensym)))      → compare with expected
;; (do-stuff)                `(progn                 → compute reward
;; (log-leave "x" result)      (log-enter ,name)
;;                               (let ((,r ...)))    Returns reward 0..1
;; Task: abstract into           (log-leave ,name,r)       + per-test diff
;;        a macro                ,r)))
```

Reward function:
- `-0.1` — syntax error or compilation failure
- `0.0` — compiles but no expansions match
- `0.1`–`0.9` — partial match (some test cases pass)
- `1.0` — all expansions correct, episode done

Variables are structurally normalized (`let`/`handler-case` bindings → `:V1`, `:V2`), so the reward measures structural correctness, not variable naming.

## Installing

```bash
git clone git@github.com:jborkowski/macro-gym.git
cd macro-gym
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires SBCL:

```bash
brew install sbcl          # macOS
apt install sbcl           # Debian/Ubuntu
```

## Usage

```python
from macro_gym import MacroEnv

env = MacroEnv(kata_id="with-logging")
obs, info = env.reset()
print(obs)

# Write your macro
macro = """(defmacro with-logging (name &body body)
  (let ((r (gensym)))
    `(progn
       (log-enter ,name)
       (let ((,r (progn ,@body)))
         (log-leave ,name ,r)
         ,r))))"""

obs, reward, done, truncated, info = env.step(macro)
print(f"Reward: {reward:.2f}  Passed: {info['passed']}/{info['total']}")
```

Example agents:

```bash
python examples/agent.py --show-solution    # test reference solutions
python examples/agent.py --kata with-logging # interactive loop
```

## Katas

Katas are self-contained directories under `katas/<id>/`:

- `setup.lisp` — preloaded code that simulates the "codebase" the agent observes
- `tests.lisp` — alist of `(input-form . expected-expansion)` pairs

To add a new kata, create a directory with these two files. No registration needed.

## Architecture

```
┌──────────────┐     s-expression protocol      ┌──────────┐
│   Python     │  ──────────────────────────    │   SBCL   │
│  Gymnasium   │  stdin/stdout (subprocess)     │  server  │
│    Env       │                                 │          │
│              │  (eval-macro <kata> <source>)  │ expands  │
│              │  → (:reward 1.0 :done t ...)   │   macro  │
└──────────────┘                                 └──────────┘
```

## License

MIT
