;; Preloaded codebase — lock acquire/release pattern.
;; Trap: release MUST be in the cleanup of unwind-protect, not after
;; the body. Otherwise an error inside the critical section leaves the
;; lock held forever. Same shape as with-resource but with a fixed
;; (acquire ...) / (release ...) call instead of a configurable
;; release function.

(defun acquire (lock) (declare (ignore lock)) nil)
(defun release (lock) (declare (ignore lock)) nil)
