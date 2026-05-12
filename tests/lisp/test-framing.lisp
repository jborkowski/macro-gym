;;; test-framing.lisp — length-prefixed IPC framing roundtrip.
;;;
;;; The framing protocol is:
;;;   <decimal-byte-count>\n<UTF-8 payload>
;;; Length prefix is byte count of the UTF-8-encoded payload, NOT char
;;; count. Bug class this defends against: readline()-based decoders
;;; desyncing when payload contains embedded newlines (a defmacro
;;; expanding to a multi-line error string would do this).

(in-package :cl-user)
(defpackage :macro-gym/test-framing
  (:use :cl :parachute :macro-gym))
(in-package :macro-gym/test-framing)

(define-test framing)

(defun roundtrip (form)
  "Encode FORM using the grader's framing protocol, then decode and
   return the parsed result. Asserts the encode/decode is lossless."
  (let* ((encoded (with-output-to-string (s)
                    (macro-gym::respond-to-stream s form)))
         ;; Parse <bytecount>\n<payload>\n format
         (newline-pos (position #\Newline encoded))
         (declared-bytes (parse-integer (subseq encoded 0 newline-pos)))
         (payload (subseq encoded (1+ newline-pos)))
         ;; Strip trailing newline if present (encoder convention)
         (payload-no-trail (if (and (plusp (length payload))
                                    (char= (char payload (1- (length payload))) #\Newline))
                               (subseq payload 0 (1- (length payload)))
                               payload))
         (payload-bytes (sb-ext:string-to-octets payload-no-trail :external-format :utf-8)))
    (is = declared-bytes (length payload-bytes)
        "Declared byte count must match actual UTF-8 byte length of payload.")
    (with-input-from-string (s payload-no-trail)
      (let ((*read-eval* nil))
        (read s)))))

(define-test (framing ascii-plist)
  "Trivial happy path: ASCII plist."
  (let ((decoded (roundtrip '(:reward 1.0 :passed 3 :total 3))))
    (is equal decoded '(:reward 1.0 :passed 3 :total 3))))

(define-test (framing embedded-newline-in-error)
  "Defmacro error containing newline characters must roundtrip cleanly.
   This is the case that broke the old readline-based protocol."
  (let* ((multiline-error "ERROR: line 1
line 2
line 3")
         (form `(:reward -0.1 :error ,multiline-error))
         (decoded (roundtrip form)))
    (is equal (getf decoded :error) multiline-error
        "Multi-line error must survive framing roundtrip.")))

(define-test (framing non-ascii-condition-message)
  "Lisp conditions can carry non-ASCII text. Byte count must match UTF-8."
  (let* ((unicode-msg "función inválida — ‘λ’ not bound")
         (form `(:error ,unicode-msg))
         (decoded (roundtrip form)))
    (is equal (getf decoded :error) unicode-msg
        "Non-ASCII text must survive UTF-8 framing roundtrip.")))

(define-test (framing very-large-payload)
  "1 MB payload — assert framing handles large messages."
  (let* ((big-string (make-string (* 1024 1024) :initial-element #\x))
         (form `(:big ,big-string))
         (decoded (roundtrip form)))
    (is = (length (getf decoded :big)) (length big-string)
        "1 MB payload must roundtrip with no truncation.")))

(define-test (framing empty-payload)
  "Edge case: empty list."
  (let ((decoded (roundtrip nil)))
    (is equal decoded nil)))
