"""Relay acquisition diagnostics immediately while retaining machine summaries."""
import subprocess
import tempfile


def stream_stderr(command, stream):
    """Stream stderr line by line; retain stdout and stderr for existing callers.

    A temporary file for stdout prevents a full stdout pipe from deadlocking
    stderr reads. Discovery/import subprocesses keep their existing capture.
    """
    lines = []
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout:
        with subprocess.Popen(command, stdout=stdout, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace") as child:
            for line in child.stderr:
                lines.append(line)
                stream.write(line)
                stream.flush()
            code = child.wait()
        stdout.seek(0)
        return subprocess.CompletedProcess(command, code, stdout.read(), "".join(lines))
