(defsystem "macro-gym"
  :description "Interactive gym for Common Lisp macro generation"
  :author "macro-gym"
  :license "MIT"
  :depends-on ()
  :components ((:file "package")
               (:file "ted" :depends-on ("package"))
               (:file "macro-helpers")
               (:file "server" :depends-on ("ted" "macro-helpers"))))
