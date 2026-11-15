# nested-tmux2

```sh
nix run github:revng-claudietto/nested-tmux2
```

This opens a workspace with one shell in your current directory. No configuration
file is needed. Run it from a plain terminal, then build your tree as you work:

1. **Ctrl+t** creates a regular terminal tab.
2. **Ctrl+a, Ctrl+s** creates a branch with its own row; enter a name at the prompt.
3. **Alt+Left/Right** switches tabs. **Alt+Up/Down** moves into or out of a branch.
4. Type **exit** or press **Ctrl+d** at a shell prompt to close that terminal.
   Empty branches close automatically; closing the last terminal exits the workspace.
5. **Ctrl+a, x** closes the selected terminal or entire branch after confirmation.
6. **Ctrl+a, d** detaches and keeps everything running. Run the same command to reconnect.

To open an SSH branch, focus a local row and press **Ctrl+a, Ctrl+r**. Enter a
branch name, an SSH host alias, and an optional remote directory (default `~`).
See the SSH setup instructions below for authentication and host-key setup.

Tabs start at **0** and use tmux's automatic names, such as `bash` or `vim`,
following the foreground program. Use **Ctrl+a, A** to give a tab a fixed name.

To open entirely separate workspaces by name:

```sh
nix run github:revng-claudietto/nested-tmux2 -- --name work
nix run github:revng-claudietto/nested-tmux2 -- --name personal
```

Run the same command to reconnect to that named workspace. Omit `--name` to use
`default`. The outer row displays explicit workspace names such as `[work]`;
the default workspace has no name label.
From a local checkout, use `nix run . -- --name work`.
To terminate a whole workspace from another terminal:

```sh
nix run github:revng-claudietto/nested-tmux2 -- down --name work
```

Each branch has a tmux server and each child is a tab. A branch tab displays its
child server, so the rows stay visible. A local controller handles navigation
through a private Unix socket and tmux control mode.

## Demo

