;; Test cases for defmemo macro
;; Pattern: (defmemo name (args) body...) ->
;;   let captures a hash-table; defun looks up args, computes on miss.
;; The args are gathered into a list once (extra let binding) so the
;; key is built once per call, not twice.

(
 ((defmemo fib (n)
    (if (< n 2) n (+ (fib (- n 1)) (fib (- n 2)))))
  . (let ((cache (make-hash-table :test (quote equal))))
      (defun fib (n)
        (let ((key (list n)))
          (multiple-value-bind (hit found) (gethash key cache)
            (if found
                hit
                (setf (gethash key cache)
                      (progn (if (< n 2) n (+ (fib (- n 1)) (fib (- n 2))))))))))))

 ((defmemo ackermann (m n)
    (cond ((zerop m) (1+ n))
          ((zerop n) (ackermann (1- m) 1))
          (t (ackermann (1- m) (ackermann m (1- n))))))
  . (let ((cache (make-hash-table :test (quote equal))))
      (defun ackermann (m n)
        (let ((key (list m n)))
          (multiple-value-bind (hit found) (gethash key cache)
            (if found
                hit
                (setf (gethash key cache)
                      (progn (cond ((zerop m) (1+ n))
                                   ((zerop n) (ackermann (1- m) 1))
                                   (t (ackermann (1- m) (ackermann m (1- n)))))))))))))

 ((defmemo const () 42)
  . (let ((cache (make-hash-table :test (quote equal))))
      (defun const ()
        (let ((key (list)))
          (multiple-value-bind (hit found) (gethash key cache)
            (if found
                hit
                (setf (gethash key cache)
                      (progn 42))))))))
)
