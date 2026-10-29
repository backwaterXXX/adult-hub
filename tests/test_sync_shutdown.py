"""The sync child's cooperative stop: a termination signal must end a pass, not kill it."""

import ast
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC_DIR = ROOT / "sync"
sys.path.insert(0, str(SYNC_DIR))
sys.path.insert(0, str(ROOT))

import sync_shutdown

# A pass that is between checkpoints, waiting for the stop request to be honoured.
CHILD = """
import sys, time
sys.path.insert(0, {directory!r})
import sync_shutdown
{install}
print("ready", flush=True)
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if sync_shutdown.stop_requested():
        print("drained", flush=True)
        sys.exit(0)
    time.sleep(0.02)
sys.exit(3)
"""


class TerminationSignalTest(unittest.TestCase):
    """What installing the handler does to this process."""

    def setUp(self):
        inherited = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(number, handler) for number, handler in inherited.items()])
        self.addCleanup(sync_shutdown.stop_event().clear)

    def test_a_termination_signal_requests_a_stop_instead_of_an_exit(self):
        sync_shutdown.install_signal_handlers()

        os.kill(os.getpid(), signal.SIGTERM)

        self.assertTrue(sync_shutdown.stop_requested())

    def test_an_interrupt_is_left_as_the_operators_immediate_abort(self):
        inherited = signal.getsignal(signal.SIGINT)

        sync_shutdown.install_signal_handlers()

        self.assertIs(signal.getsignal(signal.SIGINT), inherited)


class ChildProcessTest(unittest.TestCase):
    """The same signal against a real child process, which is how the runner calls it."""

    def run_child(self, install_handler: bool) -> tuple[int, str]:
        script = CHILD.format(
            directory=str(SYNC_DIR),
            install="sync_shutdown.install_signal_handlers()" if install_handler else "pass",
        )
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                 text=True, cwd=str(SYNC_DIR))
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            child.send_signal(signal.SIGTERM)
            remainder = child.stdout.read()
            return child.wait(timeout=15), remainder
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()

    def test_an_unhandled_termination_signal_kills_the_pass_mid_channel(self):
        """The defect being fixed: SIGTERM is default-dispositioned, so the child dies."""
        exit_code, output = self.run_child(install_handler=False)

        self.assertEqual(exit_code, -signal.SIGTERM)
        self.assertNotIn("drained", output)

    def test_a_handled_termination_signal_lets_the_pass_reach_its_checkpoint(self):
        exit_code, output = self.run_child(install_handler=True)

        self.assertEqual(exit_code, 0)
        self.assertIn("drained", output)

    def test_the_flag_is_shared_by_the_handler_and_the_pass(self):
        self.assertFalse(sync_shutdown.stop_requested())
        try:
            sync_shutdown.stop_event().set()
            self.assertTrue(sync_shutdown.stop_requested())
        finally:
            sync_shutdown.stop_event().clear()
        self.assertFalse(sync_shutdown.stop_requested())


class SyncEntryPointWiring(unittest.TestCase):
    """The call that connects the child to its checkpoints has to stay in place."""

    def main_block_statements(self):
        tree = ast.parse((SYNC_DIR / "sync.py").read_text(encoding="utf-8"))

        def is_main_guard(node):
            test = node.test
            return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__" and len(test.ops) == 1
                    and isinstance(test.ops[0], ast.Eq)
                    and getattr(test.comparators[0], "value", None) == "__main__")

        guard = next((node for node in tree.body if isinstance(node, ast.If) and is_main_guard(node)), None)
        self.assertIsNotNone(guard, "sync.py must keep its command-line entry point")
        return [ast.unparse(statement) for statement in guard.body]

    def test_the_full_sync_path_installs_the_handler_before_running(self):
        statements = self.main_block_statements()

        installed = [index for index, text in enumerate(statements)
                     if text.startswith("install_signal_handlers(")]
        jobs_only = [index for index, text in enumerate(statements)
                     if text.startswith("if args.jobs_only:")]
        self.assertEqual(len(installed), 1, "the sync child asks for the cooperative stop once")
        self.assertEqual(len(jobs_only), 1)
        self.assertLess(jobs_only[0], installed[0],
                        "the jobs-only worker has no checkpoint to reach and keeps the default disposition")


if __name__ == "__main__":
    unittest.main()
