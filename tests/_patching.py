"""bridge_patch: replace a name on `bridge` and on every Accuretta module that has it.

bridge.py is being split into an `accuretta` package, one area at a time, with
bridge.py re-exporting every moved name. A test that patches only `bridge.x`
would then miss callers inside the new module, which look `x` up in their own
namespace, and silently test the wrong thing. bridge_patch puts the same
replacement everywhere the name lives.

It is a drop-in for `patch.object(bridge, ...)`:

    with bridge_patch("_approval_mode", return_value="soft"):
        ...
    patcher = bridge_patch("_workspace_roots", lambda: {root})
    patcher.start()
    self.addCleanup(patcher.stop)
"""
import sys
from unittest import mock

import bridge

# Keyword arguments that create the replacement. Every target after the first
# gets the very same object instead of a second, separate one.
_CREATING = ("return_value", "side_effect", "spec", "spec_set", "autospec", "new_callable", "wraps")


class _MultiPatch:
    def __init__(self, name, new=mock.DEFAULT, **kwargs):
        if not isinstance(name, str):
            raise TypeError("bridge_patch(name, ...) takes the attribute name as a string")
        self.name, self.new, self.kwargs = name, new, kwargs
        self._patchers = []

    def _targets(self):
        # bridge first when it has the name. A value a moved module owns and
        # rebinds is deliberately not re-exported from bridge (the copy would go
        # stale), so then only the module itself is patched. With no target at
        # all, patching bridge raises the usual AttributeError.
        modules = [module for mod_name, module in list(sys.modules.items())
                   if (module is not None and module is not bridge
                       and (mod_name == "accuretta" or mod_name.startswith("accuretta."))
                       and self.name in getattr(module, "__dict__", {}))]
        if hasattr(bridge, self.name) or not modules:
            return [bridge] + modules
        return modules

    def start(self):
        if self._patchers:
            raise RuntimeError(f"bridge_patch({self.name!r}) is already started")
        result = None
        shared = mock.DEFAULT
        try:
            for i, target in enumerate(self._targets()):
                if i == 0:
                    patcher = mock.patch.object(target, self.name, self.new, **self.kwargs)
                    result = patcher.start()
                    self._patchers.append(patcher)
                    shared = getattr(target, self.name)
                else:
                    rest = {k: v for k, v in self.kwargs.items() if k not in _CREATING}
                    patcher = mock.patch.object(target, self.name, shared, **rest)
                    patcher.start()
                    self._patchers.append(patcher)
        except BaseException:
            self.stop()
            raise
        return result

    def stop(self):
        while self._patchers:
            self._patchers.pop().stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def bridge_patch(name, new=mock.DEFAULT, **kwargs):
    """Patch `name` on bridge and on every loaded accuretta.* module that defines it."""
    return _MultiPatch(name, new, **kwargs)
