;;; test-evaluate.lisp — end-to-end evaluate-macro behavior tests.
;;;
;;; Drives the grader through real katas under the Parachute test
;;; framework. Each test invokes `evaluate-macro` (the same entry point
;;; the dispatcher calls) and asserts on the resulting plist.
;;;
;;; The grader is deterministic: same kata-id + same source -> same
;;; reward. These tests can therefore assert exact reward values, not
;;; just inequalities.

(in-package :cl-user)
(defpackage :macro-gym/test-evaluate
  (:use :cl :parachute :macro-gym))
(in-package :macro-gym/test-evaluate)

(define-test evaluate)

;;; ---- Helpers ---------------------------------------------------------

(defun ev (kata-id source &rest kw)
  "Convenience: call evaluate-macro and return the plist."
  (apply #'macro-gym:evaluate-macro kata-id source kw))

(defun reward (plist) (getf plist :reward))
(defun passed (plist) (getf plist :passed))
(defun total  (plist) (getf plist :total))
(defun err    (plist) (getf plist :error))
(defun err-type (plist) (getf (getf plist :error) :type))

;;; ---- Happy path ------------------------------------------------------

(define-test (evaluate happy-path-with-logging)
  "A correct with-logging macro must score reward=1.0 with all tests
passing. This is the canonical positive signal — if it breaks, the
grader is broken."
  (let* ((src "(defmacro with-logging (ctx &body body)
                 `(progn
                    (log-enter ,ctx)
                    (let ((result (progn ,@body)))
                      (log-leave ,ctx result)
                      result)))")
         (r (ev "with-logging" src)))
    (is = 1.0 (reward r) "Correct macro must score 1.0.")
    (is = (total r) (passed r) "passed == total for full-credit.")
    (true (plusp (total r)) "Kata must have at least one test case.")
    (false (err r) "No error on happy path.")
    (true (getf r :done) ":done must be t on full pass.")
    (is = 1.0 (getf r :semantic-eq-score)
        ":semantic-eq-score is 1.0 on a full pass (TED self-distance).")
    (is equal "ted-zs-v1" (getf r :semantic-eq-formula)
        ":semantic-eq-formula tags the algorithm version on every successful grade.")))

;;; ---- Syntax / read errors --------------------------------------------

(define-test (evaluate malformed-defmacro)
  "Source that isn't a defmacro form (defun, etc.) is the 'didn't even
try' bucket — classified as no-defmacro with reward -0.10. This is the
deepest negative because the rollout produced nothing the grader can
meaningfully critique."
  (let* ((r (ev "with-logging" "(defun not-a-macro () nil)"))
         (e (err r)))
    (is = -0.10 (reward r) "Non-defmacro is no-defmacro, reward -0.10.")
    (is equal "no-defmacro" (getf e :type)
        "Non-defmacro form bucket is :type 'no-defmacro'.")
    (is = 0 (passed r))
    (true (stringp (getf e :message)) "error :message is a string.")))

(define-test (evaluate unparseable-source)
  "Unbalanced parens -> reader signals -> classified as read-error and
mapped to reward -0.07 (closer to working than no-defmacro). Granular
error rewards: see ERROR-REWARD-FOR-TYPE in server.lisp."
  (let* ((r (ev "with-logging" "(defmacro foo (x"))
         (e (err r)))
    (is = -0.07 (reward r) "Read errors map to -0.07.")
    (is equal "read-error" (getf e :type)
        "Reader errors classify as read-error.")))

;;; ---- Partial credit --------------------------------------------------

(define-test (evaluate partial-credit)
  "A defmacro that returns a wrong (but well-formed) expansion for SOME
tests should land 0.0 < reward < 1.0. We can't easily construct a
1-of-2-passing macro without a fragile golden, so assert the weaker
invariant: a totally-wrong-shape macro on a multi-test kata scores 0
or negative, and a correct macro scores 1.0 — and the partial
formula `0.1 + 0.8*(p/t)` is what reward-for computes."
  ;; Direct unit-test the reward formula across the partial regime.
  (is = 0.5  (macro-gym::reward-for 1 2))   ; 0.1 + 0.8 * 0.5
  (true (< (abs (- (float (macro-gym::reward-for 2 3) 1.0s0) 0.633333)) 0.0001)
        "Reward 2/3 within tolerance of 0.633")
  (is = 0.0  (macro-gym::reward-for 0 3))
  (is = 1.0  (macro-gym::reward-for 3 3)))

(define-test (evaluate wrong-expansion-scores-zero)
  "A macro that compiles but produces a wrong expansion for every test
case scores 0.0 (no passes, no error)."
  (let* ((src "(defmacro with-logging (ctx &body body)
                 (declare (ignore ctx body))
                 'wrong)")
         (r (ev "with-logging" src)))
    (is = 0.0 (reward r) "All-wrong expansion is 0.0, not -0.1.")
    (is = 0 (passed r))
    (true (plusp (total r)))
    (false (err r) "No error — the macro ran, it was just wrong.")))

;;; ---- Timeout (hard-kill via terminate-thread) ------------------------

(define-test (evaluate timeout-hard-kill)
  "A defmacro whose body calls (sleep 30) during expansion must NOT hang
the grader. The hard timeout is 5s + grace; we allow up to 8s wall
clock as a generous CI bound. Timeout stays at -0.10 (pathological)."
  (let* ((src "(defmacro with-logging (ctx &body body)
                 (declare (ignore ctx body))
                 (sleep 30)
                 'unreached)")
         (start (get-internal-real-time))
         (r (ev "with-logging" src :per-test-timeout 2))
         (elapsed (/ (- (get-internal-real-time) start)
                     internal-time-units-per-second)))
    (is = -0.10 (reward r) "Timed-out grade is the -0.10 reward.")
    (true (< elapsed 8)
          (format nil "Timeout did not fire within 8s — elapsed ~,2fs." elapsed))
    (let ((e (err r)))
      (true e "Error plist populated.")
      (is equal "timeout" (getf e :type)
          "Timeout must classify as :type 'timeout'."))))

;;; ---- Recursive macro hits depth bound --------------------------------

(define-test (evaluate recursive-macro-depth-bound)
  "(defmacro foo () `(foo)) would loop forever inside macroexpand. The
bounded walker must error within *max-macroexpand-depth* steps, mapping
to reward -0.10 (pathological) with a depth-exceeded error type."
  (let* ((src "(defmacro with-logging (&rest args)
                 (declare (ignore args))
                 '(with-logging))")
         (r (ev "with-logging" src)))
    (is = -0.10 (reward r))
    (let ((e (err r)))
      (true e)
      (is equal "depth-exceeded" (getf e :type)
          "Recursive macro hits the depth bound, not the timeout."))))

;;; ---- Granular error reward shape (Proposal #1) ----------------------

(define-test (evaluate error-reward-map-unit)
  "ERROR-REWARD-FOR-TYPE pins the negative-side reward distribution.
Trainers rely on these exact floats — any change here MUST coincide
with a wire-version bump and a README reward-table update."
  (is = -0.07 (macro-gym::error-reward-for-type "read-error"))
  (is = -0.05 (macro-gym::error-reward-for-type "install-error"))
  (is = -0.03 (macro-gym::error-reward-for-type "evaluate-error"))
  (is = -0.10 (macro-gym::error-reward-for-type "timeout"))
  (is = -0.10 (macro-gym::error-reward-for-type "depth-exceeded"))
  (is = -0.10 (macro-gym::error-reward-for-type "no-defmacro"))
  (is = -0.10 (macro-gym::error-reward-for-type "protocol-error"))
  (is = -0.10 (macro-gym::error-reward-for-type "anything-unknown"))
  (is = -0.10 (macro-gym::error-reward-for-type nil)))

(define-test (evaluate bounded-full-macroexpand-walks-cons)
  "BOUNDED-FULL-MACROEXPAND descends into cons cells, expanding nested
macro forms recursively. Atoms pass through. The depth + timeout
guards live inside MACROEXPAND-BOUNDED / SB-EXT:WITH-TIMEOUT — this
test pins the structural walk."
  (is equal '(if t 1 nil)
      (macro-gym::bounded-full-macroexpand '(if t 1 nil) 2))
  (is equal 42 (macro-gym::bounded-full-macroexpand 42 2))
  ;; WHEN macroexpands to IF. After deep walk, both should be IFs.
  (let ((deep (macro-gym::bounded-full-macroexpand '(when t (when t 1)) 2)))
    (true (eq (car deep) 'if) "WHEN deep-expands to IF.")))

(define-test (evaluate deep-equal-preserves-full-pass)
  "The deep-equal upgrade must NEVER demote a real full-pass back to
partial. Happy path with-logging already covered above; this asserts
that the upgrade pass is a no-op when passed = total."
  (let* ((src "(defmacro with-logging (ctx &body body)
                 `(progn
                    (log-enter ,ctx)
                    (let ((result (progn ,@body)))
                      (log-leave ,ctx result)
                      result)))")
         (r (ev "with-logging" src)))
    (is = 1.0 (reward r))
    (is = (total r) (passed r))
    (dolist (test (getf r :results))
      (false (getf test :upgraded)
             "No :upgraded marker on already-passing tests."))))

(define-test (evaluate no-defmacro-distinct-from-read-error)
  "Trainer-side guarantee: 'didn't write defmacro' and 'wrote
malformed defmacro' are DIFFERENT reward signals. GRPO advantage
normalisation needs both buckets distinguishable, not collapsed to a
single -0.1 floor."
  (let* ((not-defmacro (ev "with-logging" "(progn 1 2 3)"))
         (read-err (ev "with-logging" "(defmacro foo")))
    (is = -0.10 (reward not-defmacro))
    (is = -0.07 (reward read-err))
    (true (/= (reward not-defmacro) (reward read-err))
          "no-defmacro and read-error must differ — that's the entire point of #1.")))

;;; ---- install-error: undefined function in body -----------------------

(define-test (evaluate install-error-undefined-fn)
  "A defmacro whose body calls an undefined function at expansion time
fails per-test (during macroexpand-1). No expansion succeeds, so
passed=0 and reward=0.0 (the macro itself installed cleanly; the
errors are per-test). We accept either: 0.0 (no passes, no global
error) OR -0.1 (if the implementation surfaces the error globally).
Both shapes are non-positive and non-hanging — the safety property."
  (let* ((src "(defmacro with-logging (ctx &body body)
                 (declare (ignore ctx body))
                 (this-function-does-not-exist-anywhere))")
         (r (ev "with-logging" src)))
    (true (<= (reward r) 0.0)
          (format nil "Reward must be non-positive on install error, got ~a." (reward r)))
    (is = 0 (passed r) "No tests can pass when expansion always errors.")))
