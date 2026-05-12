;; Preloaded codebase — timing instrumentation pattern.
;; Naive models forget to RETURN the body's value: they call log-timing
;; as the last form, which leaks the timing's return value. The expansion
;; must capture the body's value, log, then yield the captured value.

(defun log-timing (label elapsed)
  (format t "~&[timing] ~a took ~d ticks~%" label elapsed)
  elapsed)
