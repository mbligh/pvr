"""Acquisition diagnostics are emitted before subprocess completion."""
import io
from pathlib import Path
import sys
import tempfile
import unittest

from orchestration.subprocess_output import stream_stderr


class StreamingTests(unittest.TestCase):
    def test_stderr_is_relayed_before_exit_and_summary_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "observed"
            class Output(io.StringIO):
                def flush(self):
                    if "Download started" in self.getvalue():
                        marker.touch()
            code = (
                "import pathlib,sys,time\n"
                "print('Download started: test',file=sys.stderr,flush=True)\n"
                "p=pathlib.Path(sys.argv[1])\n"
                "for i in range(200):\n"
                " if p.exists(): break\n"
                " time.sleep(.01)\n"
                "else: sys.exit(2)\n"
                "print('result')\n"
                "print('{\\\"planned\\\": 1}',file=sys.stderr,flush=True)\n"
            )
            out = Output()
            result = stream_stderr([sys.executable, "-c", code, str(marker)], out)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "result\n")
            self.assertEqual(result.stderr, out.getvalue())
            self.assertIn('{"planned": 1}', result.stderr)


if __name__ == "__main__":
    unittest.main()
