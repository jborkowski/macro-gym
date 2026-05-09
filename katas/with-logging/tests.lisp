;; Test cases for with-logging macro
;; Format: ((input-form . expected-expansion) ...)
;; Use regular symbols for introduced variables — they get normalized automatically.

(
 ((with-logging "process-step" (+ 1 2))
  . (progn
      (log-enter "process-step")
      (let ((result (progn (+ 1 2))))
        (log-leave "process-step" result)
        result)))

 ((with-logging "http-fetch"
    (fetch-from-url "https://api.example.com/data"))
  . (progn
      (log-enter "http-fetch")
      (let ((result (progn (fetch-from-url "https://api.example.com/data"))))
        (log-leave "http-fetch" result)
        result)))

 ((with-logging "multi-step"
    (let ((x 10))
      (* x x)))
  . (progn
      (log-enter "multi-step")
      (let ((result (progn (let ((x 10)) (* x x)))))
        (log-leave "multi-step" result)
        result)))
)
