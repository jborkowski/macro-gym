"""Low-level SBCL subprocess wrapper.

Owns a single persistent SBCL process running ``lisp/server.lisp`` and the
length-prefixed framing protocol for responses.

Wire protocol (locked contract):

* Python -> SBCL: ``<sexp>\\n`` on stdin. Requests are NOT length-prefixed.
* SBCL -> Python: ``<decimal-byte-count>\\n<UTF-8 payload>\\n`` on stdout.

The byte count counts UTF-8 bytes of the payload exclusive of the trailing
newline. We deliberately open the subprocess in binary mode (``text=False``)
so framing arithmetic stays correct under Unicode payloads.

Higher-level pool / grader logic lives in :mod:`macro_gym.pool` and
:mod:`macro_gym.grader`.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

__all__ = ["SBCLProcess", "find_sbcl", "SERVER_PATH", "PROJECT_ROOT"]


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LISP_DIR = PROJECT_ROOT / "lisp"
SERVER_PATH = LISP_DIR / "server.lisp"

# Marker the Lisp server prints on stderr once it is ready to accept requests.
READY_MARKER = b"macro-gym server"


def find_sbcl() -> str:
    """Locate the SBCL binary on ``$PATH``.

    Raises:
        RuntimeError: if no ``sbcl`` executable is on ``$PATH``.
    """
    sbcl = shutil.which("sbcl")
    if sbcl:
        return sbcl
    raise RuntimeError(
        "SBCL not found on PATH. Install with one of:\n"
        "  macOS:   brew install sbcl\n"
        "  Debian:  apt-get install sbcl\n"
        "  Fedora:  dnf install sbcl\n"
        "  Arch:    pacman -S sbcl"
    )


class SBCLProcess:
    """One persistent SBCL subprocess speaking the framed grader protocol.

    The class is intentionally dumb: it owns spawn/teardown and the bytes-in /
    bytes-out framing. Worker state (grade counts, dirtiness, recycle policy)
    lives one layer up in :class:`macro_gym.pool.Worker`.
    """

    def __init__(
        self,
        sbcl_path: Optional[str] = None,
        heap_mb: int = 384,
        server_path: Optional[Path] = None,
        cwd: Optional[Path] = None,
    ) -> None:
        self.sbcl_path: str = sbcl_path or find_sbcl()
        self.heap_mb: int = int(heap_mb)
        self.server_path: Path = Path(server_path) if server_path else SERVER_PATH
        self.cwd: Path = Path(cwd) if cwd else PROJECT_ROOT
        self._proc: Optional[subprocess.Popen] = None
        # Ring buffer of recent stderr bytes for post-mortem diagnostics.
        self._stderr_tail: bytearray = bytearray()
        self._stderr_tail_max: int = 512

    # ----- lifecycle -------------------------------------------------------

    @property
    def pid(self) -> int:
        if self._proc is None:
            raise RuntimeError("SBCLProcess not started")
        return self._proc.pid

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, ready_timeout: float = 10.0) -> None:
        """Spawn the SBCL subprocess and wait for the ready marker on stderr.

        Args:
            ready_timeout: seconds to wait for the ``macro-gym server`` line
                on stderr before giving up.
        Raises:
            TimeoutError: if the ready marker doesn't appear in time.
            RuntimeError: if SBCL exits before becoming ready.
        """
        if self._proc is not None:
            return
        # SBCL arg order matters: runtime options (--noinform, --dynamic-space-size)
        # MUST precede toplevel options. --script alone is sufficient — it implies
        # --no-userinit --no-sysinit --disable-debugger --no-print --quit. Adding
        # --non-interactive causes SBCL to exit immediately instead of staying in
        # the server's (loop). --disable-debugger is also redundant with --script.
        cmd = [
            self.sbcl_path,
            "--noinform",
            "--dynamic-space-size",
            str(self.heap_mb),
            "--script",
            str(self.server_path),
        ]
        # text=False: we control encoding ourselves to keep framing bytewise.
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        self._wait_for_ready(ready_timeout)

    def _wait_for_ready(self, timeout: float) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        deadline = time.monotonic() + timeout
        buf = bytearray()
        stderr_fd = self._proc.stderr.fileno()
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                # Process died before signaling ready; drain stderr.
                remainder = self._proc.stderr.read() or b""
                self._absorb_stderr(remainder)
                raise RuntimeError(
                    f"SBCL exited before ready marker (rc={self._proc.returncode}); "
                    f"stderr tail: {self.stderr_tail()!r}"
                )
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([stderr_fd], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = os.read(stderr_fd, 4096)
            if not chunk:
                # EOF on stderr.
                continue
            self._absorb_stderr(chunk)
            buf.extend(chunk)
            if READY_MARKER in buf:
                return
        raise TimeoutError(
            f"SBCL did not signal ready within {timeout}s; "
            f"stderr tail: {self.stderr_tail()!r}"
        )

    def close(self, grace: float = 3.0) -> None:
        """Send ``(:eof)`` and wait briefly; kill on timeout. Idempotent."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        # Best-effort polite shutdown.
        try:
            if proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(b"(:eof)\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            # Drain any stderr left over for diagnostics.
            try:
                if proc.stderr is not None:
                    leftover = proc.stderr.read() or b""
                    self._absorb_stderr(leftover)
            except (OSError, ValueError):
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass

    def kill(self) -> None:
        """Hard-kill the subprocess without waiting. Idempotent."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass

    # ----- IO --------------------------------------------------------------

    def send_request(self, request_str: str) -> None:
        """Send a single request line (UTF-8 encoded).

        ``request_str`` should NOT include the trailing newline — we add it.
        """
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("SBCLProcess not started")
        if self._proc.poll() is not None:
            raise BrokenPipeError(
                f"SBCL process exited (rc={self._proc.returncode})"
            )
        payload = request_str.encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BrokenPipeError(f"send_request failed: {exc}") from exc

    def read_response(self, timeout: float) -> bytes:
        """Read one framed response. Returns the raw UTF-8 payload bytes.

        Frame: ``<decimal byte count>\\n<payload bytes>\\n``.

        Args:
            timeout: total wall-clock budget for the entire frame.
        Raises:
            TimeoutError: if the frame doesn't arrive in time. The subprocess
                is killed as a side-effect so the caller can restart it.
            BrokenPipeError / RuntimeError: on protocol / EOF failures.
        """
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("SBCLProcess not started")
        deadline = time.monotonic() + timeout
        try:
            count_line = self._read_line_until(deadline)
        except TimeoutError:
            self.kill()
            raise
        try:
            byte_count = int(count_line.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError) as exc:
            self.kill()
            raise RuntimeError(
                f"Protocol error: expected decimal byte count, got {count_line!r}"
            ) from exc
        if byte_count < 0 or byte_count > 64 * 1024 * 1024:
            self.kill()
            raise RuntimeError(
                f"Protocol error: implausible payload size {byte_count}"
            )
        payload = self._read_exact(byte_count, deadline)
        # Consume the trailing newline (one byte).
        trailing = self._read_exact(1, deadline)
        if trailing != b"\n":
            self.kill()
            raise RuntimeError(
                f"Protocol error: expected trailing newline, got {trailing!r}"
            )
        return payload

    # ----- diagnostics -----------------------------------------------------

    def drain_stderr(self) -> None:
        """Pull whatever's available on stderr into the ring buffer (non-blocking)."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            fd = self._proc.stderr.fileno()
        except (OSError, ValueError):
            return
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                return
            try:
                chunk = os.read(fd, 4096)
            except (OSError, BlockingIOError):
                return
            if not chunk:
                return
            self._absorb_stderr(chunk)

    def stderr_tail(self) -> str:
        """Return last ~512 bytes of stderr decoded as UTF-8 (replacement on error)."""
        self.drain_stderr()
        return bytes(self._stderr_tail).decode("utf-8", errors="replace")

    # ----- internal helpers ------------------------------------------------

    def _absorb_stderr(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._stderr_tail.extend(chunk)
        overflow = len(self._stderr_tail) - self._stderr_tail_max
        if overflow > 0:
            del self._stderr_tail[:overflow]

    def _read_exact(self, n: int, deadline: float) -> bytes:
        """Read exactly ``n`` bytes from stdout, honoring ``deadline`` (monotonic)."""
        if n == 0:
            return b""
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        chunks: list[bytes] = []
        remaining = n
        while remaining > 0:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                self.kill()
                raise TimeoutError(
                    f"read_response timeout while reading {n} bytes "
                    f"(stderr tail: {self.stderr_tail()!r})"
                )
            ready, _, _ = select.select([fd], [], [], min(timeout, 0.5))
            if not ready:
                if self._proc.poll() is not None:
                    raise BrokenPipeError(
                        f"SBCL exited mid-response (rc={self._proc.returncode}); "
                        f"stderr tail: {self.stderr_tail()!r}"
                    )
                continue
            try:
                chunk = os.read(fd, remaining)
            except (OSError, BlockingIOError) as exc:
                raise BrokenPipeError(f"stdout read failed: {exc}") from exc
            if not chunk:
                raise BrokenPipeError(
                    f"SBCL closed stdout mid-response; "
                    f"stderr tail: {self.stderr_tail()!r}"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_line_until(self, deadline: float) -> bytes:
        """Read bytes up to (and including) the next ``\\n``."""
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        buf = bytearray()
        while True:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError(
                    f"read_response timeout waiting for byte-count line "
                    f"(stderr tail: {self.stderr_tail()!r})"
                )
            ready, _, _ = select.select([fd], [], [], min(timeout, 0.5))
            if not ready:
                if self._proc.poll() is not None:
                    raise BrokenPipeError(
                        f"SBCL exited before responding (rc={self._proc.returncode}); "
                        f"stderr tail: {self.stderr_tail()!r}"
                    )
                continue
            # Read 1 byte at a time for the count line to avoid over-reading
            # into the payload. The count line is at most ~12 ASCII bytes so
            # this is negligible overhead and keeps the framing strict.
            try:
                chunk = os.read(fd, 1)
            except (OSError, BlockingIOError) as exc:
                raise BrokenPipeError(f"stdout read failed: {exc}") from exc
            if not chunk:
                raise BrokenPipeError(
                    f"SBCL closed stdout before responding; "
                    f"stderr tail: {self.stderr_tail()!r}"
                )
            if chunk == b"\n":
                return bytes(buf)
            buf.extend(chunk)
            if len(buf) > 32:
                self.kill()
                raise RuntimeError(
                    f"Protocol error: byte-count line too long: {bytes(buf)!r}"
                )
