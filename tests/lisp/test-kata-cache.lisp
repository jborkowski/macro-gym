;;; test-kata-cache.lisp — kata cache + package isolation tests.
;;;
;;; Properties we care about:
;;;   - Cache hit on unchanged files (returns identical struct)
;;;   - Cache invalidation when setup.lisp or tests.lisp mtime advances
;;;   - Each kata gets its own package (no symbol leak across katas)
;;;   - setup.lisp forms re-read inside the kata package, so new symbols
;;;     intern there and don't pollute :cl-user

(in-package :cl-user)
(defpackage :macro-gym/test-kata-cache
  (:use :cl :parachute :macro-gym))
(in-package :macro-gym/test-kata-cache)

(define-test kata-cache)

;;; ---- Helpers ---------------------------------------------------------

(defparameter *tmp-root*
  (merge-pathnames
   (format nil "macro-gym-kata-cache-test-~a/" (random (expt 2 32)))
   (or (uiop:getenv "TMPDIR" )
       #+darwin "/tmp/"
       #-darwin "/tmp/")))

;; Fallback when UIOP is absent in the SBCL invocation.
(defun env-or (var default)
  (or #+sbcl (sb-ext:posix-getenv var)
      default))

(defun tmp-root ()
  (let ((base (env-or "TMPDIR" "/tmp/")))
    (merge-pathnames
     (format nil "macro-gym-kata-cache-test-~a/" (random (expt 2 32)))
     (pathname (if (char= (char base (1- (length base))) #\/)
                   base
                   (concatenate 'string base "/"))))))

(defun write-file (path contents)
  (ensure-directories-exist path)
  (with-open-file (s path :direction :output
                          :if-exists :supersede
                          :if-does-not-exist :create)
    (write-string contents s)))

(defmacro with-temp-kata-root ((root-var) &body body)
  "Bind macro-gym::*kata-root* to a fresh empty temp dir; clean up after."
  `(let* ((,root-var (tmp-root))
          (macro-gym::*kata-root* (namestring ,root-var)))
     (unwind-protect
          (progn
            (ensure-directories-exist ,root-var)
            ,@body)
       (ignore-errors
         #+sbcl (sb-ext:delete-directory ,root-var :recursive t)))))

(defun make-test-kata (root id &key setup tests)
  "Write setup.lisp + tests.lisp under ROOT/<id>/. Returns the directory."
  (let* ((dir (merge-pathnames (format nil "~a/" id) root))
         (setup-path (merge-pathnames "setup.lisp" dir))
         (tests-path (merge-pathnames "tests.lisp" dir)))
    (write-file setup-path setup)
    (write-file tests-path tests)
    dir))

(defun bump-mtime (path)
  "Advance the file-write-date of PATH to at least (now+2)s. Some
filesystems have 1-second mtime granularity; this guarantees a
detectable change."
  (sleep 1.1)
  (with-open-file (s path :direction :output
                          :if-exists :append
                          :if-does-not-exist :error)
    (terpri s)))

(defun clear-cache ()
  (clrhash macro-gym:*kata-cache*))

(defun cleanup-test-package (id)
  (let ((pkg (find-package (format nil "KATA-~a" (string-upcase id)))))
    (when pkg (ignore-errors (delete-package pkg)))))

;;; ---- Cache hit -------------------------------------------------------

(define-test (kata-cache cache-hit-returns-same-struct)
  "Second cached-load-kata call with no file change returns EQ struct."
  (clear-cache)
  (with-temp-kata-root (root)
    (make-test-kata root "cache-hit"
                    :setup "(defvar *cache-hit-var* 1)"
                    :tests "(((dummy) . (dummy)))")
    (unwind-protect
         (let ((a (macro-gym:cached-load-kata "cache-hit"))
               (b (macro-gym:cached-load-kata "cache-hit")))
           (is eq a b "Second load on unchanged files must return EQ struct."))
      (cleanup-test-package "cache-hit"))))

;;; ---- Invalidation on tests.lisp mtime change -------------------------

(define-test (kata-cache invalidates-on-tests-mtime)
  "Touching tests.lisp must invalidate the cache: next load returns a
fresh struct (not eq)."
  (clear-cache)
  (with-temp-kata-root (root)
    (let* ((dir (make-test-kata root "mtime-tests"
                                :setup "(defvar *mtime-tests-var* 1)"
                                :tests "(((dummy) . (dummy)))"))
           (tests-path (merge-pathnames "tests.lisp" dir)))
      (unwind-protect
           (let ((a (macro-gym:cached-load-kata "mtime-tests")))
             (bump-mtime tests-path)
             (let ((b (macro-gym:cached-load-kata "mtime-tests")))
               (false (eq a b)
                      "Cache must invalidate when tests.lisp mtime advances.")))
        (cleanup-test-package "mtime-tests")))))

(define-test (kata-cache invalidates-on-setup-mtime)
  "Same property, but for setup.lisp."
  (clear-cache)
  (with-temp-kata-root (root)
    (let* ((dir (make-test-kata root "mtime-setup"
                                :setup "(defvar *mtime-setup-var* 1)"
                                :tests "(((dummy) . (dummy)))"))
           (setup-path (merge-pathnames "setup.lisp" dir)))
      (unwind-protect
           (let ((a (macro-gym:cached-load-kata "mtime-setup")))
             (bump-mtime setup-path)
             (let ((b (macro-gym:cached-load-kata "mtime-setup")))
               (false (eq a b)
                      "Cache must invalidate when setup.lisp mtime advances.")))
        (cleanup-test-package "mtime-setup")))))

;;; ---- Per-kata package isolation --------------------------------------

(define-test (kata-cache per-kata-distinct-packages)
  "Kata A and kata B get distinct packages; neither sees the other's symbols."
  (clear-cache)
  (with-temp-kata-root (root)
    (make-test-kata root "iso-a"
                    :setup "(defvar *iso-a-leak* 'a-only)"
                    :tests "(((dummy) . (dummy)))")
    (make-test-kata root "iso-b"
                    :setup "(defvar *iso-b-leak* 'b-only)"
                    :tests "(((dummy) . (dummy)))")
    (unwind-protect
         (let* ((ka (macro-gym:cached-load-kata "iso-a"))
                (kb (macro-gym:cached-load-kata "iso-b"))
                (pa (macro-gym::kata-package ka))
                (pb (macro-gym::kata-package kb)))
           (false (eq pa pb) "Distinct packages per kata.")
           (true (find-symbol "*ISO-A-LEAK*" pa)
                 "A's symbol interned in A's package.")
           (false (find-symbol "*ISO-B-LEAK*" pa)
                  "B's symbol must NOT appear in A's package.")
           (true (find-symbol "*ISO-B-LEAK*" pb)
                 "B's symbol interned in B's package.")
           (false (find-symbol "*ISO-A-LEAK*" pb)
                  "A's symbol must NOT appear in B's package."))
      (cleanup-test-package "iso-a")
      (cleanup-test-package "iso-b"))))

;;; ---- Setup interns into kata package, not :cl-user -------------------

(define-test (kata-cache setup-interns-in-kata-package)
  "A defvar in setup.lisp must intern its symbol in the kata package,
NOT in :cl-user. Otherwise kata authors silently pollute the host
image and bugs become order-dependent across grade calls."
  (clear-cache)
  (with-temp-kata-root (root)
    (make-test-kata root "intern-check"
                    :setup "(defvar *kata-intern-marker* 42)"
                    :tests "(((dummy) . (dummy)))")
    ;; Pre-condition: marker is NOT already in :cl-user.
    (let ((pre (find-symbol "*KATA-INTERN-MARKER*" :cl-user)))
      (when pre (unintern pre :cl-user)))
    (unwind-protect
         (let* ((k (macro-gym:cached-load-kata "intern-check"))
                (pkg (macro-gym::kata-package k)))
           (true (find-symbol "*KATA-INTERN-MARKER*" pkg)
                 "Symbol must intern in the kata package.")
           (false (find-symbol "*KATA-INTERN-MARKER*" :cl-user)
                  "Symbol must NOT leak into :cl-user."))
      (cleanup-test-package "intern-check"))))
