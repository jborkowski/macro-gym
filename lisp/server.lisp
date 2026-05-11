(defpackage :macro-gym
  (:use :cl)
  (:export :main-loop))

(in-package :macro-gym)

;; Tighten compiler policy so expansions don't carry debug instrumentation
;; and compile/load is fast — model-generated macros land in this server
;; thousands of times per training run.
(sb-ext:restrict-compiler-policy 'debug 0)
(sb-ext:restrict-compiler-policy 'speed 3)

;;; --- Variable normalization for deterministic comparison ---

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

;;; --- Kata loading ---

(defstruct kata
  id
  name
  description
  setup    ; list of forms to evaluate
  tests)   ; alist: ((input . expected-expansion) ...)

(defun load-kata (kata-id)
  "Load kata definition from katas/<id>/ directory."
  (let* ((dir (format nil "katas/~a" kata-id))
         (setup-path (format nil "~a/setup.lisp" dir))
         (tests-path (format nil "~a/tests.lisp" dir)))
    (unless (probe-file dir)
      (error "Kata ~a not found at ~a" kata-id dir))
    (make-kata
     :id kata-id
     :name kata-id
     :setup (with-open-file (f setup-path)
              (loop for form = (read f nil :eof)
                    until (eq form :eof)
                    collect form))
     :tests (with-open-file (f tests-path)
              (read f)))))

;;; --- Macro evaluation ---

(defun safe-read-macro (source)
  "Read a defmacro form from source string, validate it."
  (let ((form (with-input-from-string (s source) (read s))))
    (unless (and (listp form) (eq (car form) 'defmacro))
      (error "Expected (defmacro ...) form, got: ~a" (car form)))
    form))

(defun install-macro (defmacro-form)
  "Eval the defmacro form and return the macro name."
  ;; Pre-compile in a throwaway context first: catches malformed defmacro
  ;; (unbalanced parens, undefined helpers, ill-typed forms) before any
  ;; macro body would run during expansion. Signals an error on bad input
  ;; that the outer handler-case turns into a -0.1 reward, not a hang.
  (handler-case
      (sb-ext:with-timeout 5
        (compile nil `(lambda () ,defmacro-form)))
    (sb-ext:timeout (c)
      (declare (ignore c))
      (error "install-macro: compile timeout (5s)")))
  (eval defmacro-form)
  (cadr defmacro-form))

(defun evaluate-macro (kata-id macro-source)
  "Core evaluation: compile macro, run on all test cases, return results."
  (handler-case
      (let* ((kata (load-kata kata-id))
             (_ (dolist (form (kata-setup kata)) (eval form)))
             (defmacro-form (safe-read-macro macro-source))
             (macro-name (install-macro defmacro-form))
             (tests (kata-tests kata))
             (total (length tests))
             (results nil)
             (passed 0))
        (dolist (pair tests)
          (let* ((input (car pair))
                 (expected (cdr pair))
                 (actual (handler-case (sb-ext:with-timeout 5 (macroexpand-1 input))
                           (sb-ext:timeout (c) (declare (ignore c)) "ERROR: macroexpand-1 timeout (5s)")
                           (error (c) (format nil "ERROR: ~a" c))))
                 (normalized-expected (normalize-variables expected))
                 (normalized-actual (if (stringp actual) actual
                                        (normalize-variables actual))))
            (push (list :input (write-to-string input :pretty nil)
                        :expected (write-to-string expected :pretty nil)
                        :actual (if (stringp actual) actual
                                    (write-to-string actual :pretty nil))
                        :pass (equal normalized-actual normalized-expected))
                  results)))
        (setf results (nreverse results))
        (setf passed (count-if (lambda (r) (getf r :pass)) results))
        (let* ((fraction (if (zerop total) 0.0 (/ passed total)))
               (done (= passed total))
               (reward (cond
                         ((= passed total) 1.0)
                         ((> passed 0) (+ 0.1 (* 0.8 fraction)))
                         (t 0.0))))
          `(:reward ,reward :done ,done :passed ,passed :total ,total
            :results ,results :error nil)))
    (error (c)
      `(:reward -0.1 :done nil :passed 0 :total 0
        :results nil :error ,(format nil "~a" c)))))

;;; --- Server loop ---

(defun respond (plist)
  "Send plist response to stdout."
  (let ((*print-case* :downcase))
    (prin1 plist)
    (terpri)
    (finish-output)))

(defun main-loop ()
  "Read eval-macro requests from stdin, write responses to stdout."
  (let ((*standard-input* *standard-input*)
        (*standard-output* *standard-output*))
    (loop
      (let ((request (read *standard-input* nil :eof)))
        (when (eq request :eof) (return))
        (when (and (listp request) (eq (car request) 'eval-macro))
          (let ((kata-id (second request))
                (macro-source (third request)))
            (respond (evaluate-macro kata-id macro-source))))))))

;;; --- Entry point ---

(defun main ()
  (format *error-output* "~&;; macro-gym server ready~%")
  (finish-output *error-output*)
  (main-loop))

(main)
