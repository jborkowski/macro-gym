;;; run-tests.lisp — Lisp test runner. Invoke from CI:
;;;   sbcl --noinform --non-interactive --script tests/run-tests.lisp
;;;
;;; Exit code: 0 if all green, 1 if any failures. CI gate.
;;;
;;; Parachute is vendored under tests/vendor/parachute/ (single file or
;;; submodule). No Quicklisp dependency in CI so the test runner stays
;;; hermetic. To regenerate the vendor copy:
;;;   git submodule add https://github.com/Shinmera/parachute tests/vendor/parachute

(declaim (optimize (safety 3) (debug 2) (speed 1)))

;; Load Parachute. Fall back to ASDF/QL if the vendor copy is missing,
;; so local development without a vendored copy still works.
;; Ensure ASDF is loaded (SBCL ships with it but doesn't require it in
;; --script mode by default).
(require :asdf)

;; Try Quicklisp first (most local SBCL installs have a quicklisp-init
;; in their userinit, but --script doesn't load userinit by default;
;; check for an explicit setup.lisp in the common location).
(let ((ql-setup (merge-pathnames "quicklisp/setup.lisp"
                                 (user-homedir-pathname))))
  (when (probe-file ql-setup)
    (load ql-setup)))

(let ((parachute-asd (merge-pathnames "vendor/parachute/parachute.asd"
                                      (make-pathname
                                        :directory (pathname-directory *load-pathname*)))))
  (cond
    ((probe-file parachute-asd)
     (asdf:load-asd parachute-asd)
     (asdf:load-system :parachute))
    ((find-package :ql)
     (funcall (find-symbol "QUICKLOAD" :ql) :parachute))
    (t
     (format *error-output* "~&;; ERROR: Parachute not found at ~a and Quicklisp not loaded.~%"
             parachute-asd)
     (format *error-output* ";; Fix: install Quicklisp, OR git submodule add https://github.com/Shinmera/parachute tests/vendor/parachute~%")
     (sb-ext:exit :code 2))))

;; Load grader code WITHOUT starting the server main-loop. We need a
;; load-only entrypoint in lisp/server.lisp that the test runner can use.
;; Convention: setting *macro-gym-load-only* before loading suppresses (main).
(defparameter cl-user::*macro-gym-load-only* t)
(load (merge-pathnames "../lisp/server.lisp"
                       (make-pathname
                         :directory (pathname-directory *load-pathname*))))

;; Load test files.
(let ((test-dir (merge-pathnames "lisp/"
                                 (make-pathname
                                   :directory (pathname-directory *load-pathname*)))))
  (dolist (file '("test-normalize.lisp"
                  "test-safety.lisp"
                  "test-framing.lisp"
                  "test-kata-cache.lisp"
                  "test-evaluate.lisp"))
    (let ((path (merge-pathnames file test-dir)))
      (if (probe-file path)
          (load path)
          (format *error-output* ";; SKIP missing test file: ~a~%" path)))))

;; Run all tests under the :macro-gym test namespace (suite). Parachute
;; collects results and reports them. Exit code reflects pass/fail.
(format t "~&;; ============================================================~%")
(format t ";;   macro-gym Lisp test suite~%")
(format t ";; ============================================================~%")

(let ((suites '(:macro-gym/test-normalize
                :macro-gym/test-safety
                :macro-gym/test-framing
                :macro-gym/test-kata-cache
                :macro-gym/test-evaluate))
      (all-passed t))
  (dolist (suite suites)
    (when (find-package suite)
      (format t "~&;; ---- running suite ~a ----~%" suite)
      (let ((results (ignore-errors (funcall (find-symbol "TEST" :parachute) suite))))
        (when results
          ;; Parachute API: (parachute:results-with-status :failed RESULTS)
          ;; returns the list of failed sub-results. Empty list = clean.
          (let* ((rws (find-symbol "RESULTS-WITH-STATUS" :parachute))
                 (failed (when rws
                           (ignore-errors (funcall rws :failed results)))))
            (when (and failed (consp failed))
              (setf all-passed nil)
              (format t ";;   ! suite ~a had ~d failure(s)~%" suite (length failed))))))))
  (format t "~&;; ============================================================~%")
  (format t ";;   OVERALL: ~a~%" (if all-passed "ALL TESTS PASSED" "FAILURES DETECTED"))
  (format t ";; ============================================================~%")
  (sb-ext:exit :code (if all-passed 0 1)))
