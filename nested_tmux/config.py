"""Strict configuration parsing; no environment expansion on the wrong host."""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml


class Error(Exception):
    """A user-facing configuration, transport, or operation error."""


def string(value, where):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise Error(f"{where}: expected a nonempty string without control characters")
    return value


@dataclass(frozen=True)
class Node:
    path: str
    title: str | None
    host: str
    cwd: str
    env: dict[str, str]
    children: tuple["Node", ...] = ()
    argv: tuple[str, ...] = ()
    shell: str | None = None

    @property
    def branch(self):
        return bool(self.children)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class Config:
    name: str
    root: Node
    digest: str


def as_data(config):
    """Export launch intent; the runtime may additionally refresh leaf working directories."""

    def visit(node, parent=None):
        ident = node.path.rsplit("/", 1)[-1]
        result = {"id": ident}
        if node.title is not None:
            result["title"] = node.title
        if node.host != (parent.host if parent else "local"):
            result["host"] = node.host
        if parent is None or node.cwd != parent.cwd or node.host != parent.host:
            result["cwd"] = node.cwd
        env = {
            key: value
            for key, value in node.env.items()
            if parent is None or parent.env.get(key) != value
        }
        if env:
            result["env"] = env
        if node.branch:
            result["children"] = [visit(child, node) for child in node.children]
        elif node.argv:
            result["command"] = list(node.argv)
        elif node.shell is not None:
            result["command"] = node.shell
        return result

    return {"name": config.name, "root": visit(config.root)}


def workspace_name(value):
    name = string(value, "name")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise Error("name must contain only letters, digits, underscores or hyphens")
    return name


def config_home():
    home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not home.is_absolute():
        home = Path.home() / ".config"
    return home


def add_nodes(saved, live):
    """Preserve saved launch intent; append live nodes missing at each tree path."""
    if saved["id"] != live["id"]:
        raise Error("saved and live root IDs differ; use save --replace to replace the recipe")
    if ("children" in saved) != ("children" in live):
        raise Error(
            f"{saved['id']}: terminal/row conflict; use save --replace to replace the recipe"
        )
    if "children" not in saved:
        return saved
    current = {node["id"]: node for node in live["children"]}
    children = [
        add_nodes(node, current[node["id"]]) if node["id"] in current else node
        for node in saved["children"]
    ]
    known = {node["id"] for node in saved["children"]}
    children.extend(node for node in live["children"] if node["id"] not in known)
    return {**saved, "children": children}


