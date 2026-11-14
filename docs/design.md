# Design and alternatives

The UI requirement decides the architecture: visible nested rows and keyboard
navigation, with the controller authoritative about the
tree. Every branch is a dedicated tmux server, with a single `tree` session and
one window per child. Branch windows run terminal clients attached to child
servers. A leaf window runs the user's command.

```mermaid
flowchart LR
    K[Keyboard] --> R[Local root tmux]
    R -- private Unix socket RPC --> C[Local controller]
    C -- tmux control mode --> L[Local child rows]
    C -- outbound SSH: control mode --> S[Remote tmux rows]
    R -- nested terminal attachment --> L
    R -- outbound SSH: terminal --> S
```

The root owns navigation keys. Local RPC selects a row or tab through its tmux
control connection and updates row colors. A remote row never asks a local row
to run anything. The terminal's ordinary input/output is separate from the
control stream. Each branch has its own socket namespace, isolated from the
user's existing tmux sessions and configuration.

## Why these interfaces

The application exposes a small JSON protocol on a mode-0600 Unix socket inside
a mode-0700 directory. Key bindings invoke a local helper that writes to this
socket. Operations are serialized, so concurrent shortcuts do not interleave
tmux commands or update the tree concurrently.

For tmux itself, use its documented [control mode](https://github.com/tmux/tmux/wiki/Control-Mode)
through a persistent `tmux -C` subprocess. This gives commands and framed replies
over stdin/stdout, and works unchanged over `ssh -T`. The tmux client handles
the actual server socket. Writing directly to tmux's internal binary
[client/server protocol](https://github.com/tmux/tmux/blob/3.4/proc.c) would couple
the application to internal structures and descriptor passing without improving
the UI or trust boundary.

Commands are encoded as tmux arguments; SSH commands are separately shell
quoted. Launches use `respawn-pane` with a launch command and cwd, never text
typed into a running shell. No synthetic Alt+F12 or parent-socket environment
variable is used. A timeout closes that control stream and does not retry a
mutation whose outcome is unknown.

## Live tree versus initialization

YAML is a bootstrap recipe. The local root's `@nt-tree` option stores the live
tree; per-window options store stable node paths and startup markers. The root
metadata is base64-encoded JSON, validated with the same schema as YAML.
Remote metadata is never used as local tree authority.

`up` reconnects to a live root, recovers its tree, and reconnects surviving child
servers. It only launches missing terminals or recreates missing servers.
Launch markers distinguish an unstarted window from a launched window and an
interrupted, uncertain launch. The controller checks pane exit state through
control mode on a half-second maintenance interval, removes exited leaves from
the live tree, and prunes empty ancestor rows. Closing the last terminal ends
the workspace. Internally `remain-on-exit` retains panes until this cleanup,
including commands that finish before startup completes; they are never replayed
merely because they exited. When the controller is stopped, cleanup waits for
its return. Unavailable control streams are not evidence that a terminal exited.

`--name` selects an independent workspace without a YAML file, or overrides the
name in a startup recipe. Each name maps to its own private runtime directory,
controller, and tmux socket namespace, including separate servers on remote
hosts. Dumps include the selected name. Reusing a name reconnects to that live
tree; distinct names do not share terminal state.

Adding/removing nodes changes live metadata. Dumping exports that tree with
best-effort cwd observations. It preserves the configured command, rather than
guessing how to reconstruct an arbitrary running process. No restore plugin or
shell-history scraping is involved. Node IDs remain stable when titles change.
Leaves without an explicit title enable tmux's native `automatic-rename`, with
its default format and process detection on the host running that leaf. Branch
names and explicitly assigned titles stay fixed. Export records explicit titles
even when they equal the node ID, and omits automatic program names so they
remain automatic after restoration. Maintenance re-arms native naming so quiet
commands also update after replacing their launcher shell; no process naming
algorithm is implemented in Python.

This is not a transactional distributed system: an SSH failure midway through
an edit can leave part of the requested tree created. The recorded intent lets
`up` reconnect/reconcile; an uncertain launch requires explicit inspection.
Removal and `down` terminate processes and may report an unreachable remote
host. They should not be interpreted as successful remote cleanup on failure.

## Alternatives considered

| Approach | Assessment for this project |
| --- | --- |
| Shell scripts and tmux configuration | Compact, but synthetic navigation keys and child-to-parent control make SSH and security harder to reason about. |
| [tmuxp](https://github.com/tmux-python/tmuxp) or [tmuxinator](https://github.com/tmuxinator/tmuxinator) | Useful declarative workspace launchers. We would still need to implement the nested-row interaction, runtime tree authority, and SSH trust boundary. |
| One tmux server with a synthesized multirow status bar | Reduces local process count, but requires a separate UI for ancestor selection and remote process persistence. It would no longer display the actual nested sessions. |
| [Zellij layouts](https://zellij.dev/documentation/layouts.html) | A different multiplexer/layout model; reproducing these rows and cross-host navigation adds substantial integration work. |
| [WezTerm multiplexing](https://wezterm.org/multiplexing.html) | Offers local/remote multiplexing but ties this workflow to a particular terminal frontend and its UI model. |
| A custom terminal emulator/multiplexer | Maximum UI control, with a much larger terminal compatibility and security burden. |

These are design judgments for this UI, not claims that the alternatives cannot
be extended. The chosen approach retains tmux's terminal rendering and process
lifetime management, at the cost of one server per row, nested terminal
attachments, and one SSH control connection per remote row. Initial limits are
32 levels, 1024 nodes, 60 KB of encoded live metadata, and 4 KB per RPC request.
