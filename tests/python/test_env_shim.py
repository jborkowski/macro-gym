"""Backward-compat: ``MacroEnv`` over the shared ``MacroGrader``.

These tests verify the gym contract that ``examples/agent.py`` and any
SB3/CleanRL caller depends on, plus the DeprecationWarning on the
top-level re-export.
"""

from __future__ import annotations

import shutil
import warnings

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

BAD_MACRO = "(defmacro broken (x) (does-not-exist x))"


@pytest.fixture(scope="module")
def shared_grader():
    from macro_gym.grader import MacroGrader
    g = MacroGrader(pool_size=2)
    try:
        yield g
    finally:
        g.close()


def test_reset_returns_str_dict(shared_grader):
    from macro_gym.compat import MacroEnv
    env = MacroEnv(kata_id="with-logging", grader=shared_grader)
    obs, info = env.reset()
    assert isinstance(obs, str)
    assert isinstance(info, dict)
    assert info.get("kata_id") == "with-logging"
    assert "with-logging" in obs


def test_step_good_macro_full_reward(shared_grader):
    from macro_gym.compat import MacroEnv
    env = MacroEnv(kata_id="with-logging", grader=shared_grader)
    env.reset()
    obs, reward, done, truncated, info = env.step(GOOD_WITH_LOGGING)
    assert isinstance(obs, str)
    assert reward == pytest.approx(1.0)
    assert done is True
    assert truncated is False
    assert isinstance(info, dict)
    assert info["passed"] == info["total"] and info["total"] > 0


def test_step_bad_macro_floor_reward(shared_grader):
    from macro_gym.compat import MacroEnv
    env = MacroEnv(kata_id="with-logging", grader=shared_grader)
    env.reset()
    _, reward, done, _, _ = env.step(BAD_MACRO)
    assert done is False
    assert reward in (-0.1, 0.0) or (-0.1 <= reward <= 0.0)


def test_close_is_noop_grader_survives(shared_grader):
    """env.close() must not tear down the shared grader."""
    from macro_gym.compat import MacroEnv
    env = MacroEnv(kata_id="with-logging", grader=shared_grader)
    env.reset()
    env.close()
    # Grader must still be usable after env.close().
    result = shared_grader.grade("with-logging", GOOD_WITH_LOGGING)
    assert float(result.get("reward", 0.0)) == pytest.approx(1.0)


def test_top_level_import_warns():
    """`from macro_gym import MacroEnv` must emit DeprecationWarning."""
    import importlib
    import macro_gym
    importlib.reload(macro_gym)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _ = macro_gym.MacroEnv  # trigger __getattr__ deprecation path
    msgs = [str(w.message) for w in caught
            if issubclass(w.category, DeprecationWarning)]
    assert any("MacroEnv" in m or "compat" in m for m in msgs), \
        f"expected DeprecationWarning mentioning MacroEnv, got: {msgs}"


def test_compat_import_does_not_warn(shared_grader):
    """`from macro_gym.compat import MacroEnv` is the supported path; no warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from macro_gym.compat import MacroEnv
        env = MacroEnv(kata_id="with-logging", grader=shared_grader)
        env.reset()
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == [], f"unexpected DeprecationWarning(s) from compat path: {dep}"
