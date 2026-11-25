"""The sandbox guest stops when the process that started it exits.

Nothing here runs wsl.exe: the launch helpers are patched, and atexit is
replaced so no real exit hook is registered in the test process."""
import unittest
from unittest import mock

import bridge
from _patching import bridge_patch


class SandboxExitStopTests(unittest.TestCase):
    def setUp(self):
        self.registered = []
        self.flag = bridge_patch("_sandbox_exit_stop_registered", False)
        self.flag.start()
        self.atexit = mock.patch("atexit.register", side_effect=self.registered.append)
        self.atexit.start()

    def tearDown(self):
        self.atexit.stop()
        self.flag.stop()

    def test_registers_once(self):
        bridge._note_sandbox_use()
        bridge._note_sandbox_use()
        self.assertEqual(self.registered, [bridge._stop_sandbox_on_exit])

    def test_exit_terminates_only_the_sandbox(self):
        with bridge_patch("_wsl_run", return_value=(0, "")) as run:
            bridge._stop_sandbox_on_exit()
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args, ["--terminate", bridge.SANDBOX_DISTRO])
        self.assertNotIn("--shutdown", args)

    def test_exit_stop_swallows_errors(self):
        with bridge_patch("_wsl_run", side_effect=OSError("gone")):
            bridge._stop_sandbox_on_exit()

    def test_wsl_run_into_sandbox_registers(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with bridge_patch("_wsl_exe", return_value="wsl.exe"), \
             mock.patch.object(bridge.subprocess, "run", return_value=fake):
            bridge._wsl_run(["-d", bridge.SANDBOX_DISTRO, "-u", "root", "--", "true"])
        self.assertEqual(self.registered, [bridge._stop_sandbox_on_exit])

    def test_wsl_run_outside_sandbox_does_not_register(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with bridge_patch("_wsl_exe", return_value="wsl.exe"), \
             mock.patch.object(bridge.subprocess, "run", return_value=fake):
            bridge._wsl_run(["-l", "-v"])
            bridge._wsl_run(["--terminate", bridge.SANDBOX_DISTRO])
            bridge._wsl_run(["-d", "Ubuntu", "--", "true"])
        self.assertEqual(self.registered, [])

    def test_sandbox_bash_registers_other_distro_does_not(self):
        with bridge_patch("_wsl_exe", return_value="wsl.exe"), \
             mock.patch.object(bridge.subprocess, "Popen", side_effect=OSError("no spawn")):
            bridge._wsl_bash("Ubuntu", "true")
            self.assertEqual(self.registered, [])
            bridge._wsl_bash(bridge.SANDBOX_DISTRO, "true")
        self.assertEqual(self.registered, [bridge._stop_sandbox_on_exit])


if __name__ == "__main__":
    unittest.main()
