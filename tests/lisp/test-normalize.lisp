;;; test-normalize.lisp — normalize-variables correctness.
;;;
;;; Variable normalization is the deterministic-comparison oracle: it
;;; collapses gensyms and let-binding variables into canonical :V1, :V2,
;;; ... so structural correctness is what's measured, not symbol naming.
;;; Edge cases here are the difference between "model gets reward 1.0"
;;; and "model gets reward 0.0 because it picked a different gensym
;;; name." Bugs in this function become silent reward-signal corruption.

(in-package :cl-user)
(defpackage :macro-gym/test-normalize
  (:use :cl :parachute :macro-gym))
(in-package :macro-gym/test-normalize)

(define-test normalize)

(define-test (normalize gensym-collapse)
  "Two distinct gensyms with same role collapse to same :Vn."
  (let* ((g1 (gensym "R"))
         (g2 (gensym "R"))
         (form `(let ((,g1 1)) (let ((,g2 2)) (+ ,g1 ,g2))))
         (n (macro-gym::normalize-variables form)))
    ;; Both gensyms become some :V1, :V2 — assert distinct (different roles)
    ;; but BOTH normalized form matches itself across renames.
    (let* ((g3 (gensym "R"))
           (g4 (gensym "R"))
           (form2 `(let ((,g3 1)) (let ((,g4 2)) (+ ,g3 ,g4))))
           (n2 (macro-gym::normalize-variables form2)))
      (is equal n n2
          "Two structurally-identical forms with different gensyms must normalize to the same shape."))))

(define-test (normalize let-binding-captured)
  "let-bound names are normalized, not just gensyms — so a model picking
   'tmp' vs 'r' for a let binding doesn't change the reward."
  (let ((a (macro-gym::normalize-variables '(let ((tmp 1)) tmp)))
        (b (macro-gym::normalize-variables '(let ((r 1)) r))))
    (is equal a b
        "let-bound 'tmp' and 'r' must normalize identically.")))

(define-test (normalize handler-case-binding-captured)
  "handler-case condition variable is normalized."
  (let ((a (macro-gym::normalize-variables
            '(handler-case (foo) (error (c) (log c)))))
        (b (macro-gym::normalize-variables
            '(handler-case (foo) (error (err) (log err))))))
    (is equal a b
        "handler-case binding var must normalize across renames.")))

(define-test (normalize keywords-preserved)
  "Keywords are NOT touched — they're public protocol values."
  (let ((n (macro-gym::normalize-variables '(:foo :bar :baz))))
    (is equal n '(:foo :bar :baz)
        "Keywords must pass through normalize-variables unchanged.")))

(define-test (normalize free-symbols-preserved)
  "Free symbols (function calls, package-bound names) are NOT touched.
   Only gensyms and lexical-binding vars get renamed."
  (let ((n (macro-gym::normalize-variables '(+ 1 2))))
    (is equal n '(+ 1 2)
        "Function call `+` must NOT be renamed — it's a free symbol.")))

(define-test (normalize reader-duplicated-uninterned)
  "v3 fix: when SBCL's reader parses literal `#:NAME` `#:NAME` tokens in
   the same form, it produces SEPARATE uninterned-symbol objects with the
   same print name. Pre-v3 these got DIFFERENT :V indices (EQ-keyed map);
   v3 keys by NAME so reader-duplicated tokens collapse to ONE :V slot —
   matching what macroexpand-1 produces when a defmacro binds a gensym
   once and references it multiple times."
  (let* ((expected-from-reader
          ;; Simulate reader behavior: two distinct uninterned symbols
          ;; with identical print name "G1".
          (let ((sym-a (make-symbol "G1"))
                (sym-b (make-symbol "G1")))
            `(let ((,sym-a "x") (f ,sym-a))
               (when (probe ,sym-b) (use ,sym-b)))))
         (actual-from-expand
          ;; Simulate macroexpand: ONE gensym, four EQ-shared references.
          (let ((g (make-symbol "G1")))
            `(let ((,g "x") (f ,g))
               (when (probe ,g) (use ,g)))))
         (n-expected (macro-gym::normalize-variables expected-from-reader))
         (n-actual   (macro-gym::normalize-variables actual-from-expand)))
    (is equal n-expected n-actual
        "Reader-duplicated `#:G1` tokens must collapse to the same :V slot
         as a macroexpand-1-shared single gensym referenced multiple times.")))
