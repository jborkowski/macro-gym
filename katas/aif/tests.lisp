;; Test cases for aif macro
;; Pattern: (aif test then else) -> (let ((it test)) (if it then else))
;; The symbol IT is deliberately captured (anaphoric); body references it.

(
 ((aif (lookup-user 42)
       (greet it)
       (error "no such user"))
  . (let ((it (lookup-user 42)))
      (if it
          (greet it)
          (error "no such user"))))

 ((aif (assoc :name record)
       (cdr it)
       :unknown)
  . (let ((it (assoc :name record)))
      (if it
          (cdr it)
          :unknown)))

 ((aif nil :then :else)
  . (let ((it nil))
      (if it
          :then
          :else)))
)
