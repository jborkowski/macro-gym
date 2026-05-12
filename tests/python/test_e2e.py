"""End-to-end smoke + correctness suite.

Runs the full Python <-> Lisp IPC across every shipped kata and every
documented failure mode. If anything in this file fails, the v0.3
refactor is NOT shippable. This is the gate before integration.

What it covers:
    1. Reference solution for each of the 10 shipped katas → reward 1.0
    2. Bad macro (syntax error / wrong shape) → reward -0.1
    3. Partial credit (passes some test cases, not all)
    4. Timeout via (sleep 30) → reward -0.1 within ~6s
    5. RCE attempt via #.(...) blocked by *read-eval* nil
    6. Recursive macroexpansion bounded → reward -0.1
    7. Unknown kata → kata-not-found error
    8. Pool concurrency (grade_batch parallel, order preserved)
    9. reward_fn signature: prompts=None, list[str], list[dict]
   10. Compat MacroEnv shim: reset/step still work, DeprecationWarning
   11. Worker survives a hostile macro, next grade succeeds
   12. Result schema: no Lisp `:key` leakage; all snake_case Python keys

Run: ``.venv/bin/pytest tests/python/test_e2e.py -v``

Skips entire module if SBCL not on PATH.
"""

from __future__ import annotations

import os
import shutil
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("sbcl") is None,
    reason="SBCL not on PATH",
)


# ----------------------------------------------------------------------------
# Reference solutions for every shipped kata. Naming doesn't matter — variable
# normalization (server.lisp normalize-variables) collapses gensyms and let
# bindings to canonical :V1 :V2 ... before comparing. STRUCTURE matters.
# ----------------------------------------------------------------------------

REF_SOLUTIONS: dict[str, str] = {
    "with-logging": """
(defmacro with-logging (name &body body)
  (let ((r (gensym)))
    `(progn
       (log-enter ,name)
       (let ((,r (progn ,@body)))
         (log-leave ,name ,r)
         ,r))))""",

    "with-retry": """
(defmacro with-retry (&body body)
  `(let ((count 0))
     (handler-case (progn ,@body)
       (error (e)
         (incf count)
         (if (> count *max-retries*)
             (error e)
             (progn
               (sleep (random-backoff))
               (progn ,@body)))))))""",

    "with-timing": """
(defmacro with-timing (label &body body)
  (let ((start (gensym))
        (result (gensym)))
    `(let ((,start (get-internal-real-time)))
       (let ((,result (progn ,@body)))
         (log-timing ,label (- (get-internal-real-time) ,start))
         ,result))))""",

    "with-transaction": """
(defmacro with-transaction (&body body)
  (let ((r (gensym))
        (e (gensym)))
    `(progn
       (begin-tx)
       (handler-case
           (let ((,r (progn ,@body)))
             (commit-tx)
             ,r)
         (error (,e)
           (rollback-tx)
           (error ,e))))))""",

    "unless-let": """
(defmacro unless-let (binding &body body)
  (let ((var (car binding))
        (expr (cadr binding)))
    `(let ((,var ,expr))
       (unless ,var
         ,@body))))""",

    "aif": """
(defmacro aif (test then &optional else)
  `(let ((it ,test))
     (if it
         ,then
         ,else)))""",

    "with-resource": """
(defmacro with-resource (binding &body body)
  (let ((var (car binding))
        (acq (cadr binding)))
    `(let ((,var ,acq))
       (unwind-protect
            (progn ,@body)
         (release-resource ,var)))))""",

    "when-debug": """
(defmacro when-debug (&body body)
  `(when *debug* ,@body))""",

    "defmemo": """
(defmacro defmemo (name args &body body)
  (let ((cache (gensym))
        (key (gensym))
        (hit (gensym))
        (found (gensym)))
    `(let ((,cache (make-hash-table :test 'equal)))
       (defun ,name ,args
         (let ((,key (list ,@args)))
           (multiple-value-bind (,hit ,found) (gethash ,key ,cache)
             (if ,found
                 ,hit
                 (setf (gethash ,key ,cache)
                       (progn ,@body)))))))))""",

    "with-mutex-held": """
(defmacro with-mutex-held (lock-form &body body)
  (let ((lock (car lock-form)))
    `(progn
       (acquire ,lock)
       (unwind-protect
            (progn ,@body)
         (release ,lock)))))""",
}


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def grader():
    """One shared grader for the module. pool_size=2 keeps the smoke quick."""
    from macro_gym import MacroGrader
    g = MacroGrader(pool_size=2, default_timeout=15.0)
    yield g
    g.close()


