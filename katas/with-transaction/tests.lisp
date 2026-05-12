;; Test cases for with-transaction macro
;; Pattern: begin, run body, commit on success, rollback + resignal on error.

(
 ((with-transaction (insert-user "alice"))
  . (progn
      (begin-tx)
      (handler-case
          (let ((result (progn (insert-user "alice"))))
            (commit-tx)
            result)
        (error (e)
          (rollback-tx)
          (error e)))))

 ((with-transaction
    (insert-order 42)
    (update-inventory 42)
    (notify-shipper 42))
  . (progn
      (begin-tx)
      (handler-case
          (let ((result (progn (insert-order 42)
                               (update-inventory 42)
                               (notify-shipper 42))))
            (commit-tx)
            result)
        (error (e)
          (rollback-tx)
          (error e)))))

 ((with-transaction 0)
  . (progn
      (begin-tx)
      (handler-case
          (let ((result (progn 0)))
            (commit-tx)
            result)
        (error (e)
          (rollback-tx)
          (error e)))))
)
