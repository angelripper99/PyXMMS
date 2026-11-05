"""A second PyXMMS process must stop while the first still holds the lock."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOLDER = textwrap.dedent(
    """\
    import sys
    from pathlib import Path
    from pyxmms.app import acquire_single_instance
    ok = acquire_single_instance(Path(sys.argv[1]))
    print("locked" if ok else "busy", flush=True)
    sys.stdin.readline()
    """
)


class SingleInstanceTests(unittest.TestCase):
    def test_second_process_is_refused_until_the_first_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = str(Path(directory) / "instance.lock")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            holder = subprocess.Popen(
                [sys.executable, "-c", HOLDER, lock],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=ROOT,
                env=env,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                refused = subprocess.run(
                    [sys.executable, "-c", HOLDER, lock],
                    input="\n",
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                    env=env,
                    timeout=10,
                )
                self.assertEqual(refused.stdout.strip(), "busy")
                self.assertEqual(refused.returncode, 0)
            finally:
                holder.stdin.close()
                holder.wait(timeout=10)
                holder.stdout.close()
                holder.stderr.close()

            again = subprocess.run(
                [sys.executable, "-c", HOLDER, lock],
                input="\n",
                capture_output=True,
                text=True,
                cwd=ROOT,
                env=env,
                timeout=10,
            )
            self.assertEqual(again.stdout.strip(), "locked")

    def test_main_reports_the_running_instance_and_help_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = str(Path(directory) / "instance.lock")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["QT_QPA_PLATFORM"] = "offscreen"
            holder = subprocess.Popen(
                [sys.executable, "-c", HOLDER, lock],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=ROOT,
                env=env,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                # Point the app at this lock without starting a window.
                probe = textwrap.dedent(
                    f"""\
                    import sys
                    from pathlib import Path
                    import pyxmms.app as app
                    app.CONFIG_PATH = Path({str(Path(directory) / "state.json")!r})
                    app.QMessageBox.critical = staticmethod(lambda *args: None)
                    sys.exit(app.main(["pyxmms"]))
                    """
                )
                # acquire_single_instance uses CONFIG_PATH.parent, which is `directory`.
                refused = subprocess.run(
                    [sys.executable, "-c", probe],
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                    env=env,
                    timeout=20,
                )
                self.assertEqual(refused.returncode, 1, refused.stderr)
                self.assertIn("PyXMMS is already running.", refused.stderr)
                help_run = subprocess.run(
                    [sys.executable, "-m", "pyxmms", "--help"],
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                    env=env,
                    timeout=15,
                )
                self.assertEqual(help_run.returncode, 0)
                self.assertNotIn("already running.", help_run.stderr.lower())
                self.assertIn("pyxmms [files or folders...]", help_run.stdout)
                self.assertNotIn("clone", help_run.stdout.lower())
            finally:
                holder.stdin.close()
                holder.wait(timeout=10)
                holder.stdout.close()
                holder.stderr.close()


if __name__ == "__main__":
    unittest.main()
