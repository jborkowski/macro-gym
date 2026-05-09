"""SBCL subprocess management for macro-gym.

Spawns a persistent SBCL process running the macro-gym server,
communicates via s-expression protocol over stdin/stdout.
"""

import subprocess
import os
import time
from pathlib import Path

from .sexp import parse, plist_to_dict


LISP_DIR = Path(__file__).parent.parent / "lisp"
SERVER_PATH = LISP_DIR / "server.lisp"


def find_sbcl() -> str:
    """Find the SBCL binary."""
    import shutil
    sbcl = shutil.which("sbcl")
    if sbcl:
        return sbcl
    raise RuntimeError("SBCL not found. Install: brew install sbcl")


class SBCLService:
    """Manages a persistent SBCL subprocess for macro evaluation."""

    def __init__(self, sbcl_path: str | None = None):
        self.sbcl_path = sbcl_path or find_sbcl()
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        """Start the SBCL server subprocess."""
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            [self.sbcl_path, "--noinform", "--script", str(SERVER_PATH)],
            cwd=str(Path(__file__).parent.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Wait for ready message on stderr
        ready = self._proc.stderr.readline()
        if "ready" not in ready:
            # SBCL might print warnings; keep reading
            for _ in range(5):
                line = self._proc.stderr.readline()
                if "ready" in line:
                    break

    def stop(self) -> None:
        """Stop the SBCL subprocess."""
        if self._proc:
            try:
                self._proc.stdin.write(":eof\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None

    def _send(self, s: str) -> None:
        assert self._proc and self._proc.stdin, "Service not started"
        self._proc.stdin.write(s)
        self._proc.stdin.write("\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict:
        assert self._proc and self._proc.stdout, "Service not started"
        line = self._proc.stdout.readline()
        if not line:
            raise ConnectionError("SBCL process closed stdout")
        result = parse(line)
        if isinstance(result, list):
            return plist_to_dict(result)
        elif isinstance(result, dict):
            return result
        return {':raw': result}

    def eval_macro(self, kata_id: str, macro_source: str) -> dict:
        """Send a macro for evaluation, return result dict.

        Result keys: :reward, :done, :passed, :total, :results, :error
        """
        escaped_source = macro_source.replace('\\', '\\\\').replace('"', '\\"')
        request = f'(eval-macro "{kata_id}" "{escaped_source}")'
        self._send(request)
        return self._recv()


# Singleton for reuse across episodes
_service: SBCLService | None = None


def get_service() -> SBCLService:
    global _service
    if _service is None:
        _service = SBCLService()
        _service.start()
    return _service


def shutdown_service() -> None:
    global _service
    if _service:
        _service.stop()
        _service = None