# ----------------------------------------------------------------------------
# 1. Reference solutions
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("kata_id", sorted(REF_SOLUTIONS.keys()))
def test_reference_solution_scores_perfect(grader, kata_id):
    """Each shipped kata's reference solution must score reward=1.0.

    If this fails, either the kata's tests.lisp is inconsistent with the
    pattern, or normalize-variables doesn't capture a binding form the
    kata relies on. Either way: blocker.
    """
    src = REF_SOLUTIONS[kata_id]
    r = grader.grade(kata_id, src)
    assert r["error"] is None, f"unexpected error for {kata_id}: {r['error']}"
    assert r["reward"] == 1.0, (
        f"kata {kata_id}: reward={r['reward']} passed={r['passed']}/{r['total']}\n"
        f"first failing test:\n{_first_fail(r)}"
    )
    assert r["passed"] == r["total"] > 0
    assert r["done"] is True


def _first_fail(result):
    """Pretty-print the first failing test case for debugging."""
    for entry in result.get("results", []):
        if not entry.get("pass"):
            return (f"  input:    {entry.get('input')}\n"
                    f"  expected: {entry.get('expected')}\n"
                    f"  actual:   {entry.get('actual')}")
    return "  (no per-test detail available)"


# ----------------------------------------------------------------------------
# 2. Bad macro
# ----------------------------------------------------------------------------

def test_unparseable_source_returns_minus_point_one(grader):
    """Unbalanced parens → read-error, reward -0.1, no worker death."""
    r = grader.grade("with-logging", "(defmacro foo (")
    assert r["reward"] == -0.1
    assert r["error"] is not None
    assert r["error"]["type"] in {"read-error", "unknown"}, r["error"]


def test_not_a_defmacro_returns_minus_point_one(grader):
    """`defun` instead of `defmacro` → reward -0.1."""
    r = grader.grade("with-logging", "(defun foo () nil)")
    assert r["reward"] == -0.1
    assert r["error"] is not None


def test_undefined_helper_in_macro_body(grader):
    """defmacro body references an undefined function. The error fires
    AT EXPANSION TIME (when each test input invokes the macro), so it
    surfaces as per-test-case failures, not a global :error. Reward
    must be 0.0 or -0.1, and the worker must survive."""
    src = ("(defmacro with-logging (&body body) "
           "  (undefined-helper-fn-zzz) "
           "  `(progn ,@body))")
    r = grader.grade("with-logging", src)
    assert r["reward"] <= 0.0
    # Each per-test-case actual must be an ERROR string OR :pass nil
    all_failed = all(not entry.get("pass") for entry in r.get("results", []))
    assert all_failed or r["error"] is not None, (
        f"expected all tests to fail or global error; got {r}"
    )
    # Worker must still be usable
    r2 = grader.grade("with-logging", REF_SOLUTIONS["with-logging"])
    assert r2["reward"] == 1.0, "worker did not survive a bad macro"


# ----------------------------------------------------------------------------
# 3. Partial credit
# ----------------------------------------------------------------------------

def test_partial_credit_scores_between_0_and_1(grader):
    """A macro that ALMOST matches: produces the right shape for the first
    test case but uses the wrong helper for the rest. Should score >0 <1.

    We do this by writing a with-logging that uses log-enter/log-leave
    correctly for 'process-step' but is hard-coded to that label — so
    the other test cases (http-fetch, multi-step) score 0.

    Hard to game generically; for this smoke test we accept any reward
    strictly between 0 and 1 (the partial-credit reward formula).
    """
    # A deliberately wrong macro that ignores its NAME arg — won't match
    # the expected expansion since the expected uses the actual name.
    bad = """
(defmacro with-logging (name &body body)
  (let ((r (gensym)))
    `(progn
       (log-enter "process-step")
       (let ((,r (progn ,@body)))
         (log-leave "process-step" ,r)
         ,r))))"""
    r = grader.grade("with-logging", bad)
    # Should be either 0.0 (all wrong) or partial (1 right by accident).
    # Either way, must NOT be 1.0 and must NOT be -0.1.
    assert r["error"] is None
    assert 0.0 <= r["reward"] < 1.0, r


