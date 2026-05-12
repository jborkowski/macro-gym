;;; package.lisp — defpackage for :macro-gym. Loaded first so ted.lisp
;;; and server.lisp can both `(in-package :macro-gym)`.

(defpackage :macro-gym
  (:use :cl)
  (:export :main
           :safe-read-macro
           :normalize-variables
           :evaluate-macro
           :cached-load-kata
           :respond-to-stream
           :macro-gym-timeout
           :*kata-cache*
           :*max-macroexpand-depth*
           ;; TED public surface
           :sexp-ted
           :sexp-similarity
           :build-ted-tree
           :*max-ted-nodes*
           :*ted-formula-version*))
