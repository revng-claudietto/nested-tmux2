"""Allow tmux bindings and the daemon to work from a source checkout at any cwd."""

if __package__ in (None, ""):
    import json
    import sys

    sys.path[:0] = json.loads(sys.argv.pop(1))

from nested_tmux.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
