"""Documented tmux control mode, over pipes or SSH (never the binary socket protocol)."""

import os
import queue
import select
import subprocess
import threading
import time
from collections import deque

from .config import Error
from .ssh import Transport, clean_env  # noqa: F401 - retain the public transport imports

MAX_LINE = 65536
MAX_RESPONSE = 1024 * 1024
MAX_QUEUED_LINES = 128


class TransportError(Error):
    """The stream cannot safely be reused; the last command may have executed."""


def quote(value):
    """Quote one tmux parser token. Shell quoting is a separate operation."""
    value = str(value)
    if "\0" in value:
        raise Error("tmux command arguments cannot contain NUL")
    # Single quotes suppress tmux variable expansion, including a leading '$'.
    # Concatenate an escaped quote/newline between literal segments as needed.
    return (
        "'"
        + (value.replace("'", "'\\''").replace("\n", "'\"\\n\"'").replace("\r", "'\"\\r\"'"))
        + "'"
    )


def command(*args):
    return " ".join(quote(arg) for arg in args)


class Control:
    def __init__(self, transport, socket_name, timeout=15):
        self.transport = transport
        self.socket_name = socket_name
        self.timeout = timeout
        self.process = None
        self.lines = queue.Queue(maxsize=MAX_QUEUED_LINES)
        self.errors = deque(maxlen=20)
        self.threads = []

    def connect(self, create=True):
        for attempt in range(3):
            try:
                self._connect(create)
                return
            except TransportError as exc:
                if not create or "server exited unexpectedly" not in str(exc) or attempt == 2:
                    raise
                # kill-server can acknowledge before its listener disappears.
                # Only the idempotent bootstrap new-session -A is retried here;
                # no user launch command has been sent. Recovery is attach-only.
                time.sleep(0.05 * (attempt + 1))

    def _connect(self, create):
        self.close()
        if self.transport.master:
            self.transport.master.ensure()
        self.lines = queue.Queue(maxsize=MAX_QUEUED_LINES)
        self.errors = deque(maxlen=20)
        args = ["tmux", "-u", "-L", self.socket_name, "-f", "/dev/null", "-C"]
        args += (
            ["new-session", "-A", "-s", "tree", "-n", "__bootstrap__", "sleep 2147483647"]
            if create
            else ["attach-session", "-t", "tree"]
        )
        self.process = subprocess.Popen(
            self.transport.wrap(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        process, lines, errors = self.process, self.lines, self.errors

        stop = self.reader_stop = threading.Event()

        def read(stream, limit, stdout=False):
            pending = b""
            fd = stream.fileno()
            os.set_blocking(fd, False)
            try:
                while not stop.is_set():
                    if not select.select([fd], [], [], 0.1)[0]:
                        if process.poll() is not None:
                            break
                        continue
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    pending += chunk
                    while pending:
                        end = pending.find(b"\n")
                        if end < 0 and len(pending) <= limit:
                            break
                        if stdout and (end > limit or (end < 0 and len(pending) > limit)):
                            errors.append("remote control line exceeds size limit")
                            process.terminate()
                            return
                        count = end + 1 if 0 <= end <= limit else min(len(pending), limit)
                        line, pending = pending[:count], pending[count:]
                        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if stdout:
                            try:
                                lines.put_nowait(text)
                            except queue.Full:
                                errors.append(
                                    "remote control notification queue exceeds size limit"
                                )
                                process.terminate()
                                return
                        else:
                            errors.append(text)
                if pending and not stdout:
                    errors.append(pending.decode("utf-8", errors="replace"))
            finally:
                if stdout:
                    while True:
                        try:
                            lines.put_nowait(None)
                            break
                        except queue.Full:
                            try:
                                lines.get_nowait()
                            except queue.Empty:
                                pass

        self.threads = [
            threading.Thread(target=read, args=(process.stdout, MAX_LINE, True), daemon=True),
            threading.Thread(target=read, args=(process.stderr, 4096), daemon=True),
        ]
        for thread in self.threads:
            thread.start()
        try:
            self._response()
            self.call("refresh-client", "-f", "no-output,ignore-size")
        except Exception:
            self.close()
            raise

    def _response(self, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        block = None
        output = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(
                    f"tmux control timeout on {self.transport.host}; command outcome is unknown"
                )
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TransportError(
                    f"tmux control timeout on {self.transport.host}; command outcome is unknown"
                ) from exc
            if line is None:
                detail = "; ".join(self.errors)
                raise TransportError(
                    f"tmux control disconnected on {self.transport.host}: {detail}"
                )
            if block is None:
                if line.startswith("%begin "):
                    block = line.split()[1:3]
                elif line.startswith("%exit"):
                    raise TransportError(f"tmux control exited on {self.transport.host}: {line}")
                continue
            fields = line.split()
            if fields and fields[0] in {"%end", "%error"} and fields[1:3] == block:
                if fields[0] == "%error":
                    raise Error("tmux: " + "\n".join(output))
                return output
            # Notifications occur outside response blocks. Command output may itself start with %.
            size += len(line)
            if size > MAX_RESPONSE:
                raise TransportError("remote control response exceeds size limit")
            output.append(line)

    @property
    def connected(self):
        return self.process is not None and self.process.poll() is None

    def call(self, *args, timeout=None):
        if self.process is None or self.process.poll() is not None:
            raise Error(f"tmux control is not connected on {self.transport.host}")
        try:
            self.process.stdin.write(command(*args) + "\n")
            self.process.stdin.flush()
            return self._response(timeout)
        except TransportError:
            self.close()
            raise
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise Error(f"tmux control write failed on {self.transport.host}: {exc}") from exc

    def close(self):
        process = self.process
        if process is None:
            return
        self.process = None
        self.reader_stop.set()
        if process.poll() is None:
            # Ask the persistent remote tmux client to release its mux channel.
            # A broken/stalled channel must never block local shutdown.
            try:
                os.set_blocking(process.stdin.fileno(), False)
                os.write(process.stdin.fileno(), b"detach-client\n")
                process.wait(timeout=0.25)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for thread in self.threads:
            thread.join()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
