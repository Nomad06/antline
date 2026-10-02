"""The public tree names no private project, person or machine path."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[1]
# One bracketed letter per name keeps this file from matching its own pattern.
PRIVATE = re.compile(r"rog[i]i|star[s]teer|SOL[O]-|bogatyre[v]|/User[s]/user"
                     r"|ai-usag[e]|progress fiel[d]", re.IGNORECASE)
# The design spec and plan describe the scrub itself, so they name what it removes.
EXEMPT_PREFIXES = ()


def tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True,
                         check=True).stdout
    return [REPO / name.decode("utf-8") for name in out.split(b"\0") if name]


class TestNoPrivateStrings(unittest.TestCase):
    def test_no_tracked_file_names_a_private_string(self):
        # rule: R-NO-PRIVATE-STRINGS
        try:
            paths = tracked_files()
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.skipTest("not a git checkout")
        offenders = []
        for path in paths:
            relative = path.relative_to(REPO).as_posix()
            if relative.startswith(EXEMPT_PREFIXES) or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if PRIVATE.search(line):
                    offenders.append("%s:%d: %s" % (relative, number, line.strip()[:100]))
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
