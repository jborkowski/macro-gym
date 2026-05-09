;; Test cases for with-retry macro
;; Format: ((input-form . expected-expansion) ...)
;; Use regular symbols for introduced variables — they get normalized automatically.

(
 ((with-retry (http-get "/api/data"))
  . (let ((count 0))
      (handler-case (progn (http-get "/api/data"))
        (error (e)
          (incf count)
          (if (> count *max-retries*)
              (error e)
              (progn
                (sleep (random-backoff))
                (progn (http-get "/api/data"))))))))

 ((with-retry (connect-db :host "localhost" :port 5432))
  . (let ((count 0))
      (handler-case (progn (connect-db :host "localhost" :port 5432))
        (error (e)
          (incf count)
          (if (> count *max-retries*)
              (error e)
              (progn
                (sleep (random-backoff))
                (progn (connect-db :host "localhost" :port 5432))))))))
)
