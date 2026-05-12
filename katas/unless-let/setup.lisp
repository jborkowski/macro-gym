;; Preloaded codebase — nothing to set up.
;; unless-let binds VAR to EXPR, then runs BODY only when VAR is nil.
;; Mirror of when-let. Naive models forget the binding is visible inside
;; the body (it must be — the body often needs to fall back to a default
;; that mentions the var, e.g. for logging that the lookup failed).

(defvar *fallback-counter* 0)