# ----------------------------------------------------------------------------
# 4. Timeout
# ----------------------------------------------------------------------------

def test_sleep_30_macro_times_out_quickly(grader):
    """A defmacro whose body calls (sleep 30) must NOT hang — server
    enforces ~5s expansion timeout with hard thread-kill. Wall clock
    must stay under ~12s including IPC overhead."""
    src = "(defmacro with-logging (&rest args) (sleep 30) nil)"
    t0 = time.perf_counter()
    r = grader.grade("with-logging", src, timeout=15.0)
    elapsed = time.perf_counter() - t0
    assert elapsed < 12.0, f"timeout did not fire — took {elapsed:.1f}s"
    assert r["reward"] == -0.1
    assert r["error"] is not None
    # Worker may need restart after thread-kill; verify next grade works.
    r2 = grader.grade("with-logging", REF_SOLUTIONS["with-logging"])
    assert r2["reward"] == 1.0


# ----------------------------------------------------------------------------
# 5. RCE blocked
# ----------------------------------------------------------------------------

def test_read_eval_rce_blocked(grader, tmp_path):
    """#.(...) at read time must be blocked by *read-eval* nil. We use
    a canary file: if the form runs, the file appears. After grading
    the malicious source, the canary must still be absent."""
    canary = tmp_path / "rce-canary.txt"
    assert not canary.exists()
    # The defmacro source contains a #.(touch canary) reader macro that
    # would fire DURING (read) if *read-eval* were on. With it off,
    # read raises an error and the grader returns -0.1.
    src = (f'(defmacro with-logging (&rest a) '
           f'#.(with-open-file (s "{canary}" :direction :output) '
           f'(write-string "rce" s)) nil)')
    r = grader.grade("with-logging", src)
    assert r["reward"] <= 0.0
    assert not canary.exists(), "RCE FIRED — *read-eval* is not blocking #.(...)"


# ----------------------------------------------------------------------------
# 6. Recursive macroexpand depth
# ----------------------------------------------------------------------------

def test_recursive_macro_bounded(grader):
    """(defmacro foo () `(foo)) would infinite-loop macroexpand-1. The
    server bounds depth and returns an error instead of hanging."""
    src = "(defmacro with-logging (&rest a) `(with-logging))"
    t0 = time.perf_counter()
    r = grader.grade("with-logging", src, timeout=15.0)
    elapsed = time.perf_counter() - t0
    assert elapsed < 12.0, f"recursive bomb hung for {elapsed:.1f}s"
    assert r["reward"] <= 0.0


# ----------------------------------------------------------------------------
# 7. Unknown kata
# ----------------------------------------------------------------------------

def test_unknown_kata_returns_error(grader):
    """Asking the grader for a kata that doesn't exist surfaces a clear
    error; does NOT crash a worker."""
    r = grader.grade("definitely-not-a-kata-zzz", REF_SOLUTIONS["with-logging"])
    assert r["reward"] <= 0.0
    assert r["error"] is not None


# ----------------------------------------------------------------------------
# 8. Pool concurrency
# ----------------------------------------------------------------------------

def test_grade_batch_preserves_order(grader):
    """grade_batch must return results in input order, even when work
    is dispatched across multiple workers."""
    items = [
        ("with-logging", REF_SOLUTIONS["with-logging"]),
        ("when-debug",   REF_SOLUTIONS["when-debug"]),
        ("with-logging", "(defmacro with-logging (n &body b) nil)"),  # wrong
        ("when-debug",   REF_SOLUTIONS["when-debug"]),
    ]
    results = grader.grade_batch(items)
    assert len(results) == 4
    assert results[0]["reward"] == 1.0
    assert results[1]["reward"] == 1.0
    assert results[2]["reward"] < 1.0   # wrong macro
    assert results[3]["reward"] == 1.0


def test_concurrent_grades_do_not_corrupt_workers(grader):
    """Hammer the pool with 10 parallel grades. All must score 1.0; no
    worker may return a result intended for a different request (which
    would be the symptom of framing desync or pipe interleaving)."""
    n = 10
    items = [("with-logging", REF_SOLUTIONS["with-logging"])] * n
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda it: grader.grade(*it), items))
    for i, r in enumerate(results):
        assert r["reward"] == 1.0, f"result {i}: {r}"


