;;; server.lisp — macro-gym v0.3 verifier-first grader.
;;;
;;; Public contract (consumed by Python `macro_gym.sbcl.SBCLProcess`):
;;;
;;;   stdin:  one s-expression per request.
;;;             (grade "kata-id" "macro-source-string")
;;;             (eval-macro "kata-id" "macro-source-string")   ; legacy alias
;;;             (:eof)                                          ; clean shutdown
;;;
;;;   stdout: length-prefixed UTF-8 plist responses:
;;;             <decimal-byte-count>\n<payload>\n
;;;
;;; Safety surface:
;;;   - `*read-eval* nil` + fresh `*readtable*` when reading hostile source
;;;   - per-kata package isolation (symbols don't leak across katas)
;;;   - bounded macroexpansion depth (default 64)
;;;   - hard kill via `sb-thread:terminate-thread` on timeout
;;;
;;; This is NOT a capability sandbox — see docs/grader.md threat model.

;; Load sibling sources so the bare `sbcl --script lisp/server.lisp`
;; invocation (used by `macro_gym.sbcl.SBCLProcess`) still works end-to-end.
;; The ASDF system in macro-gym.asd loads these in dependency order too.
(let ((here (make-pathname
             :defaults (or *load-pathname* *compile-file-pathname*
                           (merge-pathnames "server.lisp")))))
  (dolist (f '("package.lisp" "ted.lisp"))
    (load (merge-pathnames f here))))

(in-package :macro-gym)

;; Tighten compiler policy: model-generated macros land here thousands of
;; times per training run. Don't carry debug instrumentation; speed up the
;; compile pass. Leave SAFETY at the default (>=1) so out-of-bounds and
;; type errors signal cleanly rather than corrupt the worker.
(sb-ext:restrict-compiler-policy 'debug 0)
(sb-ext:restrict-compiler-policy 'speed 3)

;;; ============================================================
;;;   Variable normalization (preserved verbatim — DO NOT EDIT)
;;; ============================================================

(defun collect-lambda-list-vars (ll table)
  "Extract variable names from a destructuring lambda list."
  (cond
    ((symbolp ll)
     (unless (or (keywordp ll)
                 (member ll '(&optional &rest &key &body &aux &whole)))
       (setf (gethash ll table) t)))
    ((consp ll)
     (collect-lambda-list-vars (car ll) table)
     (collect-lambda-list-vars (cdr ll) table))))

(defun mark-binding-vars (form table)
  "Walk FORM and mark all lexical variables (from let, handler-case, etc.) in TABLE."
  (when (consp form)
    (let ((head (car form)))
      (cond
        ((or (eq head 'let) (eq head 'let*))
         (dolist (binding (cadr form))
           (let ((sym (if (consp binding) (car binding) binding)))
             (when (and (symbolp sym) (not (keywordp sym)))
               (setf (gethash sym table) t))))
         (dolist (sub (cddr form)) (mark-binding-vars sub table)))
        ((eq head 'handler-case)
         (dolist (clause (cddr form))
           (let ((var (cadr clause)))
             (when (consp var) (setq var (car var)))
             (when (and (symbolp var) (not (keywordp var)))
               (setf (gethash var table) t))))
         (mark-binding-vars (cadr form) table))
        ((eq head 'multiple-value-bind)
         (dolist (var (cadr form))
           (when (and (symbolp var) (not (keywordp var)))
             (setf (gethash var table) t)))
         (dolist (sub (cdddr form)) (mark-binding-vars sub table)))
        ((eq head 'destructuring-bind)
         (let ((lambda-list (cadr form)))
           (collect-lambda-list-vars lambda-list table))
         (dolist (sub (cdddr form)) (mark-binding-vars sub table)))
        (t
         (dolist (sub form) (mark-binding-vars sub table)))))))

(defun normalize-variables (form)
  "Replace gensyms AND let-binding variables with canonical :V1, :V2, ..."
  (let ((counter 0)
        (canon-map (make-hash-table :test #'eq))
        (binding-vars (make-hash-table :test #'eq)))
    (mark-binding-vars form binding-vars)
    (labels ((gensym-p (s)
               (and (symbolp s)
                    (not (keywordp s))
                    (null (symbol-package s))))
             (walk (f)
               (cond
                 ((and (symbolp f)
                       (not (keywordp f))
                       (or (gensym-p f) (gethash f binding-vars)))
                  (or (gethash f canon-map)
                      (setf (gethash f canon-map)
                            (intern (format nil "V~d" (incf counter)) :keyword))))
                 ((consp f)
                  (cons (walk (car f)) (walk (cdr f))))
                 (t f))))
      (walk form))))

;;; ============================================================
;;;   Conditions
;;; ============================================================

(define-condition macro-gym-timeout (error)
  ((message :initarg :message :initform "macroexpansion timeout" :reader macro-gym-timeout-message))
  (:report (lambda (c s) (format s "~a" (macro-gym-timeout-message c)))))

(define-condition macro-gym-depth-exceeded (error)
  ((depth :initarg :depth :reader macro-gym-depth-exceeded-depth))
  (:report (lambda (c s)
             (format s "macroexpansion depth limit ~d exceeded"
                     (macro-gym-depth-exceeded-depth c)))))

;;; ============================================================
;;;   Kata cache + per-kata package isolation
;;; ============================================================

(defstruct kata
  id
  package
  setup-path
  tests-path
  tests
  cache-mtime)

(defvar *kata-cache* (make-hash-table :test #'equal)
  "Maps kata-id (string) -> KATA struct. Keyed on id; the cached struct's
CACHE-MTIME is compared on lookup. If files have changed since cache,
the entry is invalidated and reloaded.")

(defvar *kata-root* "katas/"
  "Root directory under which kata definitions live. Each kata is a
subdirectory containing setup.lisp and tests.lisp.")

(defun kata-paths (kata-id)
  "Returns (values setup-path tests-path). Signals an error if either is missing."
  (let* ((dir (format nil "~a~a/" *kata-root* kata-id))
         (setup-path (format nil "~asetup.lisp" dir))
         (tests-path (format nil "~atests.lisp" dir)))
    (unless (probe-file setup-path)
      (error "Kata ~a: setup.lisp not found at ~a" kata-id setup-path))
    (unless (probe-file tests-path)
      (error "Kata ~a: tests.lisp not found at ~a" kata-id tests-path))
    (values setup-path tests-path)))

(defun kata-max-mtime (setup-path tests-path)
  "File-write-date of the more-recently modified of the two files."
  (max (or (file-write-date setup-path) 0)
       (or (file-write-date tests-path) 0)))

(defun kata-package-name (kata-id)
  (format nil "KATA-~a" (string-upcase kata-id)))

(defun ensure-kata-package (kata-id)
  "Find or create the kata's package. Use :CL so kata code has the
standard environment available, but symbols intern in the kata package."
  (let ((name (kata-package-name kata-id)))
    (or (find-package name)
        (make-package name :use '(:cl)))))

(defun load-setup-into-package (setup-path package)
  "Re-read setup.lisp inside PACKAGE so freshly-encountered symbols
intern in the kata package rather than :cl-user. Fresh readtable +
*read-eval* nil for hostile-safety hygiene."
  (let ((*package* package)
        (*readtable* (copy-readtable nil))
        (*read-eval* nil))
    (with-open-file (f setup-path :direction :input)
      (loop for form = (read f nil :eof)
            until (eq form :eof)
            do (let ((*read-eval* t))  ; allow eval of safely-read forms
                 (eval form))))))

(defun read-tests (tests-path package)
  "Read the tests alist inside PACKAGE so symbols resolve consistently
with setup.lisp (which also reads/interns inside the kata package).
Fresh readtable + *read-eval* nil for hostile-safety."
  (let ((*package* package)
        (*readtable* (copy-readtable nil))
        (*read-eval* nil))
    (with-open-file (f tests-path :direction :input)
      (read f))))

(defun cached-load-kata (kata-id)
  "Return a KATA struct. If cached and the files haven't changed, returns
the cached instance (eq). Otherwise loads, caches, returns fresh."
  (multiple-value-bind (setup-path tests-path) (kata-paths kata-id)
    (let* ((mtime (kata-max-mtime setup-path tests-path))
           (cached (gethash kata-id *kata-cache*)))
      (when (and cached (eql (kata-cache-mtime cached) mtime))
        (return-from cached-load-kata cached))
      ;; Stale or absent — (re)load. Drop the old package if any, so
      ;; setup-form re-evaluation doesn't trip "already defined" issues.
      (when cached
        (let ((old-pkg (kata-package cached)))
          (when (and old-pkg (find-package old-pkg))
            (ignore-errors (delete-package old-pkg)))))
      (let* ((pkg (ensure-kata-package kata-id))
             (k (make-kata :id kata-id
                           :package pkg
                           :setup-path setup-path
                           :tests-path tests-path
                           :cache-mtime mtime)))
        (load-setup-into-package setup-path pkg)
        (setf (kata-tests k) (read-tests tests-path pkg))
        (setf (gethash kata-id *kata-cache*) k)
        k))))

;;; ============================================================
;;;   Safe read + install
;;; ============================================================

(defun safe-read-macro (source)
  "Read a single defmacro form from SOURCE (a string). Defenses:
   - *read-eval* nil blocks `#.(...)` RCE at read time.
   - Fresh *readtable* blocks reader-macro persistence across calls.
Signals an error if the form isn't a (defmacro ...)."
  (let ((form (let ((*read-eval* nil)
                    (*readtable* (copy-readtable nil)))
                (with-input-from-string (s source)
                  (read s)))))
    (unless (and (listp form) (eq (car form) 'defmacro))
      (error "Expected (defmacro ...) form, got: ~s" (and (consp form) (car form))))
    form))

(defun clear-kata-macros (kata-pkg)
  "Unbind any defmacros that previously got installed in KATA-PKG. Required
between grades: without this, a grade that installs `(defmacro broken ...)`
would still see a `with-logging` macro definition left over from a previous
grade, and tests against `with-logging` would silently use the stale
definition — false positive reward."
  (do-symbols (s kata-pkg)
    (when (and (eq (symbol-package s) kata-pkg)
               (macro-function s))
      (fmakunbound s))))

(defun install-macro (defmacro-form)
  "Eval the defmacro form, return the macro name. Errors propagate to
the outer handler-case in evaluate-macro."
  (eval defmacro-form)
  (cadr defmacro-form))

;;; ============================================================
;;;   Bounded macroexpansion + hard timeout
;;; ============================================================

(defvar *max-macroexpand-depth* 64
  "Max nested macroexpansion steps before macroexpand-bounded errors.
Defends against (defmacro foo () `(foo)) style infinite recursion.")

(defun macroexpand-bounded (form)
  "macroexpand-1 walking, bounded by *MAX-MACROEXPAND-DEPTH*. Stops when
expansion fixpoints (form unchanged) — same shape as full macroexpand
but with a hard upper bound on iterations."
  (let ((current form))
    (dotimes (depth *max-macroexpand-depth*
              (error 'macro-gym-depth-exceeded :depth *max-macroexpand-depth*))
      (multiple-value-bind (expanded expanded-p) (macroexpand-1 current)
        (unless expanded-p
          (return-from macroexpand-bounded current))
        (setf current expanded)))))

(defun expand-with-hard-timeout (input timeout-seconds)
  "Run macroexpand-bounded on INPUT in a worker thread; kill the thread
if it exceeds TIMEOUT-SECONDS. Returns the expansion or signals
MACRO-GYM-TIMEOUT. `with-timeout` alone is unreliable against
CPU-bound loops — terminate-thread is the belt to with-timeout's
suspenders."
  (let* ((result nil)
         (err nil)
         (thread (sb-thread:make-thread
                  (lambda ()
                    (handler-case
                        (setf result (macroexpand-bounded input))
                      (error (c) (setf err c))))
                  :name "macro-gym-expander")))
    (handler-case
        (sb-ext:with-timeout timeout-seconds
          (sb-thread:join-thread thread))
      (sb-ext:timeout ()
        (when (sb-thread:thread-alive-p thread)
          (ignore-errors (sb-thread:terminate-thread thread)))
        (error 'macro-gym-timeout
               :message (format nil "macroexpansion exceeded ~as" timeout-seconds))))
    (when err (error err))
    result))

;;; ============================================================
;;;   Grading
;;; ============================================================

(defun safe-write-form (form)
  "PRIN1 a form to string with stable settings. Used for the human-
readable strings in :input/:expected/:actual fields. Disabling
*print-circle* keeps the output free of `#1=` / `#1#` markers that
would confuse downstream diffing."
  (let ((*print-pretty* nil)
        (*print-readably* nil)
        (*print-case* :downcase)
        (*print-circle* nil))
    (write-to-string form :pretty nil :escape t)))

(defun classify-error (c)
  "Map a Lisp condition to an :error plist."
  (let ((cls (string-downcase (symbol-name (type-of c)))))
    (list :type (cond
                  ((typep c 'macro-gym-timeout) "timeout")
                  ((typep c 'macro-gym-depth-exceeded) "depth-exceeded")
                  ((or (typep c 'reader-error)
                       (typep c 'end-of-file))
                   "read-error")
                  ((typep c 'sb-ext:timeout) "timeout")
                  ((or (typep c 'undefined-function)
                       (typep c 'unbound-variable))
                   "install-error")
                  (t "evaluate-error"))
          :message (handler-case (princ-to-string c)
                     (error () "<unprintable condition>"))
          :lisp-condition cls
          :stderr-tail "")))

(defun run-tests (macro-name tests timeout-per-test)
  "Run each test case. Returns (values results passed total sim-scores).
Catches per-test runtime errors (so one bad expansion doesn't void the
whole grade) but lets MACRO-GYM-TIMEOUT and MACRO-GYM-DEPTH-EXCEEDED
propagate to the outer evaluate-macro handler — those mean the macro
itself is pathological, not just wrong on one input, and the contract
maps them to a global reward=-0.1.

SIM-SCORES is a list (one entry per test, in original order) of:
  - a single-float in [0,1]: structural TED similarity between the
    normalised actual and normalised expected expansions
  - NIL: tree above *MAX-TED-NODES* — TED skipped, no signal
  - 0.0 specifically: the macroexpansion errored on this test input
    (per design F6: model produced no tree, so distance is maximal)"
  (declare (ignore macro-name))
  (let ((results nil)
        (passed 0)
        (total (length tests))
        (sim-scores nil))
    (dolist (pair tests)
      (let* ((input (car pair))
             (expected (cdr pair))
             (actual
               (handler-case (expand-with-hard-timeout input timeout-per-test)
                 ;; Let global pathologies propagate up.
                 (macro-gym-timeout (c) (error c))
                 (macro-gym-depth-exceeded (c) (error c))
                 (error (c) (format nil "ERROR: ~a" c))))
             (normalized-expected (normalize-variables expected))
             (normalized-actual (if (stringp actual) actual
                                    (normalize-variables actual)))
             (pass (and (not (stringp actual))
                        (equal normalized-actual normalized-expected))))
        (when pass (incf passed))
        (let ((sim (cond
                     ;; Expansion errored on this input — no tree to compare.
                     ((stringp actual) 0.0)
                     ;; Identical post-normalisation: skip the work, it's 1.0.
                     (pass 1.0)
                     ;; Run TED. May return NIL if either tree exceeds the cap.
                     (t (sexp-similarity normalized-actual
                                          normalized-expected)))))
          (push sim sim-scores))
        (push (list :input (safe-write-form input)
                    :expected (safe-write-form expected)
                    :actual (if (stringp actual) actual (safe-write-form actual))
                    :pass pass)
              results)))
    (values (nreverse results) passed total (nreverse sim-scores))))

(defun aggregate-semantic-eq (sim-scores)
  "Mean of non-NIL entries, or NIL if every entry is NIL. Returns a
single-float so the wire output is consistent — Python parses it as
Optional[float]."
  (let ((vals (remove nil sim-scores)))
    (when vals
      (coerce (/ (reduce #'+ vals) (length vals)) 'single-float))))

(defun reward-for (passed total)
  (cond
    ((zerop total) 0.0)
    ((= passed total) 1.0)
    ((> passed 0) (+ 0.1 (* 0.8 (/ passed total))))
    (t 0.0)))

(defun evaluate-macro (kata-id macro-source &key (per-test-timeout 5))
  "Grade MACRO-SOURCE against the kata identified by KATA-ID. Returns
a plist with :reward :passed :total :results :error :done :semantic-eq-score."
  (handler-case
      (let* ((kata (cached-load-kata kata-id))
             ;; Bind *package* to the kata package for BOTH read and eval
             ;; so the macro name (and any free symbols) intern in the
             ;; same package the tests use.
             (defmacro-form (let ((*package* (kata-package kata)))
                              (safe-read-macro macro-source)))
             ;; Per-grade reset: unbind macros installed by prior grades.
             ;; Without this, a model that submits (defmacro broken ...) would
             ;; pass tests against `with-logging` if a previous grade had
             ;; installed a correct `with-logging`. See test_reward_fn_all_bad.
             (_ (clear-kata-macros (kata-package kata)))
             (macro-name (let ((*package* (kata-package kata)))
                           (install-macro defmacro-form))))
        (declare (ignore _))
        (multiple-value-bind (results passed total sim-scores)
            (let ((*package* (kata-package kata)))
              (run-tests macro-name (kata-tests kata) per-test-timeout))
          (list :reward (reward-for passed total)
                :passed passed
                :total total
                :results results
                :error nil
                :done (and (plusp total) (= passed total))
                :semantic-eq-score (aggregate-semantic-eq sim-scores)
                :semantic-eq-formula *ted-formula-version*)))
    (macro-gym-timeout (c)
      (list :reward -0.1 :passed 0 :total 0 :results nil
            :error (classify-error c)
            :done nil :semantic-eq-score nil :semantic-eq-formula *ted-formula-version*))
    (macro-gym-depth-exceeded (c)
      (list :reward -0.1 :passed 0 :total 0 :results nil
            :error (classify-error c)
            :done nil :semantic-eq-score nil :semantic-eq-formula *ted-formula-version*))
    (error (c)
      (list :reward -0.1 :passed 0 :total 0 :results nil
            :error (classify-error c)
            :done nil :semantic-eq-score nil :semantic-eq-formula *ted-formula-version*))))

;;; ============================================================
;;;   Framed I/O
;;; ============================================================

(defun respond-to-stream (stream plist)
  "Write PLIST to STREAM using length-prefixed framing:
   <decimal-byte-count>\\n<UTF-8 payload>\\n
Byte count is of the encoded payload, not the trailing newline.

NOTE: *print-readably* is bound to NIL. With *print-readably* t SBCL
emits strings as #A((N) base-char . \"...\") to preserve array
specialization on re-read. That format is valid CL but our Python
s-expression parser only handles plain \"...\" strings. *print-escape* t
is enough for plist round-trip (keywords and quoted strings)."
  (let* ((*print-pretty* nil)
         (*print-readably* nil)
         (*print-case* :downcase)
         (*print-escape* t)
         (*print-circle* nil)
         (payload (write-to-string plist :pretty nil :escape t :readably nil))
         (bytes (sb-ext:string-to-octets payload :external-format :utf-8)))
    (format stream "~d~%" (length bytes))
    (write-string payload stream)
    (terpri stream)
    (finish-output stream)))

(defun respond (plist)
  (respond-to-stream *standard-output* plist))

;;; ============================================================
;;;   Main loop
;;; ============================================================

(defun dispatch-request (request)
  "Handle a single parsed request. Returns the plist response, or :exit
to signal clean shutdown."
  (cond
    ((eq request :eof) :exit)
    ((and (listp request) (eq (car request) :eof)) :exit)
    ((and (listp request)
          (or (eq (car request) 'grade)
              (eq (car request) 'eval-macro)))
     (let ((kata-id (second request))
           (macro-source (third request)))
       (unless (stringp kata-id)
         (return-from dispatch-request
           (list :reward -0.1 :passed 0 :total 0 :results nil
                 :error (list :type "protocol-error"
                              :message "kata-id must be a string"
                              :lisp-condition nil
                              :stderr-tail "")
                 :done nil :semantic-eq-score nil :semantic-eq-formula *ted-formula-version*)))
       (unless (stringp macro-source)
         (return-from dispatch-request
           (list :reward -0.1 :passed 0 :total 0 :results nil
                 :error (list :type "protocol-error"
                              :message "macro-source must be a string"
                              :lisp-condition nil
                              :stderr-tail "")
                 :done nil :semantic-eq-score nil :semantic-eq-formula *ted-formula-version*)))
       (evaluate-macro kata-id macro-source)))
    (t
     (list :reward -0.1 :passed 0 :total 0 :results nil
           :error (list :type "protocol-error"
                        :message (format nil "unknown request shape: ~s"
                                         (and (consp request) (car request)))
                        :lisp-condition nil
                        :stderr-tail "")
           :done nil :semantic-eq-score nil :semantic-eq-formula *ted-formula-version*))))

(defun main ()
  (let ((override (init-max-ted-nodes-from-env)))
    (when override
      (format *error-output*
              "~&;; MACRO_GYM_MAX_TED_NODES override: *max-ted-nodes*=~d~%"
              override)))
  (format *error-output* "~&;; macro-gym server v0.3 ready~%")
  (finish-output *error-output*)
  (loop
    (let* ((request (handler-case (read *standard-input* nil :eof)
                      (error (c)
                        (respond (list :reward -0.1 :passed 0 :total 0 :results nil
                                       :error (classify-error c)
                                       :done nil :semantic-eq-score nil :semantic-eq-formula *ted-formula-version*))
                        (return))))
           (response (dispatch-request request)))
      (when (eq response :exit) (return))
      (respond response))))

;;; ============================================================
;;;   Entry point (load-only aware)
;;; ============================================================

;; Tests load this file via run-tests.lisp WITHOUT spawning main. They
;; set CL-USER::*MACRO-GYM-LOAD-ONLY* before LOAD; we honor it here.
(unless (and (boundp 'cl-user::*macro-gym-load-only*)
             (symbol-value 'cl-user::*macro-gym-load-only*))
  (main))
