"""Exercise the Windows double-click launchers without starting real services."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = {
    "启动系统.cmd": "launch.py",
    "启动AI助手.cmd": "launch_ai.py",
}


class WindowsLauncherTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe behavior")
    def test_launchers_are_ascii_crlf_and_select_py(self):
        for filename, target in LAUNCHERS.items():
            with self.subTest(filename=filename):
                data = (ROOT / filename).read_bytes()
                self.assertTrue(data.isascii())
                self.assertIn(b"\r\n", data)
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

                with tempfile.TemporaryDirectory() as folder:
                    folder = Path(folder)
                    marker = folder / "launcher-target.txt"
                    (folder / filename).write_bytes(data)
                    (folder / target).write_text(
                        "from pathlib import Path\n"
                        "import os\n"
                        "Path(os.environ['LAUNCHER_TEST_MARKER']).write_text("
                        + repr(target)
                        + ", encoding='utf-8')\n",
                        encoding="utf-8",
                    )
                    env = os.environ.copy()
                    env["LAUNCHER_TEST_MARKER"] = str(marker)
                    result = subprocess.run(
                        ["cmd.exe", "/d", "/c", filename],
                        cwd=folder,
                        env=env,
                        input="\n",
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(target, marker.read_text(encoding="utf-8"))

    def test_git_preserves_windows_launcher_line_endings(self):
        attributes = (ROOT / ".gitattributes").read_text(encoding="ascii")
        self.assertIn("*.cmd text eol=crlf", attributes)
        release_builder = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
        self.assertIn("'.gitattributes'", release_builder)


if __name__ == "__main__":
    unittest.main()
