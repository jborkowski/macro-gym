"""Tests for MacroGrader.reward_fn — the TRL/verl-facing signature.

Skipped wholesale when SBCL isn't on PATH so CI without the toolchain
still runs the rest of the suite.
"""

from __future__ import annotations

import shutil

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("sbcl") is None, reason="SBCL not on PATH"
)


GOOD_WITH_LOGGING = """(defmacro with-logging (ctx-name &body body)
  (let ((r (gensym "RESULT")))
    `(progn
       (log-enter ,ctx-name)
       (let ((,r (progn ,@body)))
         (log-leave ,ctx-name ,r)
         ,r))))"""

BAD_MACRO = "(defmacro broken (x) (this-symbol-does-not-exist x))"


@pytest.fixture(scope="module")
def grader():
    from macro_gym import MacroGrader
    g = MacroGrader(pool_size=2)
    try:
        yield g
    finally:
        g.close()


def test_reward_fn_prompts_none(grader):
    out = grader.reward_fn(
        None,
        [GOOD_WITH_LOGGING],
        kata_ids=["with-logging"],
    )
    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], float)
    assert out[0] == pytest.approx(1.0)


def test_reward_fn_prompts_list_str(grader):
    out = grader.reward_fn(
        [";; kata: with-logging\n;; write the macro"],
        [GOOD_WITH_LOGGING],
        kata_ids=["with-logging"],
    )
    assert len(out) == 1
    assert out[0] == pytest.approx(1.0)


def test_reward_fn_prompts_list_dict(grader):
    # TRL chat-format shape: list[dict] per row, or list[list[dict]].
    out = grader.reward_fn(
        [[{"role": "user", "content": "write with-logging"}]],
        [GOOD_WITH_LOGGING],
        kata_ids=["with-logging"],
    )
    assert len(out) == 1
    assert out[0] == pytest.approx(1.0)


def test_reward_fn_length_mismatch_raises(grader):
    with pytest.raises(ValueError):
        grader.reward_fn(
            None,
            [GOOD_WITH_LOGGING, GOOD_WITH_LOGGING],
            kata_ids=["with-logging"],
        )


def test_reward_fn_returns_correct_length(grader):
    n = 4
    out = grader.reward_fn(
        None,
        [GOOD_WITH_LOGGING] * n,
        kata_ids=["with-logging"] * n,
    )
    assert len(out) == n
    assert all(isinstance(x, float) for x in out)


def test_reward_fn_all_bad(grader):
    n = 3
    out = grader.reward_fn(
        None,
        [BAD_MACRO] * n,
        kata_ids=["with-logging"] * n,
    )
    assert len(out) == n
    # bad → reward is the syntax-error / no-match floor; never >0
    assert all(x <= 0.0 for x in out)
    # at least one should hit the explicit -0.1 sentinel for a broken macro
    assert any(x == pytest.approx(-0.1) for x in out) or all(x == 0.0 for x in out)


def test_reward_fn_order_preserved_via_grade_batch(grader):
    # Mix good and bad, in known order; reward_fn must return aligned floats.
    completions = [GOOD_WITH_LOGGING, BAD_MACRO, GOOD_WITH_LOGGING]
    kata_ids = ["with-logging"] * 3
    out = grader.reward_fn(None, completions, kata_ids=kata_ids)
    assert len(out) == 3
    assert out[0] == pytest.approx(1.0)
    assert out[2] == pytest.approx(1.0)
    assert out[1] <= 0.0  # the broken one in the middle


def test_reward_fn_absorbs_extra_kwargs(grader):
    # TRL passes a grab-bag of kwargs (trainer_state, completion_ids, ...).
    out = grader.reward_fn(
        None,
        [GOOD_WITH_LOGGING],
        kata_ids=["with-logging"],
        trainer_state={"global_step": 42},
        completion_ids=[[1, 2, 3]],
        unrelated="ignored",
    )
    assert out == pytest.approx([1.0])
