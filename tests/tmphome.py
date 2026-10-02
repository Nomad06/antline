"""Temporary directories that do not outlive the test process.

Every test module points PPL_HOME at a fresh directory at import time, before
ppl is imported, so paths.home() can never resolve to the real data
root (~/.ppl). Plain tempfile.mkdtemp left every one of those behind: a
few hundred suite runs had accumulated over 900 pplhome-* directories in
TMPDIR. Registering the removal at creation keeps that property (a fresh
directory per module, chosen before any import) and still cleans up, including
after a failing run, since atexit fires either way.
"""
import atexit
import shutil
import tempfile


def mkdtemp(prefix="ppltmp-"):
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path
