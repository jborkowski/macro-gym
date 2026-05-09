"""Example agents for macro-gym.

Demonstrates the gym loop: observe -> generate macro -> get reward -> improve.

Run:
    python examples/agent.py                    # random kata, manual steps
    python examples/agent.py --kata with-logging  # specific kata
    python examples/agent.py --show-solution     # show reference solutions
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from macro_gym import MacroEnv, list_katas


# Reference solutions for the example katas
REFERENCE_MACROS = {
    "with-logging": """(defmacro with-logging (ctx-name &body body)
  (let ((r (gensym "RESULT")))
    `(progn
       (log-enter ,ctx-name)
       (let ((,r (progn ,@body)))
         (log-leave ,ctx-name ,r)
         ,r))))""",

    "with-retry": """(defmacro with-retry (&body body)
  (let ((count (gensym "COUNT"))
        (err (gensym "ERR")))
    `(let ((,count 0))
       (handler-case (progn ,@body)
         (error (,err)
           (incf ,count)
           (if (> ,count *max-retries*)
               (error ,err)
               (progn
                 (sleep (random-backoff))
                 (progn ,@body))))))))""",
}


def run_manual(kata_id: str | None = None):
    """Run the gym manually, showing each step."""
    env = MacroEnv(kata_id=kata_id, max_steps=5)

    obs, info = env.reset()
    print(f"=== Kata: {info['kata_id']} ===\n")
    print(obs)
    print("\n" + "=" * 60)

    done = False
    step = 0
    while not done and step < env.max_steps:
        print(f"\n--- Step {step + 1} ---")
        print("Enter defmacro (or 'quit'):")
        print()
        code = input("> ").strip()
        if code.lower() == "quit":
            break

        obs, reward, done, truncated, info = env.step(code)
        print(f"\nReward: {reward:.2f} | Passed: {info['passed']}/{info['total']}")
        if info.get("error"):
            print(f"Error: {info['error']}")
        if done:
            print("SOLVED!")
        elif truncated:
            print("Max steps reached.")
        print(f"\n{obs}")
        step += 1

    env.close()


def run_reference(kata_id: str | None = None):
    """Run reference solutions against katas."""
    katas = [kata_id] if kata_id else list_katas()
    for kid in katas:
        if kid not in REFERENCE_MACROS:
            print(f"  {kid}: no reference solution yet")
            continue
        env = MacroEnv(kata_id=kid)
        obs, info = env.reset()
        print(f"=== {kid} ===")
        solution = REFERENCE_MACROS[kid]
        print(f"  Trying reference solution...")
        obs, reward, done, truncated, info = env.step(solution)
        print(f"  Reward: {reward:.2f}  Passed: {info['passed']}/{info['total']}  Solved: {done}")
        if info.get("error"):
            print(f"  Error: {info['error']}")
        if not done:
            print("  Results:")
            for r in info.get("results", info.get(":results", [])):  # type: ignore[union-attr]
                status = "PASS" if r.get(":pass") else "FAIL"
                print(f"    [{status}] input: {r.get(':input', '?')}")
                if not r.get(":pass", False):
                    print(f"            expected: {r.get(':expected', '?')}")
                    print(f"            actual:   {r.get(':actual', '?')}")
        env.close()
        print()


def run_single_test(macro_source: str, kata_id: str):
    """Quick test: send one macro, see the result."""
    env = MacroEnv(kata_id=kata_id)
    obs, info = env.reset()
    print(f"Kata: {info['kata_id']}")
    print()

    obs, reward, done, truncated, info = env.step(macro_source)
    print(f"Reward: {reward:.2f}")
    print(f"Passed: {info['passed']}/{info['total']}")
    print(f"Solved: {done}")
    if info.get("error"):
        print(f"Error: {info['error']}")

    # Print per-test results
    results = info.get("results", [])
    for r in results:
        status = "PASS" if r.get(":pass") else "FAIL"
        print(f"\n  [{status}]")
        print(f"    input:    {r.get(':input', '?')}")
        if not r.get(":pass"):
            print(f"    expected: {r.get(':expected', '?')}")
            print(f"    actual:   {r.get(':actual', '?')}")

    env.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Macro Gym agent examples")
    p.add_argument("--kata", type=str, help="Kata ID to run")
    p.add_argument("--show-solution", action="store_true", help="Run reference solutions")
    p.add_argument("--test", type=str, help="Test a specific macro source (as string)")
    args = p.parse_args()

    if args.show_solution:
        run_reference(args.kata)
    elif args.test:
        run_single_test(args.test, args.kata or "with-logging")
    else:
        run_manual(args.kata)
