;;; ted.lisp — Zhang-Shasha tree edit distance on S-expressions.
;;;
;;; Wire contract (consumed by server.lisp):
;;;   (sexp-similarity pred ref) -> single-float in [0,1] or NIL on overflow.
;;;     1.0 = identical (label + structure). 0.0 = max edits over max tree size.
;;;     NIL = either tree exceeded *MAX-TED-NODES*.
;;;
;;; Label scheme (decided in design review):
;;;   atom       -> symbol-name string (for symbols, package-agnostic),
;;;                 the atom itself otherwise (numbers/strings/keywords)
;;;   cons cell  -> :CONS sentinel; children = list elements, dotted tail
;;;                 appended as a final child (rare in practice).
;;;
;;; This is run on already-normalised forms (post `normalize-variables`),
;;; so gensyms / lex-bound vars are :V1, :V2, … and compare by name.
;;;
;;; Cost model: insert=1, delete=1, relabel=(0 if labels EQUAL else 1).
;;; Variable indices (`:V1` vs `:V2`) cost 1 to relabel — see plan F3.

(in-package :macro-gym)

(defparameter *max-ted-nodes* 200
  "Hard cap on per-tree node count. Trees above this size short-circuit
to similarity NIL rather than running O(n^3-4) Zhang-Shasha.
Calibrated against the GRPO sanity dataset (cl-ds + creative-macros):
a 100-node cap dropped three real katas to NIL; 200 covers them with
~5-50 ms per grade in tight SBCL. Operators may shrink for safety if
training reports slow grades — there's no env-var hook yet (TODO).")

(defparameter *ted-formula-version* "ted-zs-v1"
  "Stable wire identifier for the TED formula. Trainers' loss curves
will encode this — bump on any algorithm or label-scheme change.")

;;; ============================================================
;;;   Node label / children (s-expression view)
;;; ============================================================

(declaim (inline node-label))
(defun node-label (form)
  "Symbol-name for symbols (package-agnostic per design F8), :CONS for
cons cells, the atom itself otherwise (numbers, strings, keywords)."
  (cond
    ((null form) :NIL)
    ((consp form) :CONS)
    ((symbolp form) (symbol-name form))
    (t form)))

;;; ============================================================
;;;   Postorder build + leftmost descendants
;;; ============================================================
;;;
;;; A TED-TREE is the flat post-order encoding of an S-expression form:
;;;   LABELS[i]    = label of the i-th node (in left-to-right postorder)
;;;   LEFTMOST[i]  = postorder index of i's leftmost leaf descendant
;;;                  (for a leaf, leftmost[i] = i)
;;;   KEYROOTS     = ascending list of "last node with each leftmost value"
;;;                  — the standard Zhang-Shasha keyroot set
;;;   SIZE         = number of nodes
;;;
;;; Build is depth-first; we early-exit when the running node count
;;; exceeds *MAX-TED-NODES* to defend against pathological expansions.

(defstruct ted-tree
  (labels   #() :type simple-vector)
  (leftmost #() :type (simple-array fixnum (*)))
  (keyroots nil :type list)
  (size     0   :type fixnum))

(defun %ted-walk (form labels-vec leftmost-vec)
  "Walk FORM in left-to-right postorder, push label+leftmost-idx into the
two adjustable vectors. Return the postorder index of the node we just
emitted, or -1 if we tripped *MAX-TED-NODES* (signaled to caller via the
returned value being checked against -1)."
  (declare (optimize (speed 3) (safety 1)))
  (labels ((leaf (form)
             (vector-push-extend (node-label form) labels-vec)
             (let ((idx (1- (length labels-vec))))
               (vector-push-extend idx leftmost-vec)
               idx))
           (over () (>= (length labels-vec) *max-ted-nodes*))
           (visit (form)
             (cond
               ((over) -1)
               ((atom form) (leaf form))
               (t
                ;; Internal :CONS node. Walk children in order.
                ;; my-leftmost is the postorder index of THIS node's leftmost
                ;; leaf descendant — that's leftmost-vec[first-child], NOT the
                ;; first child's own postorder index (which would be wrong
                ;; whenever the first child is itself internal).
                (let ((my-leftmost -1)
                      (first-child t))
                  (loop for tail = form then (cdr tail)
                        while (consp tail)
                        do (let ((ci (visit (car tail))))
                             (when (= ci -1) (return-from visit -1))
                             (when first-child
                               (setf my-leftmost (aref leftmost-vec ci))
                               (setf first-child nil)))
                        ;; dotted tail: append as final child
                        finally
                           (unless (null tail)
                             (let ((ci (visit tail)))
                               (when (= ci -1) (return-from visit -1))
                               (when first-child
                                 (setf my-leftmost (aref leftmost-vec ci))
                                 (setf first-child nil)))))
                  (when (over) (return-from visit -1))
                  (vector-push-extend :CONS labels-vec)
                  (let ((self-idx (1- (length labels-vec))))
                    (vector-push-extend
                     (if (= my-leftmost -1) self-idx my-leftmost)
                     leftmost-vec)
                    self-idx))))))
    (visit form)))

(defun build-ted-tree (form)
  "Build a TED-TREE for FORM, or return NIL on overflow."
  (let ((labels (make-array 16 :adjustable t :fill-pointer 0))
        (leftmost (make-array 16 :adjustable t :fill-pointer 0 :element-type 'fixnum)))
    (let ((root (%ted-walk form labels leftmost)))
      (when (or (= root -1) (>= (length labels) *max-ted-nodes*))
        (return-from build-ted-tree nil))
      (let* ((n (length labels))
             ;; keyroots: largest postorder index i for each distinct leftmost[i].
             (last-lm (make-hash-table :test 'eql)))
        (dotimes (i n)
          (setf (gethash (aref leftmost i) last-lm) i))
        (let ((kr (sort (loop for v being the hash-values of last-lm
                              collect v)
                        #'<)))
          (make-ted-tree
           :labels (coerce labels 'simple-vector)
           :leftmost (let ((vec (make-array n :element-type 'fixnum)))
                       (dotimes (i n vec)
                         (setf (aref vec i) (aref leftmost i))))
           :keyroots kr
           :size n))))))

;;; ============================================================
;;;   Zhang-Shasha core
;;; ============================================================

(defun %label-cost (a b)
  (declare (optimize (speed 3) (safety 1)))
  (if (equal a b) 0 1))

(defun %forest-distance (t1 t2 ki kj td)
  "Compute forest distances between subtrees rooted at keyroots
KI (in T1) and KJ (in T2). Fills both the local forest table FD and
the persistent tree-distance table TD for any tree-aligned (i1, j1)
pair encountered along the way."
  (declare (optimize (speed 3) (safety 1))
           (type fixnum ki kj)
           (type (simple-array fixnum (* *)) td))
  (let* ((lm1 (aref (ted-tree-leftmost t1) ki))
         (lm2 (aref (ted-tree-leftmost t2) kj))
         (rows (the fixnum (+ 2 (- ki lm1))))   ; size+1 of T1 forest prefix
         (cols (the fixnum (+ 2 (- kj lm2))))
         (fd (make-array (list rows cols) :element-type 'fixnum
                                          :initial-element 0))
         (lm1v (ted-tree-leftmost t1))
         (lm2v (ted-tree-leftmost t2))
         (lab1 (ted-tree-labels t1))
         (lab2 (ted-tree-labels t2)))
    (declare (type (simple-array fixnum (*)) lm1v lm2v)
             (type simple-vector lab1 lab2)
             (type (simple-array fixnum (* *)) fd))
    (loop for x of-type fixnum from 1 below rows do
      (setf (aref fd x 0) (1+ (aref fd (1- x) 0))))
    (loop for y of-type fixnum from 1 below cols do
      (setf (aref fd 0 y) (1+ (aref fd 0 (1- y)))))
    (loop for x of-type fixnum from 1 below rows do
      (loop for y of-type fixnum from 1 below cols do
        (let* ((i1 (the fixnum (+ lm1 x -1)))
               (j1 (the fixnum (+ lm2 y -1)))
               (li1 (aref lm1v i1))
               (lj1 (aref lm2v j1))
               (del (1+ (aref fd (1- x) y)))
               (ins (1+ (aref fd x (1- y)))))
          (if (and (= li1 lm1) (= lj1 lm2))
              ;; Tree-aligned: i1 and j1 each anchor their full subtree.
              (let* ((cost (%label-cost (aref lab1 i1) (aref lab2 j1)))
                     (ren (+ (aref fd (1- x) (1- y)) cost))
                     (v (min del ins ren)))
                (setf (aref fd x y) v)
                (setf (aref td i1 j1) v))
              ;; Forest-only: refer to stored tree distance for (i1, j1).
              (let* ((a (the fixnum (- li1 lm1)))
                     (b (the fixnum (- lj1 lm2)))
                     (sub (+ (aref fd a b) (aref td i1 j1)))
                     (v (min del ins sub)))
                (setf (aref fd x y) v))))))
    nil))

(defun ted (t1 t2)
  "Zhang-Shasha tree edit distance between TED-TREE structures T1 and T2.
Returns a non-negative fixnum, or NIL if either tree is missing
(overflowed *MAX-TED-NODES* at build time)."
  (when (or (null t1) (null t2)) (return-from ted nil))
  (let* ((n1 (ted-tree-size t1))
         (n2 (ted-tree-size t2))
         (td (make-array (list n1 n2) :element-type 'fixnum
                                      :initial-element 0)))
    (declare (type (simple-array fixnum (* *)) td))
    (dolist (i (ted-tree-keyroots t1))
      (dolist (j (ted-tree-keyroots t2))
        (%forest-distance t1 t2 i j td)))
    (aref td (1- n1) (1- n2))))

;;; ============================================================
;;;   Public entry point
;;; ============================================================

(defun sexp-ted (a b)
  "Tree edit distance between two S-expressions A and B. Returns a
non-negative integer or NIL if either tree exceeds *MAX-TED-NODES*."
  (let ((ta (build-ted-tree a))
        (tb (build-ted-tree b)))
    (ted ta tb)))

(defun sexp-similarity (a b)
  "Normalised similarity in [0,1]: 1.0 = identical labels and structure;
0.0 = max-distance / max-tree-size. NIL if either tree overflows the
*MAX-TED-NODES* cap. Normalisation uses max(|A|, |B|), which is the
tight upper bound on TED for unit edit costs on rooted ordered trees
(F4 from the eng review)."
  (let ((ta (build-ted-tree a))
        (tb (build-ted-tree b)))
    (when (or (null ta) (null tb))
      (return-from sexp-similarity nil))
    (let ((d (ted ta tb))
          (m (max (ted-tree-size ta) (ted-tree-size tb))))
      (when (or (null d) (zerop m))
        (return-from sexp-similarity (if (and (eql d 0) (zerop m)) 1.0 nil)))
      (max 0.0 (coerce (- 1.0 (/ d (float m))) 'single-float)))))

;; Symbol exports live in lisp/package.lisp.
