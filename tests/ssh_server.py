"""Unprivileged OpenSSH fixture: private keys, strict host checking, loopback only."""

import os
import pwd
import shutil
import socket
import subprocess
from pathlib import Path

from helpers import eventually


class SSHServer:
    def __init__(self, directory, allow_agent_forwarding=False, max_sessions=10):
        self.directory = directory
        sockets = directory / "t"
        sockets.mkdir(mode=0o700)
        self.sshd = shutil.which("sshd")
        if not self.sshd:
            raise RuntimeError("OpenSSH sshd is required for the SSH integration tests")
        for name in ("host", "client"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(directory / name)],
                check=True,
            )
        (directory / "authorized_keys").write_text((directory / "client.pub").read_text())
        (directory / "authorized_keys").chmod(0o600)
        (directory / "known_hosts").write_text(
            "nt-test-host " + (directory / "host.pub").read_text()
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server_config = directory / "sshd_config"
        self.server_config.write_text(f"""ListenAddress 127.0.0.1
Port {self.port}
LogLevel VERBOSE
MaxSessions {max_sessions}
HostKey {directory / "host"}
PidFile {directory / "sshd.pid"}
AuthorizedKeysFile {directory / "authorized_keys"}
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
StrictModes no
PermitRootLogin prohibit-password
AllowUsers {pwd.getpwuid(os.getuid()).pw_name}
AllowTcpForwarding no
AllowAgentForwarding {"yes" if allow_agent_forwarding else "no"}
X11Forwarding no
PrintMotd no
SetEnv PATH={os.environ["PATH"]} TMUX_TMPDIR={sockets}
""")
        self.client_config = directory / "ssh_config"
        self.client_config.write_text(f"""Host nt-test-host
  HostName 127.0.0.1
  Port {self.port}
  User {pwd.getpwuid(os.getuid()).pw_name}
  IdentityFile {directory / "client"}
  IdentitiesOnly yes
  HostKeyAlias nt-test-host
  UserKnownHostsFile {directory / "known_hosts"}
  GlobalKnownHostsFile /dev/null
  StrictHostKeyChecking yes
  LogLevel ERROR
""")
        self.log = (directory / "sshd.log").open("w+")
        env = dict(os.environ)
        user = pwd.getpwuid(os.getuid())
        if not Path(user.pw_shell).exists():
            # Nix's sandbox account has /noshell. Give only the test sshd a
            # synthetic NSS entry; never edit the host's passwd or SSH settings.
            wrapper = env.get("NESTED_TMUX_TEST_NSS_WRAPPER")
            if not wrapper:
                raise RuntimeError(
                    "sshd test account has no usable shell; provide NESTED_TMUX_TEST_NSS_WRAPPER"
                )
            passwd = directory / "passwd"
            group = directory / "group"
            passwd.write_text(
                f"{user.pw_name}:x:{user.pw_uid}:{user.pw_gid}:SSH test:{directory}:{shutil.which('bash')}\n"
            )
            group.write_text(f"test:x:{user.pw_gid}:\n")
            env.update(
                LD_PRELOAD=wrapper, NSS_WRAPPER_PASSWD=str(passwd), NSS_WRAPPER_GROUP=str(group)
            )
        self.env = env
        self.resume()
        try:
            eventually(self.ready)
            probe = subprocess.run(
                [
                    "ssh",
                    "-F",
                    str(self.client_config),
                    "-o",
                    "BatchMode=yes",
                    "nt-test-host",
                    "printenv TMUX_TMPDIR",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode or probe.stdout.strip() != str(sockets):
                raise RuntimeError(probe.stderr + (directory / "sshd.log").read_text())
        except Exception:
            self.close()
            raise

    def ready(self):
        if self.process.poll() is not None:
            raise RuntimeError((self.directory / "sshd.log").read_text())
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    @property
    def authentications(self):
        return (self.directory / "sshd.log").read_text().count("Accepted publickey")

    def pause(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)

    def resume(self):
        if getattr(self, "process", None) is not None and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            [self.sshd, "-D", "-e", "-f", str(self.server_config)],
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=self.log,
            env=self.env,
        )
        eventually(self.ready)

    def close(self):
        self.pause()
        self.log.close()
