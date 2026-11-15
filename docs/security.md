# Security review

The design grants a remote machine control over its own terminals and their
displayed output. It provides no remote RPC endpoint or parent-control channel
into the local workspace. This is a reviewed trust boundary with regression
tests, not a guarantee against vulnerabilities in SSH, tmux, the OS, or a terminal
emulator.

## Trust assumptions

The local user, local configuration, SSH configuration, application files, and
local tmux server are trusted. YAML commands intentionally execute with the
user's privileges on the specified host. Only load configurations you trust.
YAML parsing uses ruamel.yaml's round-trip loader with duplicate-key and
non-string-key rejection. A restricted constructor rejects unknown tags,
including Python object tags; no unsafe constructors are enabled. This prevents
object construction, not the intentional execution of `command`. The runtime
model contains plain strings, so YAML comments and styles stay in the recipe
editor. Additive saving checks that aliases cannot make edits to one row change
an unrelated row.
Automatic recipes are read only from local XDG configuration directories, never
from the current working directory or a remote host. Workspace names are
validated before constructing paths. User recipes take precedence over system
recipes; an invalid selected recipe fails rather than falling back to another.
Configuration symlinks are supported, including Nix-managed configuration files;
their targets are part of the trusted local configuration.

The remote account/server and its stdout/stderr may be hostile. Other processes
running as the same local Unix user are **not** isolated: they can use local
tmux sockets and the controller socket. Running remote work in a separate host
or Unix account is a prerequisite for that boundary; naming a localhost SSH
alias does not create isolation from the same account.

## Boundary controls

| Potential path from remote to local | Control |
| --- | --- |
| Calling the local controller | Unix socket only, private local directory, no listener on TCP, no forwarding or reverse SSH channel. |
| Asking a parent row to execute a command | Root-local keyboard bindings and a locally held tree. No `TMUX_PARENT`, remote hook, or remote Python agent. |
| Forging tree structure/host routes | Only the local root's metadata supplies the live tree. Remote replies never become local RPC requests. A remote subtree cannot change host or return to `local`. |
| Forging terminal exit reports | Reported window IDs are matched only against the reporting row's locally held children. Only exited leaves are removed; unrelated local terminals and other named workspaces are outside that map. Disconnects never trigger removal. |
| SSH agent/X11/port access | Explicitly disable agent and X11 forwarding, clear configured forwards, and disable SSH escape commands on both control and interactive connections. |
| Reusing a permissive SSH master | A private, locally derived master socket overrides the configured `ControlPath`. The application creates its own master with forwarding disabled; it never joins an unrelated master. Channel clients cannot fall back to an independent connection. |
| Local SSH hooks | `PermitLocalCommand=no`; host arguments are validated aliases, not arbitrary SSH options. User-authored ProxyCommand/ProxyJump configuration remains trusted. |
| Command or format injection | Distinct tmux-token and shell-argument encoding; stable locally generated socket names; no shell evaluation of remote replies. Reject tmux format introducers in cwd. |
| Terminal clipboard, passthrough, or rename sequences | Local root `set-clipboard=off`, `get-clipboard=off` where supported, `allow-passthrough=off`, `allow-rename=off`, and `set-titles=off`. Child settings also default to these values. Tab names use the configured title or node ID, with automatic program-based renaming disabled. |
| Malformed/flooded control data | Response framing, bounded lines, bounded reply size and queue, bounded SSH master diagnostics, command deadlines, sanitized CLI diagnostics; overflow/disconnect closes the stream. |
| Native keyboard helper | Fixed action vocabulary, bounded buffers, partial I/O handling, one monotonic deadline, sanitized diagnostics, and no retry after a missing acknowledgement. It connects only to the local Unix socket; it has no tmux parser, shell execution, or SSH code. |
| Replaying a command after a lost acknowledgement | Mark startup before launch; retain the uncertain state and refuse automatic replay. Background reconnect uses attach-only control clients and never recreates a missing server. |
| Discovery redirecting a kill to another host | Only local configuration chooses queried hosts. Socket basenames and workspace/path metadata are validated; returned paths are never executed or used as local socket paths. A remote row cannot claim a local root or expand recursive termination onto another host. |

The `nested-tmux2-` prefix and deterministic identities identify managed servers;
they are not authentication. The same remote account and workspace/path identify
the same server from different computers, so an authorized full shutdown can
terminate another client's attached row. Other workspaces remain outside the
selection. Remote metadata remains untrusted and never becomes local tree
authority. Discovery output and stderr are bounded, operations have deadlines,
and failed hosts are reported without treating them as empty inventories.

These SSH controls use documented [OpenSSH options](https://man.openbsd.org/ssh_config.5);
clipboard and passthrough settings follow the [tmux manual](https://man.openbsd.org/tmux.1).
Host-key verification follows the user's SSH configuration and is not disabled.
The test sshd uses a generated host key pinned in a temporary known_hosts file.
An agent may authenticate outbound SSH locally without being forwarded.

## What remains possible

- A remote machine can read or change all remote terminals under its account,
  fabricate their output and control replies, stall/disconnect, or destroy them.
  Reply parsing cannot authenticate a compromised remote tmux implementation.
- A remote application can draw misleading text, including imitation status
  rows and prompts, and can receive keystrokes you send to it. Never interpret
  terminal output as a trustworthy local approval prompt.
- Terminal parsing still occurs in local tmux. The outer server filters the
  explicit clipboard/passthrough paths above, but this is not a complete emulator
  sandbox or protection against parser bugs. Keep those dependencies updated.
- A remote cwd observed during export can be false. It remains data scoped to
  that remote subtree; review exported YAML before reusing or sharing it.
- Explicit environment settings and commands are visible to processes under
  their account and stored in root metadata/export files. Do not use this as a
  secrets manager. User SSH configuration may separately send environment
  variables; those local settings remain the user's trust decision.
- An unreachable remote host can delay an operation. Returning to the parent
  does not require a successful remote query, but a style update may take its
  bounded timeout. Background workers automatically reattach after connectivity
  returns; connection attempts and retries have deadlines/backoff. A shared
  connection failure interrupts all channels using that master until recovery.

## Verification

Tests launch an unprivileged loopback OpenSSH server with temporary keys. They
exercise actual SSH control and terminal streams, keyboard navigation, runtime
remote edits, and reconnection without duplicate process launches. A permissive
client configuration attempts agent/X11 forwarding, a local command, a shared
master socket, and a local port forward. A real pre-existing permissive master
is established first; the application still creates an independent private
master with forwarding disabled. Tests also enable hostile remote clipboard/passthrough output and verify
that it does not escape the local root or execute displayed shell syntax.
Tests count real SSH authentications to verify sharing and recovery, exercise
idle expiry and stale socket replacement, and ensure channel exhaustion never
opens another TCP connection. Unsafe socket paths and symlinks are rejected.
An SSH exit test closes a remote branch while checking that the local shell's
process survives. Local tests verify that unavailable rows are retained and
that edits, detach, and shutdown in one named workspace leave another intact.

Protocol tests cover quoting, response synchronization after errors, bounded
messages and deadlines, and unknown command outcomes. Local tests check socket
permissions and malformed requests. All tmux mutations address generated test
socket names explicitly. These tests are regression evidence; the loopback
fixture is not a separate-machine penetration test or a formal security proof.
