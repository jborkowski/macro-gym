;; Test cases for with-mutex-held macro
;; Pattern: (with-mutex-held (lock) body...) ->
;;   acquire, body in unwind-protect, release in cleanup.

(
 ((with-mutex-held (*db-lock*)
    (write-row row))
  . (progn
      (acquire *db-lock*)
      (unwind-protect
           (progn (write-row row))
        (release *db-lock*))))

 ((with-mutex-held (my-lock)
    (incf *counter*)
    (push :event *log*)
    *counter*)
  . (progn
      (acquire my-lock)
      (unwind-protect
           (progn
             (incf *counter*)
             (push :event *log*)
             *counter*)
        (release my-lock))))

 ((with-mutex-held ((slot-value obj 'lock))
    (mutate obj))
  . (progn
      (acquire (slot-value obj 'lock))
      (unwind-protect
           (progn (mutate obj))
        (release (slot-value obj 'lock)))))
)
