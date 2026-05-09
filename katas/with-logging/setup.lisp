;; Preloaded codebase — the "pattern" that exists in the codebase
;; Agent observes this pattern repeating and should abstract it into a macro

(defvar *log-buffer* nil)
(defvar *log-depth* 0)

(defun log-enter (ctx)
  (push (format nil "~a> ENTER ~a" (make-string *log-depth* :initial-element #\Space) ctx)
        *log-buffer*)
  (incf *log-depth* 2))

(defun log-leave (ctx val)
  (decf *log-depth* 2)
  (push (format nil "~a< LEAVE ~a => ~a" (make-string *log-depth* :initial-element #\Space) ctx val)
        *log-buffer*)
  val)
