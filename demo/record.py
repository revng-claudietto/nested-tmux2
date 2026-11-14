"""Drive the installed application through a real PTY for asciinema.

The recording uses its own HOME, runtime directory, and generated tmux sockets.
Every transition waits for application state; timing pauses are for the viewer.
"""

import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

COLS, ROWS = 100, 28


class Demo:
    def __init__(self, directory):
        self.directory = directory
        self.work = directory / "work"
        self.work.mkdir()
        (directory / "run").mkdir(mode=0o700)
        self.env = {
            **{
                k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE", "TMUX_PARENT"}
            },
            "HOME": str(directory),
            "XDG_RUNTIME_DIR": str(directory / "run"),
            "TERM": "xterm-256color",
            "PS1": "demo:~/work $ ",
            "PROMPT_COMMAND": "",
            "HISTFILE": "/dev/null",
            "SHELL": shutil.which("bash"),
        }
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.process = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-i"],
            cwd=self.work,
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        self.done = threading.Event()
        self.output = bytearray()
        self.reader = threading.Thread(target=self.drain, daemon=True)
        self.reader.start()

    def drain(self):
        while not self.done.is_set():
            if select.select([self.master], [], [], 0.1)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.output.extend(data)
                os.write(sys.stdout.fileno(), data)

    def cli(self, operation, *args):
        return subprocess.run(
            ["nested-tmux", operation, *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout

    def status(self):
        return json.loads(self.cli("status"))

    def wait(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(0.05)
        raise RuntimeError("demo transition did not complete")

    def tmux(self, *args):
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()

    def caption(self, text):
        self.tmux("set-option", "-g", "status-right-length", "64")
        self.tmux("set-option", "-g", "status-right", " " + text + " ")
        time.sleep(0.9)

    def keys(self, data):
        os.write(self.master, data)

    def type(self, text):
        for char in text:
            self.keys(char.encode())
            time.sleep(0.025)
        self.keys(b"\r")
        time.sleep(0.6)

    def navigate(self, data, active, caption):
        self.caption(caption)
        self.keys(data)
        self.wait(lambda: self.status()["active"] == active)
        time.sleep(1)

    def branch(self, name, parent):
        self.caption("Ctrl+a, Ctrl+s: create a branch named " + name)
        offset = len(self.output)
        self.keys(b"\x01\x13")
        self.wait(lambda: b"New row id" in self.output[offset:])
        self.type(name)
        self.wait(lambda: self.status()["active"] == parent + "/" + name)
        time.sleep(0.5)

    def run(self):
        self.wait(lambda: b"~/work $" in self.output)
        self.type("# Build your terminal tree by hand. No YAML needed.")
        self.type("nested-tmux")
        self.wait(lambda: bool(self.status()["rows"]))
        self.socket = self.status()["rows"][0]["socket"]
        self.branch("project", "work")
        self.branch("tools", "work/project")
        self.caption("Each branch adds a visible row")
        self.caption("Ctrl+t: create a regular terminal in this row")
        self.keys(b"\x14")
        self.wait(lambda: len(self.status()["rows"][2]["windows"]) == 2)
        self.caption("Ctrl+a, A: rename the terminal")
        self.keys(b"\x01A")
        self.wait(lambda: b"New title:" in self.output)
        self.type("notes")
        time.sleep(0.5)
        self.type("printf 'A new terminal, in the same nested workspace.\\n'")
        self.caption("Alt+Left / Right: switch tabs in the active row")
        self.keys(b"\x1b[1;3D")
        time.sleep(1)
        self.keys(b"\x1b[1;3C")
        time.sleep(1)
        self.caption("exit: close this terminal; the other tab stays open")
        self.type("exit")
        self.wait(lambda: len(self.status()["rows"][2]["windows"]) == 1)
        self.navigate(b"\x1b[1;3B", "work/project", "Alt+Down: return to the parent row")
        self.navigate(b"\x1b[1;3A", "work/project/tools", "Alt+Up: enter the selected child row")
        self.navigate(b"\x1b[1;3B", "work/project", "Alt+Down: back to project")
        self.branch("scratch", "work/project")
        self.caption("A branch has its own row and terminals")
        self.type("printf 'This whole branch can be closed together.\\n'")
        self.navigate(b"\x1b[1;3B", "work/project", "Alt+Down: select the branch from its parent")
        self.caption("Ctrl+a, x: close the selected branch and its terminals")
        self.keys(b"\x01x")
        self.wait(lambda: b"Close this terminal" in self.output)
        time.sleep(1.5)
        self.keys(b"y")
        self.wait(lambda: all(r["path"] != "work/project/scratch" for r in self.status()["rows"]))
        self.caption("Branch closed. Other rows keep running.")
        time.sleep(1)
        self.navigate(b"\x1b[1;3B", "work", "Alt+Down: back to the root")
        self.caption("Ctrl+a, d: detach without stopping the workspace")
        self.keys(b"\x01d")
        time.sleep(0.8)
        self.type("nested-tmux dump -o saved.yaml")
        self.wait(lambda: (self.work / "saved.yaml").exists())
        self.type("# Optional YAML: restore this tree after reboot with up saved.yaml")
        time.sleep(2)

    def close(self):
        try:
            self.cli("down")
        finally:
            self.done.set()
            self.reader.join(timeout=2)
            self.keys(b"exit\r")
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            os.close(self.master)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nt-demo-", dir="/tmp") as temporary:
        demo = Demo(Path(temporary))
        try:
            demo.run()
        finally:
            demo.close()
