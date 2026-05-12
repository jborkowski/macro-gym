;; Preloaded codebase — debug guard.
;; The simplest kata: a body should only run when the dynamic flag
;; *debug* is non-nil. Trap: models that wrap each body form in its
;; own when expression instead of putting the whole body inside one
;; `when` lose laziness (well, not laziness — but they produce noisier,
;; nonequivalent expansions).

(defvar *debug* nil)