def save(data, *, name, replace=False, source=None):
    """Atomically persist a live tree as a private user startup recipe."""
    workspace_name(name)
    directory = config_home() / "nested-tmux2"
    destination = directory / f"{name}.yaml"
    data = {**data, "name": name}
    if not replace:
        # Prefer the recipe being updated; otherwise seed from the selected
        # explicit/system/.yml recipe without modifying that source file.
        if destination.exists() or destination.is_symlink():
            source = destination
        elif source is None:
            source = discover(name)
        if source is not None:
            saved = read_data(source)
            if isinstance(saved, dict):
                saved = {**saved, "name": name}
            saved_config = parse(saved)
            saved_hosts = {node.path: node.host for node in saved_config.root.walk()}
            for node in parse(data).root.walk():
                if node.path in saved_hosts and saved_hosts[node.path] != node.host:
                    raise Error(
                        f"{node.path}: saved and live hosts differ; "
                        "use save --replace to replace the recipe"
                    )
            data = {**saved, "root": add_nodes(saved["root"], data["root"])}
    parse(data)  # Check the merged tree before touching the destination.
    contents = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=f".{name}-", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def discover(name):
    """Find a workspace recipe in XDG precedence order, never in the cwd."""
    workspace_name(name)  # Validate before constructing a filesystem path.
    directories = [
        config_home(),
        *map(Path, (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(os.pathsep)),
    ]
    for directory in directories:
        if not directory.is_absolute():
            continue
        for extension in ("yaml", "yml"):
            path = directory / "nested-tmux2" / f"{name}.{extension}"
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            # Invalid recipes, dangling symlinks and unreadable files must fail
            # visibly rather than silently starting a different configuration.
            return path
    return None


def parse(data):
    if not isinstance(data, dict) or set(data) - {"name", "root"}:
        raise Error("configuration must contain only name and root")
    name = workspace_name(data.get("name"))

    count = 0

    def node(raw, parent=None, depth=0):
        nonlocal count
        count += 1
        if count > 1024:
            raise Error("maximum tree size is 1024 nodes")
        if depth > 32:
            raise Error("maximum tree depth is 32")
        if not isinstance(raw, dict):
            raise Error("each tree node must be an object")
        if not all(isinstance(key, str) for key in raw):
            raise Error("node field names must be strings")
        unknown = set(raw) - {"id", "title", "host", "cwd", "env", "children", "command"}
        if unknown:
            raise Error(f"unknown node fields: {', '.join(sorted(unknown))}")
        ident = string(raw.get("id"), "id")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", ident):
            raise Error(f"invalid node id: {ident}")
        path = f"{parent.path}/{ident}" if parent else ident
        title = string(raw["title"], f"{path}.title") if "title" in raw else None
        host = string(raw.get("host", parent.host if parent else "local"), f"{path}.host")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", host):
            raise Error(f"{path}: host must be an SSH alias or user@host, not SSH options")
        if not parent and host != "local":
            raise Error("the root must be local; put remote work in a child branch")
        if parent and parent.host != "local" and host != parent.host:
            raise Error(
                f"{path}: a remote subtree must stay on its host; use SSH ProxyJump for routing"
            )
        inherited_cwd = parent.cwd if parent and host == parent.host else "~"
        cwd = string(raw.get("cwd", inherited_cwd), f"{path}.cwd")
        if not (cwd.startswith("/") or cwd == "~" or cwd.startswith("~/")):
            raise Error(f"{path}: cwd must be absolute or start with ~/")
        # tmux expands formats in -c; reject rather than silently reinterpret paths.
        if "#" in cwd:
            raise Error(f"{path}: cwd cannot contain tmux format introducer '#'")
        env = dict(parent.env) if parent else {}
        new_env = raw.get("env", {})
        if not isinstance(new_env, dict):
            raise Error(f"{path}.env must be an object of string values")
        for key, value in new_env.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key in {"TMUX", "TMUX_PANE"}
            ):
                raise Error(f"{path}: invalid or reserved environment name: {key}")
            if not isinstance(value, str) or "\0" in value or "\n" in value or "\r" in value:
                raise Error(f"{path}.env.{key} must be a string without NUL or newlines")
            env[key] = value
        launch = raw.get("command")
        argv = launch if isinstance(launch, list) else []
        shell = launch if isinstance(launch, str) else None
        if "command" in raw and not (argv or shell):
            raise Error(f"{path}.command must be a nonempty string or array of strings")
        for arg in argv:
            if not isinstance(arg, str) or any(c in arg for c in "\0\r\n"):
                raise Error(f"{path}.command must contain strings without NUL or newlines")
        if argv and not argv[0]:
            raise Error(f"{path}.command executable must not be empty")
        if shell is not None and "\0" in shell:
            raise Error(f"{path}.command cannot contain NUL")
        base = Node(path, title, host, cwd, env, argv=tuple(argv), shell=shell)
        if "children" not in raw:
            if parent and host != parent.host:
                raise Error(f"{path}: put host on a branch, so remote terminals persist in tmux")
            return base
        children = raw["children"]
        if not isinstance(children, list) or not children or argv or shell is not None:
            raise Error(f"{path}: branches need nonempty children and cannot have command")
        parsed = tuple(node(child, base, depth + 1) for child in children)
        if len({child.path for child in parsed}) != len(parsed):
            raise Error(f"{path}: duplicate child id")
        return Node(path, title, host, cwd, env, children=parsed)

    root = node(data.get("root"))
    if not root.branch:
        raise Error("root must have children")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return Config(name, root, hashlib.sha256(canonical.encode()).hexdigest())


def read_data(path):
    path = Path(path)
    try:

        class Loader(yaml.SafeLoader):
            pass

        def mapping(loader, node):
            loader.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                if not isinstance(key, str):
                    raise Error("YAML mapping keys must be strings")
                if key in result:
                    raise Error(f"duplicate YAML key: {key}")
                result[key] = loader.construct_object(value_node)
            return result

        Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        if path.stat().st_size > 1024 * 1024:
            raise Error("configuration is larger than 1 MiB")
        return yaml.load(path.read_text(), Loader=Loader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise Error(f"{path}: {exc}") from exc


def load(path, *, name=None):
    data = read_data(path)
    if name is not None and isinstance(data, dict):
        data = {**data, "name": name}
    return parse(data)