[![Terminal tree navigation, creating tabs and branches, closing a branch, and exporting YAML](https://github.com/revng-claudietto/nested-tmux2/releases/download/demo/demo.gif)](https://github.com/revng-claudietto/nested-tmux2/releases/tag/demo)

[Download MP4](https://github.com/revng-claudietto/nested-tmux2/releases/download/demo/demo.mp4)
· [Asciinema recording](https://github.com/revng-claudietto/nested-tmux2/releases/download/demo/demo.cast)

## Run

The flake pins **nixos-unstable** in `flake.lock` and includes Python, PyYAML, tmux,
OpenSSH, and a small native keyboard helper. Linux on x86_64 and aarch64 is
supported by the flake; the test run used x86_64. From this directory:

```sh
nix run path:.
```

Alternatively, install with `python3 -m pip install .` in a virtual environment.
This needs Python 3.11+, tmux 3.4+, and the OpenSSH client on PATH. No changes to
`~/.tmux.conf` are necessary. Workspace servers use dedicated socket names and
ignore the user's tmux configuration.

Nix uses the native `nested-tmux-key` helper to avoid starting Python for each
navigation shortcut. Source/pip installations use the Python fallback unless
that helper is on PATH; build it separately with `nix build .#key-helper`.
See [performance measurements](docs/performance.md).

Attach from a plain terminal for the intended keyboard behavior: an enclosing
tmux may intercept the same shortcuts. Starting a workspace inside another tmux
does not modify that outer server.

## Keys

| Key | Action in the active row |
| --- | --- |
| Alt+Left / Alt+Right | Previous / next tab |
| Alt+Up | Enter the selected child row |
| Alt+Down | Return to the parent row |
| Ctrl+t or Ctrl+a, Ctrl+c | New regular terminal |
| Ctrl+a, Ctrl+s | New child row; prompt for its ID |
| Ctrl+a, Ctrl+r | New SSH child row; prompt for ID, host alias, and remote cwd |
| Ctrl+a, Ctrl+a | Last tab |
| Ctrl+a, A | Rename the selected terminal or child row |
| Ctrl+a, x | Remove the selected terminal or entire child row, with confirmation |
| Ctrl+a, d | Detach; processes keep running |
| Ctrl+a, [ | Copy mode in the active row |
| Ctrl+a, a | Send a literal Ctrl+a to the terminal |

The active row's selected tab is green; other rows' selected tabs are gray.
Ordinary typing reaches the deepest visible terminal, as with nested tmux.
The local outer server owns the shortcuts even when the active row is remote.

To close a branch, press **Alt+Down** to focus its parent row, keep that branch
selected, then press **Ctrl+a, x** and confirm with **y**. This terminates every
terminal and nested row inside it. From the CLI, use
`nested-tmux remove work/project`. Use `down` to close the root workspace.
Removing a row's last terminal also closes that row and any empty ancestors.

With `nested-tmux` on PATH, you can also manage the default workspace by hand:

```sh
nested-tmux add-row project --parent work
nested-tmux add-terminal logs --parent work/project --command 'tail -f application.log'
nested-tmux rename work/project 'My project'
nested-tmux remove work/project
nested-tmux down
```

With Nix, pass those arguments after `--`, for example
`nix run github:revng-claudietto/nested-tmux2 -- add-row project --parent work`.

Add `--name work` to any command to select a named workspace:

```sh
nested-tmux add-row project --name work
nested-tmux dump --name work -o saved-work.yaml
nested-tmux status --name personal
nested-tmux down --name work
```

## Optional YAML initialization

To initialize a named workspace automatically after reboot, save its recipe as
`~/.config/nested-tmux2/work.yaml`, then start it as usual:

```sh
nix run github:revng-claudietto/nested-tmux2 -- --name work
```

Lookup follows the [XDG Base Directory specification](https://specifications.freedesktop.org/basedir/latest/):
`$XDG_CONFIG_HOME/nested-tmux2/` first (default `~/.config/nested-tmux2/`),
then `nested-tmux2/` under each directory in `$XDG_CONFIG_DIRS` (default `/etc/xdg`).
Relative XDG paths are ignored. Within each directory, `<name>.yaml` takes
precedence over `<name>.yml`; the first matching file wins, without merging.
Without `--name`, the filename is `default.yaml` (or `default.yml`). No matching
file means one shell in the current directory, as before.

For discovered files, `name` is optional: the selected workspace name takes
precedence over any name in the file. Invalid recipes report an error.
An explicit YAML path overrides automatic lookup:

```sh
nix run github:revng-claudietto/nested-tmux2 -- up work.yaml --attach
```

For example, save this as `~/.config/nested-tmux2/work.yaml` for automatic
lookup, or as `work.yaml` for the explicit command above, and adjust the project directory:

```yaml
name: development
root:
  id: work
  cwd: ~/
  children:
    - id: shell
    - id: project
      cwd: /home/me/src/project
      children:
        - id: editor
          command: [vi, README.md]
        - id: build
          command: |
            make
            exec bash -l
        - id: tools
          children:
            - id: shell
            - id: monitor
              command: top
```

Edit paths and commands for your machine. A node with `children` is a row; a leaf
is a terminal. Leaves without `command` launch the host's default login shell.
An array launches an executable with literal arguments. A string runs through
`/bin/sh -c`; use `exec bash -l` at its end if you want an interactive shell
afterward. A terminal closes when its command finishes.

`cwd` and `env` inherit from the parent. Paths must be absolute or start with
`~/`; `~` is resolved on the machine that runs the terminal. Crossing to another
host resets the default cwd to that host's home. `env` is a mapping of strings.
`title` sets a fixed displayed label without changing the stable `id`. Without
`title`, terminals use tmux's automatic naming and branches display their IDs.
IDs and workspace names use letters, digits, underscores, and hyphens. Initial rows must
have at least one child; empty rows are removed automatically at runtime.

`name` identifies the workspace within the runtime directory. When its root tmux
server already exists, **the live tree wins**: editing YAML and running `up`
again does not undo runtime changes or rerun surviving commands. When the
controller restarts it reads the tree from the local root server. After a reboot
or `down`, YAML supplies the initial tree again.
`--name` overrides the YAML name, so `nested-tmux up work.yaml --name experiment`
initializes a separate workspace from the same recipe. Subsequent commands can
select it with `--name experiment` without supplying the YAML file.

## Edit and export at runtime

To use the current tree as the next startup recipe for a named workspace:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/nested-tmux2"
nix run github:revng-claudietto/nested-tmux2 -- dump --name work \
  -o "${XDG_CONFIG_HOME:-$HOME/.config}/nested-tmux2/work.yaml"
```

To save a workspace you built by hand:

```sh
nix run github:revng-claudietto/nested-tmux2 -- dump -o saved-work.yaml
```

For a workspace initialized from a YAML file, supply that file to select it:

```sh
nested-tmux add-terminal work.yaml logs --parent work/project \
  --cwd /home/me/src/project --command 'tail -f application.log'
nested-tmux add-row work.yaml scratch --parent work
nested-tmux rename work.yaml work/scratch 'Scratch work'
nested-tmux remove work.yaml work/project/logs
nested-tmux dump work.yaml -o saved-work.yaml
```

Omit `--parent` to use the active row. `add-row` creates a row with one shell;
it also accepts `--host` and `--cwd`. Removing a row terminates its subtree.
Use the keyboard shortcuts for ordinary tab/row creation and removal.

Export includes managed rows and terminals, IDs, titles, hosts, environments,
configured launch commands, and each terminal's current cwd when available.
This includes all managed remote branches: their SSH host aliases and cwd values
queried from tmux on the remote host.
Only explicitly assigned titles are exported; automatic names remain automatic
when the tree is restored. Generated IDs such as `shell-2` identify terminals
in CLI paths and YAML independently of their displayed program names.
Unavailable cwd values fall back to the launch cwd. Export does not recover a
program manually launched inside an interactive shell, shell variables, process
memory, scrollback, split panes, or the current selection. It does not discover
tabs created using unmanaged native tmux commands. Use the provided keys/CLI
for changes you want to export. Keep exported files private if `env` or commands
contain secrets; they are written as ordinary files subject to your umask.

Run `up saved-work.yaml` after reboot to initialize from the exported tree.
It retains the same workspace name, so it attaches to the existing workspace
while that workspace is still alive. Use a different name for an independent copy.

## SSH: what to do

1. Install tmux 3.4+ on the remote host. It needs a POSIX shell, `env`, and `sleep`;
   it does **not** need this Python application.
2. Configure an ordinary SSH alias and key-based authentication locally:

   ```sshconfig
   Host devbox
       HostName server.example.org
       User me
       IdentityFile ~/.ssh/id_ed25519
       # ProxyJump bastion
   ```

3. Run `ssh devbox 'tmux -V'` once, verify the host key, and confirm login works.
   Connections made by nested-tmux use batch mode, so they cannot prompt for
   passwords or unknown host keys. A local SSH agent can authenticate the
   connection; it is not forwarded.
4. Focus a local row and press **Ctrl+a, Ctrl+r**. Enter a branch name such as
   `server`, the SSH alias `devbox`, and a remote directory such as `~/src/project`
   (leave it empty for the remote home directory). The new branch is selected
   automatically. The same operation is available from the CLI:

   ```sh
   nested-tmux add-row server --parent work --host devbox --cwd '~/src/project'
   ```

   You can optionally initialize the same arrangement from YAML:

   ```yaml
   name: remote-work
   root:
     id: work
     children:
       - id: local
       - id: server
         host: devbox
         cwd: ~/src/project
         children:
           - id: shell
           - id: monitor
             command: top
   ```

   ```sh
   nested-tmux up remote.yaml --attach
   ```

Quote remote paths containing `~` to prevent your local shell expanding them.

All SSH invocations for the same workspace and host alias share **one underlying
SSH connection**, using a private OpenSSH `ControlMaster`. No SSH multiplexing
configuration is needed. The connection stays open while any channel is in use,
and for **five minutes after its last channel closes** (`ControlPersist=300`).
Different workspace names and host aliases use separate masters.

Each remote row has a persistent control channel (`ssh -T` running `tmux -C`).
Each local-to-remote branch has a terminal channel (`ssh -tt` attaching to tmux).
Deeper rows on that same host attach locally there. Cleanup commands also reuse
the master. Pressing **Alt+Right** starts the small helper **locally**, which
writes to the local controller socket. The controller writes `next-window` to
the existing remote control channel: no new SSH connection, SSH channel, or
remote process is started for that key press.

After a connection drops, the controller reconnects in the background and the
terminal attachment retries automatically. Failed control attempts back off
from one second to a maximum of 30 seconds. Remote commands stay in tmux;
recovery reattaches to surviving servers and never replays interrupted commands.
Local rows remain available during background connection attempts. If the
remote tmux server itself has disappeared (for example after a remote reboot),
run `nested-tmux up` to explicitly recreate it. For a YAML workspace, pass its
file to `up`. Keep the workspace name and local runtime directory stable to
reconnect to the same remote servers.

OpenSSH's server-side `MaxSessions` setting limits simultaneous channels on one
connection; its default is 10. Count one channel per remote row, plus one per
local-to-remote branch, and leave room for temporary cleanup/recovery channels.
For larger trees, increase `MaxSessions` on that SSH server. Reaching the limit
reports an error; the application does not silently open extra connections.

Remote descendants stay on their branch's host. To use another host, create
another branch under a local row. Use `ProxyJump` for network routing. Pass
`--ssh-config /path/to/config` when starting the controller if you need a
separate SSH configuration; stop and restart the controller to change it.

## Lifecycle and limits

```sh
nested-tmux status
nested-tmux stop       # Stop only the controller; keep all terminals.
nested-tmux up         # Reconnect; recreate missing servers if needed.
nested-tmux down       # Terminate this workspace, including remote rows.
```

These commands use the workspace named `default`. Pass `--name work` to select
another workspace, or supply a YAML file to use the name in that file. Names
select independent controllers, local and remote tmux servers, and live trees.

The running controller removes exited terminals and empty branches, including
over SSH. If you deliberately stop the controller, exited panes are kept until
you restart it with `up`. A lost connection alone never counts as a terminal exit.
To pick up a new application version for an existing workspace, run `stop`, then
`up` or `attach`, with the same `--name`; surviving terminals keep running.

The runtime socket/log directory lives under `$XDG_RUNTIME_DIR` or a private
`/tmp/nested-tmux-UID` directory. `--runtime-dir /private/path` overrides the base;
use the same base for subsequent operations. SSH master socket paths must fit
88 bytes and must not contain `%`, `$`, or `~`; use a short runtime directory if
necessary. Navigation focus is shared by
clients attached to the same workspace.

If a transport fails during a command launch, the command may already have run.
The controller records an uncertain startup and refuses to replay it. Inspect
that terminal before explicitly using `down` and recreating the workspace.
This is process persistence across disconnects, not process checkpointing
across reboots. Export YAML before reboot if runtime edits should be retained.

The current scope is managed tabs and nested rows, not arbitrary tmux layouts.
See [design alternatives](docs/design.md) and the [security review](docs/security.md).

## Tests

```sh
nix flake check path:.
# Or run the suite directly:
nix develop path:. --command python3 tests/run-local.py
```

Tests use unique socket names, temporary private directories, and a loopback
OpenSSH server with temporary keys and strict host-key checks. No system sshd or
tmux configuration is changed. The runner checks that the invoking tmux server's
pane/process inventory remains unchanged. In a Nix build, a test-only NSS
wrapper gives the build user a valid login shell without editing system accounts.

Coverage includes real PTY keyboard input and visible rows, command quoting,
runtime edits, named workspace isolation, shell exit and empty-branch cleanup,
export and reinitialization, controller restart without duplicate
launches, SSH reconnection, connection sharing and idle expiry, SSH channel limits,
hostile terminal output, forwarding restrictions,
bounded RPC/control messages, and the installed Nix executable and helpers.
Native helper tests cover partial replies, malformed/oversized replies, missing
controllers, sanitized diagnostics, and timeouts without retrying actions. The
source test runner compiles the helper with `cc` if it is not already on PATH;
the Nix development shell supplies it and the compiler.

## Build the demo

```sh
nix build path:.#demo --out-link demo-result
```

The derivation records the installed application with asciinema, renders
`demo.gif` with agg and a pinned font, and encodes `demo.mp4` with FFmpeg. The
`demo.cast` file can be replayed with `asciinema play demo-result/demo.cast`.
Recording uses a private HOME, runtime directory, and isolated tmux sockets;
it exercises real navigation, tab/branch creation, branch removal, and YAML export.
Dependencies and the script are pinned; PTY scheduling can change recording timing.

The GitHub workflow runs tests and builds the demo in an Ubuntu container. It
installs Nix with the official single-user installer and uses no external actions,
CI cache, or Cachix integration. Standard Nix binary downloads remain enabled.
Successful builds on `main` update the dedicated `demo` prerelease with the GIF,
MP4, asciicast, checksums, and source commit. Pull requests build and test without
publishing. The README image uses the stable release-asset URL.
