;; Preloaded codebase — generalized RAII pattern.
;; The trap: cleanup MUST run even if body unwinds via non-local exit
;; (error, throw, return-from). unwind-protect is mandatory; a plain
;; progn with release-resource at the end leaks on failure paths.
;; Models that put release inside the protected form (instead of the
;; cleanup form) miss the entire point of unwind-protect.

(defun release-resource (r)
  (declare (ignore r))
  nil)
