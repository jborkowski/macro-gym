"""Minimal s-expression parser for SBCL communication.

Handles the subset we need: keywords, strings, numbers, nil, lists.
"""

import re
from io import StringIO
from typing import Any

# Tokenizer notes:
# - Parens are a separate token; fallback symbol stops at whitespace OR parens
#   (was \S+ which greedily ate ")" — `t)` parsed as one symbol, breaking lists).
# - Bare `t` and `nil` are recognized as boolean literals (Lisp truth values),
#   but only when followed by whitespace, paren, or end-of-string — so symbols
#   like `tmp` or `nil-foo` don't trigger them.
_SEXP_RE = re.compile(r'''\s*(?:
    ("(?:[^"\\]|\\.)*")|       # 1 string
    (:[a-zA-Z0-9_\-*?!+/]+)|   # 2 keyword (allow common CL keyword chars)
    (-?\d+\.\d+)|              # 3 float
    (-?\d+)(?![\w.])|          # 4 integer (not followed by word char or dot)
    \bnil\b|                   # 5 nil (whole word)
    \bt\b|                     # 6 t   (whole word)
    ([()])|                    # 7 parens
    ([^\s()]+)                 # 8 fallback symbol (stop at parens too)
)''', re.VERBOSE)


def _tokenize(s: str) -> list[str]:
    tokens = []
    for m in _SEXP_RE.finditer(s):
        if m.group(1):  # string
            tokens.append(m.group(1))
        elif m.group(2):  # keyword
            tokens.append(m.group(2))
        elif m.group(3):  # float
            tokens.append(float(m.group(3)))
        elif m.group(4):  # integer
            tokens.append(int(m.group(4)))
        elif m.group(5) is not None:  # parens
            tokens.append(m.group(5))
        elif m.group(6):  # fallback symbol
            tokens.append(m.group(6))
        else:
            # Either `nil` or `t` matched outside named groups — disambiguate by
            # looking at the actual matched text.
            tok = m.group(0).strip()
            if tok == 'nil':
                tokens.append(None)
            elif tok == 't':
                tokens.append(True)
    return tokens


def _parse_list(tokens: list, pos: int) -> tuple[list, int]:
    result = []
    i = pos
    while i < len(tokens):
        tok = tokens[i]
        if tok == '(':
            sublist, i = _parse_list(tokens, i + 1)
            result.append(sublist)
        elif tok == ')':
            return result, i + 1
        else:
            result.append(tok)
            i += 1
    return result, i


def parse(s: str) -> Any:
    """Parse an s-expression string into Python objects.

    (:reward 0.8 :done nil :error "msg")
    -> {':reward': 0.8, ':done': None, ':error': 'msg'}

    Returns the first complete s-expression found.

    Auto-plist coercion only fires when the top-level form LOOKS like a
    plist — keyword keys, even length. Without the guard we crashed on
    bare nested forms like ``(let ((x 1)) x)`` because nested lists
    are unhashable as dict keys (the per-test `:expected`/`:actual`
    strings emitted by the grader hit this regularly).
    """
    tokens = _tokenize(s)
    if not tokens:
        return None
    result, _ = _parse_list(tokens, 0)
    if (len(result) == 1
            and isinstance(result[0], list)
            and _looks_like_plist(result[0])):
        return _plist_to_dict(result[0])
    return result


def _looks_like_plist(lst: list) -> bool:
    """A plist has even length and keyword keys in even positions.
    Conservative — only matches the wire shape the grader emits."""
    if not lst or len(lst) % 2 != 0:
        return False
    return all(
        isinstance(lst[i], str) and lst[i].startswith(":")
        for i in range(0, len(lst), 2)
    )


def plist_to_dict(plist: list) -> dict:
    """Convert plist list to dictionary."""
    result = {}
    for i in range(0, len(plist), 2):
        if i + 1 < len(plist):
            key = plist[i]
            if isinstance(key, str) and key.startswith(':'):
                key = key  # keep colon prefix
            result[key] = plist[i + 1]
    return result


_plist_to_dict = plist_to_dict


def encode(msg: str) -> str:
    """Encode a message as an s-expression string for SBCL.

    Returns a string that SBCL can read with CL:READ.
    """
    # Escape backslashes and double quotes in the string
    escaped = msg.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'