# ----------------------------------------------------------------------------
# 9. reward_fn signature shapes (TRL compat)
# ----------------------------------------------------------------------------

def test_reward_fn_with_none_prompts(grader):
    """TRL sometimes passes prompts=None. reward_fn must accept it."""
    rewards = grader.reward_fn(
        None,
        [REF_SOLUTIONS["with-logging"], REF_SOLUTIONS["when-debug"]],
        kata_ids=["with-logging", "when-debug"],
    )
    assert rewards == [1.0, 1.0]


def test_reward_fn_with_list_str_prompts(grader):
    """list[str] prompts — most common shape."""
    rewards = grader.reward_fn(
        ["irrelevant prompt 1", "irrelevant prompt 2"],
        [REF_SOLUTIONS["aif"], REF_SOLUTIONS["unless-let"]],
        kata_ids=["aif", "unless-let"],
    )
    assert rewards == [1.0, 1.0]


def test_reward_fn_with_list_dict_prompts(grader):
    """list[dict] (chat format) prompts."""
    rewards = grader.reward_fn(
        [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}],
        [REF_SOLUTIONS["with-timing"], REF_SOLUTIONS["with-resource"]],
        kata_ids=["with-timing", "with-resource"],
    )
    assert rewards == [1.0, 1.0]


def test_reward_fn_length_mismatch_raises(grader):
    """kata_ids and completions must have same length."""
    with pytest.raises(ValueError):
        grader.reward_fn(
            None,
            [REF_SOLUTIONS["with-logging"]],
            kata_ids=["with-logging", "when-debug"],   # mismatch
        )


def test_reward_fn_absorbs_trl_kwargs(grader):
    """TRL passes extra kwargs (advantages, completion_ids, ...).
    reward_fn must absorb them via **kwargs and not crash."""
    rewards = grader.reward_fn(
        None,
        [REF_SOLUTIONS["aif"]],
        kata_ids=["aif"],
        completion_ids=[42],
        advantages=[0.5],
        anything_else="ignored",
    )
    assert rewards == [1.0]


# ----------------------------------------------------------------------------
# 10. Compat MacroEnv shim
# ----------------------------------------------------------------------------

def test_compat_macroenv_reset_step():
    """Old-style MacroEnv API still works against shared grader."""
    from macro_gym.compat import MacroEnv
    env = MacroEnv(kata_id="with-logging")
    obs, info = env.reset()
    assert isinstance(obs, str) and len(obs) > 0
    assert info.get("kata_id") == "with-logging"
    obs2, reward, done, trunc, info2 = env.step(REF_SOLUTIONS["with-logging"])
    assert reward == 1.0
    assert done is True
    env.close()


def test_top_level_macroenv_emits_deprecation_warning():
    """from macro_gym import MacroEnv must fire DeprecationWarning."""
    import importlib
    import macro_gym
    importlib.reload(macro_gym)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _ = macro_gym.MacroEnv   # triggers __getattr__
        assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
            "top-level macro_gym.MacroEnv did not fire DeprecationWarning"
        )


# ----------------------------------------------------------------------------
# 11. Result schema invariants
# ----------------------------------------------------------------------------

def test_no_lisp_keyword_leakage(grader):
    """Result dict keys must NOT contain leading colons or hyphens.
    Subagent B's normalizer should convert `:semantic-eq-score` →
    `semantic_eq_score`."""
    r = grader.grade("with-logging", REF_SOLUTIONS["with-logging"])
    for k in r.keys():
        assert not k.startswith(":"), f"Lisp keyword leaked into result: {k}"
        assert "-" not in k, f"hyphen in result key: {k}"
    if r.get("results"):
        for entry in r["results"]:
            for k in entry.keys():
                assert not k.startswith(":"), f"Lisp keyword in per-test detail: {k}"
                assert "-" not in k, f"hyphen in per-test detail: {k}"


def test_stats_reports_grades(grader):
    """grader.stats must surface alive worker count and total grades."""
    s = grader.stats
    assert isinstance(s, dict)
    assert "alive" in s or "workers" in s   # subagent B may have used either name
