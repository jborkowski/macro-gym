;; Preloaded codebase — memoized defun.
;; The trick: the hash table must be created ONCE, at definition time,
;; not on every call. So the expansion is a let that wraps the defun,
;; capturing the table in the function's closure. Models that put the
;; make-hash-table inside the defun body create a fresh table per call
;; (which is just an expensive identity function).

(defvar *memo-stats* (make-hash-table :test #'eq))
