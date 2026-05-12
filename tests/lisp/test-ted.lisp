;;; test-ted.lisp — Zhang-Shasha TED correctness + similarity scale.
;;;
;;; The reward signal that GRPO trainers will see depends on these
;;; numbers being trustworthy. Any bug here silently corrupts loss
;;; curves downstream. Tests target the four properties that matter:
;;;   1. metric identity:    TED(x, x) = 0
;;;   2. one-edit cost:      single label change / single insert = 1
;;;   3. similarity scale:   in [0,1], 1 iff identical, 0 iff max edits
;;;   4. overflow guard:     *max-ted-nodes* short-circuits to NIL

(in-package :cl-user)
(defpackage :macro-gym/test-ted
  (:use :cl :parachute :macro-gym))
(in-package :macro-gym/test-ted)

(define-test ted)

(define-test (ted identity-atom)
  (is = 0 (sexp-ted 'a 'a))
  (is = 1.0 (sexp-similarity 'a 'a)))

(define-test (ted identity-tree)
  (let ((f '(let ((x 1)) (+ x 2))))
    (is = 0 (sexp-ted f f))
    (is = 1.0 (sexp-similarity f f))))

(define-test (ted single-leaf-relabel)
  "Swap one atom for another → distance 1."
  (is = 1 (sexp-ted 'a 'b))
  (is = 1 (sexp-ted '(let ((x 1)) (+ x 2)) '(let ((x 1)) (+ x 3)))))

(define-test (ted single-insertion)
  "Adding one atom to an otherwise identical tree → distance 1.
Note: comparing (a) to () is NOT a single insertion — it's two edits
(delete the :CONS node, relabel the remaining leaf to :NIL), because
under the label scheme an empty list is a single :NIL leaf while (a)
is a :CONS with one A child."
  (is = 1 (sexp-ted '(a b) '(a b c)))
  (is = 1 (sexp-ted '(a) '(a b))))

(define-test (ted operator-swap)
  "Swapping `let` for `let*` is one relabel (the operator atom)."
  (is = 1 (sexp-ted '(let ((x 1)) x) '(let* ((x 1)) x))))

(define-test (ted package-agnostic-labels)
  "Symbols from different packages but with the same name should be
treated as equal labels (design F8): we label by symbol-name."
  (let ((sym-in-kw (intern "FOO" :keyword))
        (sym-in-cl (intern "FOO" :cl-user)))
    ;; Different home packages, identical name → label-equal.
    (is = 0 (sexp-ted (list sym-in-kw) (list sym-in-cl))
        "package-agnostic equality on symbol-name labels")))

(define-test (ted similarity-bounded)
  "Similarity must always be in [0, 1]."
  (let ((s1 (sexp-similarity '(a b c d e) '(z y x w v)))
        (s2 (sexp-similarity '(a (b (c (d))))
                              '(z (y (x (w)))))))
    (true (and (<= 0.0 s1 1.0) (<= 0.0 s2 1.0))
          "similarity out of bounds: s1=~a s2=~a" s1 s2)))

(define-test (ted similarity-near-identical)
  "One symbol wrong in an ~20-node tree → sim > 0.85."
  (let* ((ref '(progn (log-enter ctx) (let ((r (progn body)))
                                        (log-leave ctx r) r)))
         (pred '(progn (log-enter ctx) (let ((r (progn body)))
                                         (log-leaves ctx r) r))) ; log-leaves
         (s (sexp-similarity pred ref)))
    (true (> s 0.85)
          "near-identical structure must score > 0.85, got ~a" s)))

(define-test (ted similarity-wildly-different)
  "Trees with no structural overlap → sim well below 1.0."
  (let ((s (sexp-similarity '(a b c) '((x ((y))) (z) ((w)) ((v))))))
    (true (< s 0.5)
          "completely different shapes must score < 0.5, got ~a" s)))

(define-test (ted overflow-returns-nil)
  "When *MAX-TED-NODES* is small, oversize trees return NIL."
  (let ((macro-gym:*max-ted-nodes* 5))
    (is eq nil (sexp-similarity '(a (b (c (d (e f))))) '(a b c d e f g h i))
        "oversize tree must short-circuit to NIL")
    (is eq nil (sexp-ted '(1 2 3 4 5 6 7) '(1 2 3 4 5 6 7))
        "oversize tree must short-circuit to NIL for sexp-ted too")))

(define-test (ted dotted-pair)
  "Dotted pairs are handled by treating the dotted tail as a final
child. (A . B) and (A B) differ but both produce valid trees."
  (is = 0 (sexp-ted '(a . b) '(a . b)))
  ;; (a . b) has cons + a + b = 3 nodes. (a b) has cons + a + b = 3 nodes.
  ;; The shapes are tree-identical under our label scheme, so distance 0.
  ;; This is intentional: we don't try to model the dot itself.
  (is = 0 (sexp-ted '(a . b) '(a b))))

(define-test (ted normalize-then-ted)
  "End-to-end: normalize-variables + sexp-similarity should give 1.0 on
two forms that differ only in gensym names."
  (let* ((g1 (gensym "R"))
         (g2 (gensym "R"))
         (a (macro-gym::normalize-variables `(let ((,g1 1)) ,g1)))
         (b (macro-gym::normalize-variables `(let ((,g2 1)) ,g2))))
    (is = 1.0 (sexp-similarity a b)
        "post-normalisation, gensym-different forms must score 1.0")))
