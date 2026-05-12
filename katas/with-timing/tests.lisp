;; Test cases for with-timing macro
;; Format: ((input-form . expected-expansion) ...)
;; Pattern: capture start, run body, log elapsed, return body's value.

(
 ((with-timing "compute" (+ 1 2))
  . (let ((start (get-internal-real-time)))
      (let ((result (progn (+ 1 2))))
        (log-timing "compute" (- (get-internal-real-time) start))
        result)))

 ((with-timing "fetch"
    (http-get "/api/users")
    (parse-response))
  . (let ((start (get-internal-real-time)))
      (let ((result (progn (http-get "/api/users") (parse-response))))
        (log-timing "fetch" (- (get-internal-real-time) start))
        result)))

 ((with-timing "noop" nil)
  . (let ((start (get-internal-real-time)))
      (let ((result (progn nil)))
        (log-timing "noop" (- (get-internal-real-time) start))
        result)))
)
