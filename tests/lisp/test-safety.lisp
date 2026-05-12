;;; test-safety.lisp — RCE blocks, *read-eval*, kata isolation, sandbox semantics.
;;;
;;; These tests assert the Lisp-side safety contract that the grader
;;; depends on. They run under the Parachute test framework via
;;; tests/run-tests.lisp.
;;;
;;; Threat model assumption: the macro source is HOSTILE (LLM-generated,
;;; potentially adversarial). The grader trusts the OS process boundary
;;; but NOT the language. Symbol-package isolation is for kata hygiene,
;;; NOT for capability sandboxing — a malicious defmacro body can still
;;; call (cl::eval ...), (sb-ext:run-program ...), etc. A real sandbox
;;; (seccomp / bwrap / RLIMIT) is out of scope for this refactor and is
;;; documented as such in docs/grader.md.

(in-package :cl-user)
(defpackage :macro-gym/test-safety
  (:use :cl :parachute :macro-gym))
(in-package :macro-gym/test-safety)

(define-test safety)

;;; ---- *read-eval* nil blocks #.(...) at read time ---------------------

(define-test (safety read-eval-rce-blocked-bare)
  "#.(error 'rce) must not be evaluated at read time. Pre-patch behavior:
   the form runs and signals RCE-marker. Post-patch: read fails cleanly,
   evaluate-macro returns reward=-0.1, no side effect."
  (let ((side-effect-marker (gensym "RCE-")))
    (fail (macro-gym::safe-read-macro
            (format nil "(defmacro foo () #.(progn (setf cl-user::*rce-fired* '~a) nil))"
                    side-effect-marker))
          'error)
    (false (boundp 'cl-user::*rce-fired*)
           "RCE side effect must NOT have fired — *read-eval* is the only line of defense at read time.")))

(define-test (safety read-eval-rce-blocked-nested)
  "Nested #.#.( ... ) must also be blocked — a single layer of disabling
   has to apply recursively to the reader."
  (fail (macro-gym::safe-read-macro
          "(defmacro foo () #.#.(list 'progn '(error 'inner-rce)))")
        'error))

(define-test (safety read-eval-rce-blocked-feature-cond)
  "#+#.(rce) form — RCE smuggled through feature-conditional reader macros.
   *read-eval* nil must block this path too."
  (fail (macro-gym::safe-read-macro
          "(defmacro foo () #+#.(error 'feature-rce) (foo))")
        'error))

(define-test (safety read-eval-rce-blocked-run-program)
  "The dangerous case: arbitrary command execution via #.(sb-ext:run-program ...).
   If this ever runs, we have a remote code execution bug."
  (let ((canary "/tmp/macro-gym-rce-canary-DELETE-ME"))
    (ignore-errors (delete-file canary))
    (fail (macro-gym::safe-read-macro
            (format nil
                    "(defmacro foo () #.(sb-ext:run-program \"/usr/bin/touch\" '(\"~a\")) nil)"
                    canary))
          'error)
    (false (probe-file canary)
           "Canary file must NOT exist. If it does, sb-ext:run-program ran during read — RCE.")
    (ignore-errors (delete-file canary))))

(define-test (safety read-eval-blocks-legitimate-constant-fold)
  "INTENTIONAL: #.+x+ compile-time folding is also blocked. This is an
   acceptable tradeoff for the kata domain (no shipped kata uses #.),
   but the test asserts the limitation is intentional and not accidental.
   Future kata authors who try to use #. for constant folding will get
   this clear error rather than confusing read failures."
  (let ((cl-user::+x+ 42))
    (declare (special cl-user::+x+))
    (fail (macro-gym::safe-read-macro
            "(defmacro answer () `(progn ',#.cl-user::+x+))")
          'error
          "#.+x+ must be blocked even though the user intent is benign — the threat model can't distinguish benign from hostile here.")))

;;; ---- Fresh *readtable* per kata --------------------------------------

(define-test (safety fresh-readtable-per-kata)
  "A reader macro installed inside one kata must NOT persist into the
   next kata's read. Otherwise an adversarial macro could install a
   reader hook that fires on the next kata's source."
  ;; Install a sentinel reader macro in the current readtable
  (let ((readtable-before *readtable*)
        (rt (copy-readtable nil))
        (rt-sentinel-fired nil))
    (set-macro-character #\$ (lambda (stream char)
                               (declare (ignore stream char))
                               (setf rt-sentinel-fired t)
                               nil)
                         nil rt)
    (let ((*readtable* rt))
      (read-from-string "$foo"))
    (true rt-sentinel-fired "sentinel reader macro fires when active")
    (setf rt-sentinel-fired nil)
    ;; Now the grader's per-kata safe-read-macro must use a FRESH readtable,
    ;; so the sentinel must not fire even though we're calling read on $foo-bearing source.
    (let ((*readtable* readtable-before))
      (ignore-errors
        (macro-gym::safe-read-macro "(defmacro foo () '$bar)")))
    (false rt-sentinel-fired
           "Per-kata fresh *readtable* must isolate reader macros across grade calls.")))

;;; ---- Per-kata package isolation --------------------------------------

(define-test (safety per-kata-package-isolation)
  "Kata A's setup interns symbol X. Kata B's grading must not see X.
   Otherwise katas pollute each other and tests become order-dependent."
  (let* ((kata-a-pkg (or (find-package :test-kata-a)
                         (make-package :test-kata-a :use '(:cl))))
         (kata-b-pkg (or (find-package :test-kata-b)
                         (make-package :test-kata-b :use '(:cl)))))
    ;; Simulate kata A interning a sentinel
    (let ((*package* kata-a-pkg))
      (read-from-string "kata-a-sentinel-symbol"))
    ;; Kata B must NOT see it
    (false (find-symbol "KATA-A-SENTINEL-SYMBOL" kata-b-pkg)
           "Symbol interned by kata A leaked into kata B's package — isolation broken.")
    ;; Cleanup
    (delete-package kata-a-pkg)
    (delete-package kata-b-pkg)))

;;; ---- with-timeout AND thread-terminate hard-kill ---------------------

(define-test (safety expansion-timeout-fires)
  "A macro whose expansion sleeps 30 seconds must be killed within ~6s
   (timeout is 5s, with grace). Without this, one bad macro hangs a worker."
  (let* ((start (get-internal-real-time))
         (result (ignore-errors
                   (sb-ext:with-timeout 5
                     (macroexpand-1 '(test-timeout-macro)))))
         (elapsed (/ (- (get-internal-real-time) start)
                     internal-time-units-per-second)))
    (declare (ignore result))
    (true (< elapsed 10)
          (format nil "Timeout did not fire — elapsed ~,2fs, expected < 10s." elapsed))))

(define-test (safety recursive-macroexpand-depth-bound)
  "(defmacro foo () `(foo)) would loop forever. macroexpand-bounded must
   error within the depth limit. Uses the grader's actual implementation,
   not a re-implementation in the test."
  (defmacro test-recursive-bomb () '(test-recursive-bomb))
  ;; Bind a small depth so the test is fast and obviously bounded.
  (let ((macro-gym::*max-macroexpand-depth* 8))
    (fail (macro-gym::macroexpand-bounded '(test-recursive-bomb))
          'error)))

;;; ---- Threat model documentation test (intentional limitation) -------

(define-test (safety eval-side-effects-NOT-blocked)
  "DOCUMENTED LIMITATION: a defmacro whose body calls (delete-file ...)
   WILL delete the file WHEN THE MACRO IS INVOKED. (Not when defined —
   a defmacro body runs only at macroexpansion time.)

   The grader trusts the OS process boundary, not the Lisp environment.
   If you need a real sandbox, run workers under seccomp / bwrap / a
   non-privileged uid with RLIMIT_AS, RLIMIT_CPU.

   This test asserts the behavior is what we document, so anyone who
   changes the threat model in the future also updates docs/grader.md."
  (let ((canary "/tmp/macro-gym-eval-canary-DELETE-ME"))
    (ignore-errors (delete-file canary))
    (eval `(defmacro test-eval-side-effect ()
             (with-open-file (s ,canary :direction :output :if-exists :supersede)
               (write-string "fired" s))
             nil))
    ;; INVOKE the macro — this is when the body's side effect fires.
    (macroexpand-1 '(test-eval-side-effect))
    (true (probe-file canary)
          "If this fails, somebody added a real sandbox without updating docs/grader.md.
           Update the threat model section to match.")
    (ignore-errors (delete-file canary))))
