;; Test cases for with-resource macro
;; Pattern: (with-resource (var acquire-form) body...) ->
;;   bind var to acquire-form, run body protected, release in cleanup.

(
 ((with-resource (conn (open-connection "db.local"))
    (query conn "SELECT 1"))
  . (let ((conn (open-connection "db.local")))
      (unwind-protect
           (progn (query conn "SELECT 1"))
        (release-resource conn))))

 ((with-resource (f (open-file "/tmp/x.log" :direction :output))
    (write-line "hello" f)
    (write-line "world" f))
  . (let ((f (open-file "/tmp/x.log" :direction :output)))
      (unwind-protect
           (progn
             (write-line "hello" f)
             (write-line "world" f))
        (release-resource f))))

 ((with-resource (h (acquire-handle))
    nil)
  . (let ((h (acquire-handle)))
      (unwind-protect
           (progn nil)
        (release-resource h))))
)
