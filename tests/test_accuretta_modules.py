"""Every module split out of bridge.py imports cleanly and uses no undefined names.

A function moved out of bridge.py can still refer to a name that bridge.py set
inside a `try:` block (an optional import such as `dpkt`). The move tools didn't
see those at first, and the moved code then failed only at runtime, in a path no
other test covered. pyflakes finds such names statically.
"""
import ast
import importlib
import unittest
from pathlib import Path

import bridge  # noqa: F401  (loads the package the normal way first)

ROOT = Path(bridge.ROOT)
MODULES = sorted(p for p in (ROOT / "accuretta").rglob("*.py"))

# Lower layers never import higher ones (the split plan, section 5.1). The
# red-team package sits between runtime and tools.
LAYERS = {"core": 0, "security": 1, "runtime": 2, "redteam": 3, "tools": 4, "agent": 5, "server": 6}


def _imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name


def _imported_modules_top(tree):
    """Modules imported at module level (what runs at import time)."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name


class AccurettaModuleTests(unittest.TestCase):
    def test_every_module_imports(self):
        for path in MODULES:
            name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_no_undefined_names(self):
        try:
            from pyflakes import api, messages, reporter
        except ImportError:
            self.skipTest("pyflakes isn't installed")
        problems = []

        class Collect(reporter.Reporter):
            def __init__(self):
                pass

            def flake(self, message):
                if isinstance(message, (messages.UndefinedName, messages.UndefinedExport,
                                        messages.UndefinedLocal)):
                    problems.append(str(message))

            def syntaxError(self, filename, msg, lineno, offset, text):
                problems.append(f"{filename}:{lineno}: syntax error: {msg}")

            def unexpectedError(self, filename, msg):
                problems.append(f"{filename}: {msg}")

        for path in MODULES:
            api.checkPath(str(path), Collect())
        self.assertEqual(problems, [])

    def test_no_lookups_by_string(self):
        # globals() finds module names by string at runtime. In bridge.py that
        # worked; once _key_files moved, globals().get("_MCP_CONFIG_FILE") found
        # nothing and a redirected MCP key file was no longer refused. Use plain
        # imported names, which bridge_patch and the test setup also reach.
        # (locals() inside a function sees its own variables, which is fine.)
        found = []
        for path in MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "globals"):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} globals()")
        self.assertEqual(found, [])

    def test_no_import_cycles(self):
        # Two modules importing each other only work while the first one has
        # already defined what the second needs; a different import order
        # (a test, a new caller) then fails with "partially initialized module".
        # Appending helpers to an existing module made two such loops on
        # 2026-11-24; both were caught only because bridge.py stopped importing.
        graph = {}
        for path in MODULES:
            name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            tree = ast.parse(path.read_text(encoding="utf-8"))
            graph[name] = {m for _, m in _imported_modules_top(tree) if m.startswith("accuretta.")}
        state, loops = {}, []

        def visit(v, trail):
            state[v] = 1
            for w in sorted(graph.get(v, ())):
                if w not in graph:
                    continue
                if state.get(w) == 1:
                    loops.append(" -> ".join(trail[trail.index(w):] + [w]))
                elif w not in state:
                    visit(w, trail + [w])
            state[v] = 2
        for v in sorted(graph):
            if v not in state:
                visit(v, [v])
        self.assertEqual(loops, [])

    def test_no_late_changes_to_shared_objects(self):
        # bridge.py kept `_RT_RECON_TOOL_NAMES |= {"observe_response"}` after
        # the set moved out. The module built _RED_TEAM_TOOL_NAMES from the set
        # while loading, before that line ran, so the red-team set lost
        # observe_response (2026-11-24). Code that changes an object another
        # module owns, while loading, runs in a different order once things
        # move. Only agent/tool_table.py filling the shared tool registry is
        # allowed (bridge.py imports it at the point it used to fill the table).
        mutators = {"update", "add", "append", "extend", "setdefault", "insert", "discard",
                    "remove", "pop", "clear", "difference_update", "intersection_update"}

        def base(expr):
            while isinstance(expr, (ast.Subscript, ast.Attribute)):
                expr = expr.value
            return expr.id if isinstance(expr, ast.Name) else None

        found = []
        for path in MODULES + [ROOT / "bridge.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {a.asname or a.name for node in tree.body if isinstance(node, ast.ImportFrom)
                        for a in node.names}
            for node in tree.body:
                targets = []
                if isinstance(node, ast.AugAssign):
                    targets = [node.target]
                elif isinstance(node, ast.Assign):
                    targets = [t for t in node.targets if isinstance(t, (ast.Subscript, ast.Attribute))]
                elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in mutators):
                    targets = [node.value.func.value]
                for t in targets:
                    name = base(t)
                    table = path == ROOT / "accuretta" / "agent" / "tool_table.py"
                    if name in imported and not (table and name == "TOOLS"):
                        found.append(f"{path.relative_to(ROOT)}:{node.lineno} changes {name}")
        self.assertEqual(found, [])

    def test_observe_response_is_a_red_team_tool(self):
        self.assertIn("observe_response", bridge._RT_RECON_TOOL_NAMES)
        self.assertIn("observe_response", bridge._RED_TEAM_TOOL_NAMES)

    def test_no_paths_from_own_file(self):
        # __file__ names whichever file the code sits in. bridge.py's guard
        # took its own path from it; moved into a module it would have guarded
        # that module instead of bridge.py. Only core/paths.py works out where
        # the app lives; everything else builds on its ROOT.
        allowed = {ROOT / "accuretta" / "core" / "paths.py"}
        found = []
        for path in MODULES:
            if path in allowed:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name) and node.id == "__file__":
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [])

    def test_layers_only_import_downward(self):
        problems = []
        for path in MODULES:
            parts = path.relative_to(ROOT / "accuretta").parts
            if len(parts) < 2:
                continue
            own = parts[0]
            self.assertIn(own, LAYERS, f"{path}: unknown layer folder {own!r}")
            for lineno, module in _imported_modules(path):
                if module == "bridge" or module.startswith("bridge."):
                    problems.append(f"{path.name}:{lineno} imports bridge")
                bits = module.split(".")
                if bits[0] == "accuretta" and len(bits) > 1 and bits[1] in LAYERS:
                    if LAYERS[bits[1]] > LAYERS[own]:
                        problems.append(f"{own}/{path.name}:{lineno} imports the higher layer {module}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
