;; Preloaded codebase — retry pattern
;; Agent sees handler-case with retry logic repeated, should make with-retry macro

(defun random-backoff ()
  (* 0.1 (1+ (random 10))))

(defvar *max-retries* 3)
