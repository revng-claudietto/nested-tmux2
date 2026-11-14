# nested-tmux2

```sh
nix run github:revng-claudietto/nested-tmux2
```

This opens a workspace with one shell in your current directory. No configuration
file is needed. Run it from a plain terminal, then build your tree as you work:

1. **Ctrl+t** creates a regular terminal tab.
2. **Ctrl+a, Ctrl+s** creates a branch with its own row; enter a name at the prompt.
3. **Alt+Left/Right** switches tabs. **Alt+Up/Down** moves into or out of a branch.
4. **Ctrl+a, x** closes the selected terminal or branch after confirmation.
5. **Ctrl+a, d** detaches. Run the same command again to reconnect.

Each branch has a tmux server and each child is a tab. A branch tab displays its
child server, so the rows stay visible. A local controller handles navigation
through a private Unix socket and tmux control mode.

## Demo

[![Terminal tree navigation, creating tabs and branches, closing a branch, and exporting YAML](https://github.com/revng-claudietto/nested-tmux2/releases/download/demo/demo.gif)](https://github.com/revng-claudietto/nested-tmux2/releases/tag/demo)

[Download MP4](https://github.com/revng-claudietto/nested-tmux2/releases/download/demo/demo.mp4)
· [Asciinema recording](https://github.com/revng-claudietto/nested-tmux2/releases/download/demo/demo.cast)

## Run

The flake pins **nixos-unstable** in `flake.lock` and includes Python, PyYAML, tmux,
and OpenSSH. Linux on x86_64 and aarch64 is supported by the flake; the test run
used x86_64. From this directory:

```sh
nix run path:.
```

Alternatively, install with `python3 -m pip install .` in a virtual environment.
This needs Python 3.11+, tmux 3.4+, and the OpenSSH client on PATH. No changes to
`~/.tmux.conf` are necessary. Workspace servers use dedicated socket names and
ignore the user's tmux configuration.

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
A parent row must retain at least one child; otherwise close that parent row.

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

## Optional YAML initialization

If you want a specific tree after reboot, you can initialize it from YAML:

```sh
nix run github:revng-claudietto/nested-tmux2 -- up work.yaml --attach
```

For example, save this as `work.yaml` and adjust the project directory:

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
afterward. Finished commands stay visible until you remove their terminal.

`cwd` and `env` inherit from the parent. Paths must be absolute or start with
`~/`; `~` is resolved on the machine that runs the terminal. Crossing to another
host resets the default cwd to that host's home. `env` is a mapping of strings.
`title` changes the displayed label without changing the stable `id`. IDs and
workspace names use letters, digits, underscores, and hyphens. A row must have
at least one child; remove the whole row to remove its last terminal.

`name` identifies the workspace within the runtime directory. When its root tmux
server already exists, **the live tree wins**: editing YAML and running `up`
again does not undo runtime changes or rerun surviving commands. When the
controller restarts it reads the tree from the local root server. After a reboot
or `down`, YAML supplies the initial tree again.

## Edit and export at runtime

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
4. Add a remote branch to your running workspace:

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

The controller opens an SSH connection for each remote row's control stream;
the visible remote subtree uses a separate SSH terminal connection. Deeper rows
on that same host attach locally there. Alt+Up/Down and tab creation work as
usual. Remote commands remain in remote tmux when the visible SSH connection
drops. The terminal attachment retries automatically; run `nested-tmux up`
again to reconnect control streams after a failure. Surviving processes are not
restarted. Keep `name` and the local runtime directory stable to reconnect to
the same remote servers. For a YAML workspace, pass its file to `up`.

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

These commands use the workspace named `default`. Supply a YAML file after the
command to select the workspace named by that file.

The runtime socket/log directory lives under `$XDG_RUNTIME_DIR` or a private
`/tmp/nested-tmux-UID` directory. `--runtime-dir /private/path` overrides the base;
use the same base for subsequent operations. Navigation focus is shared by
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
runtime edits, export and reinitialization, controller restart without duplicate
launches, SSH reconnection, hostile terminal output, forwarding restrictions,
bounded RPC/control messages, and the installed Nix executable and helpers.

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
