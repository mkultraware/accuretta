import tempfile
from pathlib import Path
import bridge
from _patching import bridge_patch


def sandbox_user_files(testcase):
    """Redirect bridge's user-facing stores to a temp dir for this test.
    The suite writes real settings.json / chats.json through live endpoints —
    this keeps tests from damaging an actual install. bridge_patch also reaches
    the moved save/load code in accuretta.core, which keeps its own copy of the
    names."""
    tmp = Path(tempfile.mkdtemp())
    sf = tmp / "settings.json"
    sf.write_text('{}', encoding='utf-8')
    cf = tmp / "chats.json"
    cf.write_text('{"chats": {}, "order": []}', encoding='utf-8')
    testcase._orig_sf = bridge.SETTINGS_FILE
    testcase._orig_cf = bridge.CHATS_FILE
    for name, path in (("SETTINGS_FILE", sf), ("CHATS_FILE", cf)):
        patcher = bridge_patch(name, path)
        patcher.start()
        testcase.addCleanup(patcher.stop)
