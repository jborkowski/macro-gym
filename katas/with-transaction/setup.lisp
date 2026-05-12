;; Preloaded codebase — database transaction pattern.
;; The trap: on error, the macro must rollback AND re-signal the original
;; condition. Models that swallow the condition (returning nil) corrupt
;; callers; models that rollback after the error-signaling path is bypassed
;; leak transactions. handler-case with explicit resignal is the cleanest
;; shape.

(defun begin-tx () nil)
(defun commit-tx () nil)
(defun rollback-tx () nil)
