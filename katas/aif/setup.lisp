;; Preloaded codebase — anaphoric if (Paul Graham, On Lisp).
;; KEY HYGIENE NOTE: `it` is INTENTIONALLY captured. Models that
;; gensym `it` defeat the entire point of the macro — the body
;; relies on the symbol IT being visible. So unlike most macros,
;; here you must NOT use gensym for the introduced binding.
;; This is one of the few legitimate uses of intentional capture.

(defvar *aif-noise* nil)
