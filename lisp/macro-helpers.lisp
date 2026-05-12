;;; macro-helpers.lisp — common macro-authoring helpers preloaded into
;;; every kata's package. Kata reference defmacros (especially those
;;; mined from j14i/cl-ds) frequently call `with-gensyms`, `once-only`,
;;; and `with-unique-names` at macro-expansion time. Without these,
;;; the model's submission errors before any structural comparison can
;;; happen — failing the grade for reasons unrelated to macro design.
;;;
;;; These stubs follow the Alexandria conventions (the de-facto CL
;;; standard for these helpers). The expansion shapes match what the
;;; cl-ds dataset's expected-expansion strings encode, so structural
;;; comparison works post-normalization.

(in-package :cl-user)

(defpackage :macro-gym/macro-helpers
  (:use :cl)
  (:export :with-gensyms
           :with-unique-names
           :once-only))

(in-package :macro-gym/macro-helpers)

(defmacro with-gensyms ((&rest names) &body body)
  "Bind each NAME to a freshly-generated symbol for the duration of BODY.
   Standard idiom for authoring hygienic macros that introduce hidden
   temporaries the caller must never see."
  `(let ,(mapcar (lambda (n) `(,n (gensym))) names)
     ,@body))

(defmacro with-unique-names ((&rest names) &body body)
  "Like WITH-GENSYMS but the gensym's printed prefix matches the source
   name, aiding readability when inspecting macroexpansions."
  `(let ,(mapcar (lambda (n) `(,n (gensym ,(symbol-name n)))) names)
     ,@body))

(defmacro once-only (specs &body forms)
  "Evaluate each spec exactly once even if the surrounding macro's
   body references its name multiple times in the expansion. SPEC is
   either a symbol (name and value-form are the same) or a (name value-form)
   pair. Follows Alexandria's canonical implementation."
  (let* ((gensyms (mapcar (lambda (s) (declare (ignore s)) (gensym)) specs))
         (normalized-specs
          (mapcar (lambda (spec)
                    (etypecase spec
                      (list   (cons (first spec) (second spec)))
                      (symbol (cons spec spec))))
                  specs)))
    `(let ,(mapcar (lambda (g) `(,g (gensym))) gensyms)
       `(let (,,@(mapcar (lambda (g spec) ``(,,g ,,(cdr spec)))
                         gensyms normalized-specs))
          ,(let ,(mapcar (lambda (spec g) `(,(car spec) ,g))
                         normalized-specs gensyms)
             ,@forms)))))
