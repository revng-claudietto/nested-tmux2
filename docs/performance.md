# Navigation performance

The Nix package uses a small C socket client for keyboard actions. The Python
daemon remains running and retains its tmux and SSH control connections.

Measured on 2026-11-15 on the same x86_64 Linux machine with installed Nix
packages, tmux 3.8, one local row and two `cat` terminals. Each measurement used
five warm-up iterations and 100 timed samples. The Python baseline is commit
`2dfa304`; the native version uses the same pinned dependencies.

| Operation | Python median | Native median | Python p95 | Native p95 |
| --- | ---: | ---: | ---: | ---: |
| Alt+Right through a real PTY until selection changes | 82.016 ms | 2.385 ms | 85.058 ms | 2.872 ms |
| Installed helper: shell launch through completion | 78.943 ms | 1.540 ms | 82.171 ms | 2.229 ms |
| Direct Unix socket RPC from an existing Python process | 0.421 ms | 0.449 ms | 0.527 ms | 0.510 ms |
| Direct persistent tmux control command | 0.286 ms | 0.269 ms | 0.396 ms | 0.294 ms |

The median keyboard measurement improved by **34.4×**, reducing latency by
**97.1%**. The daemon/control path stayed roughly the same; removing interpreter
startup and imports accounts for most of the improvement. A process and Unix
socket connection are still created for each shortcut. Interactive prompts
continue to run Python.

The keyboard measurement sends an actual Alt+Right escape sequence to a PTY
attachment and observes the selected window through an independent persistent
control connection. Polling sleeps for 1 ms between observations, so these
values include observation delay. They do not measure terminal-emulator screen
rendering. Results are local to this machine; remote SSH round trips, larger
trees, and other controller work can add latency.

## Reproduce

Build the version to measure, then pass its installed executable and matching
tmux to the benchmark:

```sh
nix build . --out-link result
python3 tests/benchmark-navigation.py ./result/bin/nested-tmux \
  --tmux /path/to/the/packages/tmux --samples 100
```

Use the tmux path embedded in `result/bin/nested-tmux`'s Nix wrapper; different
tmux versions may use incompatible server protocols. Run each version
sequentially, avoiding other builds during measurement. The script prints the
actual binding and timings as JSON lines. It uses temporary private runtime
directories and explicit isolated socket names, cleans up its own servers, and
checks the invoking tmux server's pane/process inventory before and after.
