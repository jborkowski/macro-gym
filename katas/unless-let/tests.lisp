;; Test cases for unless-let macro
;; Pattern: (unless-let (var expr) body...) -> bind var, run body if var is nil.

(
 ((unless-let (user (lookup-user 42))
    (log-missing 42))
  . (let ((user (lookup-user 42)))
      (unless user
        (log-missing 42))))

 ((unless-let (cached (gethash key *cache*))
    (incf *fallback-counter*)
    (compute-and-store key))
  . (let ((cached (gethash key *cache*)))
      (unless cached
        (incf *fallback-counter*)
        (compute-and-store key))))

 ((unless-let (x nil) :empty)
  . (let ((x nil))
      (unless x
        :empty)))
)
