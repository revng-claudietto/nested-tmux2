import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .config import Error, discover, load, parse
from .control import clean_env
from .runtime import Tree, launcher
from .service import private_directory, request, serve, state_path


def start(config, config_path, state, ssh_config):
    private_directory(state.parent)
    private_directory(state)
    try:
        request(state, "status")
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    else:
        return request(state, "reconcile")
    args = launcher("_serve")
    if config_path is not None:
        args.append(str(config_path))
    args += ["--runtime-dir", str(state.parent), "--name", config.name]
    if ssh_config:
        args += ["--ssh-config", str(ssh_config)]
    with (state / "controller.log").open("w") as log:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=clean_env(),
            start_new_session=True,
        )
    deadline = time.monotonic() + max(30, 20 * sum(n.branch for n in config.root.walk()))
    while time.monotonic() < deadline:
        try:
            result = request(state, "status")
            return result
        except (FileNotFoundError, ConnectionRefusedError):
            if process.poll() is not None:
                raise Error(
                    (state / "controller.log").read_text().strip() or "controller failed to start"
                )
            time.sleep(0.05)
    process.terminate()
    raise Error(f"controller startup timed out; see {state / 'controller.log'}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # The workspace selector is valid before or after the subcommand. With
    # only options (or no arguments), attach is the default operation.
    selector = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    selector.add_argument("--name", "-n", help="workspace name (default: default or YAML name)")
    selection, argv = selector.parse_known_args(argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in {"-h", "--help"}):
        argv.insert(0, "attach")
    parser = argparse.ArgumentParser(
        description="Terminal trees, locally and over SSH. With no command, attach to a workspace.",
        parents=[selector],
    )
    sub = parser.add_subparsers(dest="op", required=True)
    for op in ("up", "attach", "status", "down", "stop", "check", "dump", "_serve"):
        p = sub.add_parser(op, parents=[selector])
        p.add_argument(
            "config",
            type=Path,
            nargs="?",
            help="YAML initialization (default: look up workspace name in XDG config directories)",
        )
        p.add_argument("--runtime-dir", type=Path)
        p.add_argument("--ssh-config", type=Path)
        if op == "up":
            p.add_argument("--attach", action="store_true")
        if op == "dump":
            p.add_argument("-o", "--output", type=Path)
    for op in ("add-terminal", "add-row", "remove", "rename"):
        p = sub.add_parser(op, parents=[selector])
        p.add_argument(
            "config",
            type=Path,
            nargs="?",
            help="YAML initialization (default: look up workspace name in XDG config directories)",
        )
        p.add_argument("--runtime-dir", type=Path)
        if op in {"remove", "rename"}:
            p.add_argument("path", help="tree path, such as work/project/build")
            if op == "rename":
                p.add_argument("title")
        else:
            p.add_argument("id")
            p.add_argument("--parent", help="parent row path; defaults to the active row")
            p.add_argument("--cwd")
            p.add_argument("--title")
            if op == "add-row":
                p.add_argument("--host")
            else:
                p.add_argument("--command")
    nav = sub.add_parser("nav", parents=[selector])
    nav.add_argument("config", type=Path, nargs="?")
    nav.add_argument("action", choices=("up", "down", "left", "right", "new", "last", "copy"))
    nav.add_argument("--runtime-dir", type=Path)
    rpc = sub.add_parser("_rpc")
    rpc.add_argument("state", type=Path)
    rpc.add_argument("action")
    prompt = sub.add_parser("_prompt")
    prompt.add_argument("state", type=Path)
    prompt.add_argument("action", choices=("row", "ssh-row", "rename"))
    if argv[0] in sub.choices:
        # Keep flags valid between the optional YAML file and required targets.
        args = sub.choices[argv[0]].parse_intermixed_args(argv[1:])
        args.op = argv[0]
    else:
        args = parser.parse_args(argv)
    try:
        if args.op == "_rpc":
            request(args.state, "nav", action=args.action)
            return 0
        if args.op == "_prompt":
            if args.action in {"row", "ssh-row"}:
                ident = input("New row id (letters, digits, _ or -): ").strip()
                if ident:
                    node = {"id": ident, "children": [{"id": "shell"}]}
                    if args.action == "ssh-row":
                        host = input("SSH host alias (empty to cancel): ").strip()
                        if not host:
                            return 0
                        node["host"] = host
                        node["cwd"] = input("Remote cwd [~]: ").strip() or "~"
                        print("Opening SSH branch...", flush=True)
                    request(args.state, "add", node=node)
            else:
                title = input("New title: ").strip()
                if title:
                    request(args.state, "rename", title=title)
            return 0
        name = selection.name
        config_path = args.config
        if config_path is None:
            name = name if name is not None else "default"
            config_path = discover(name)
        config = (
            load(config_path, name=name)
            if config_path is not None
            else parse(
                {
                    "name": name,
                    "root": {"id": "work", "cwd": os.getcwd(), "children": [{"id": "shell"}]},
                }
            )
        )
        state = state_path(config.name, args.runtime_dir)
        if args.op == "check":
            print(f"{config.name}: valid ({sum(n.branch for n in config.root.walk())} rows)")
            return 0
        ssh_config = getattr(args, "ssh_config", None)
        if ssh_config:
            ssh_config = ssh_config.resolve()
        if args.op == "_serve":
            serve(config, state, ssh_config)
            return 0
        if args.op in {"up", "attach"}:
            result = start(
                config,
                config_path.resolve() if config_path is not None else None,
                state,
                ssh_config,
            )
            if args.op == "attach" or args.attach:
                # A dedicated root server, never the invoking user's current server.
                attach = ["tmux", "-L", result["rows"][0]["socket"], "attach-session", "-t", "tree"]
                os.execvpe(attach[0], attach, clean_env())
        elif args.op == "nav":
            result = request(state, "nav", action=args.action)
        elif args.op in {"add-terminal", "add-row"}:
            node = {"id": args.id}
            for field in ("cwd", "title", "host", "command"):
                value = getattr(args, field, None)
                if value is not None:
                    node[field] = value
            if args.op == "add-row":
                node["children"] = [{"id": "shell"}]
            result = request(state, "add", parent=args.parent, node=node)
        elif args.op == "remove":
            result = request(state, "remove", path=args.path)
        elif args.op == "rename":
            result = request(state, "rename", path=args.path, title=args.title)
        elif args.op == "dump":
            result = yaml.safe_dump(request(state, "dump"), sort_keys=False, allow_unicode=True)
            if args.output:
                args.output.write_text(result)
            else:
                print(result, end="")
            return 0
        elif args.op == "down":
            try:
                result = request(state, "down")
            except (FileNotFoundError, ConnectionRefusedError):
                tree = Tree(config, state, ssh_config)
                try:
                    tree.recover(create=False)
                    tree.down()
                    result = {"stopped": True}
                finally:
                    tree.close()
        else:
            result = request(state, args.op)
        print(json.dumps(result, indent=2))
        return 0
    except (Error, OSError, subprocess.TimeoutExpired) as exc:
        # Remote diagnostics are data, never terminal control sequences.
        diagnostic = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(exc))
        print(f"nested-tmux: {diagnostic}", file=sys.stderr)
        if args.op == "_prompt":
            try:
                input("Press Enter to close.")
            except (EOFError, KeyboardInterrupt):
                pass
        return 1
