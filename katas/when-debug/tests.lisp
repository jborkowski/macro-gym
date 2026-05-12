;; Test cases for when-debug macro
;; Pattern: (when-debug body...) -> (when *debug* body...)
;; The grader expands recursively to fixpoint, so `when` further expands
;; to `(if X (progn body...))` — the expected expansions reflect that.

(
 ((when-debug (format t "trace: ~a~%" x))
  . (if *debug*
        (format t "trace: ~a~%" x)))

 ((when-debug
    (log-state)
    (dump-heap)
    (sanity-check))
  . (if *debug*
        (progn (log-state) (dump-heap) (sanity-check))))

 ((when-debug)
  . (if *debug*
        nil))
)
