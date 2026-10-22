"""The sample project the demo works on: a tiny shop, its issue, its harness and a gh stand-in."""
import json
import sys

from ..studio import stacks

FILES = {
    "shop/__init__.py": "",
    "shop/cart.py": "def total(items):\n    return sum(items)\n",
    "tests/__init__.py": "",
    "tests/test_cart.py": (
        "import unittest\n\nfrom shop.cart import total\n\n\n"
        "class TestCart(unittest.TestCase):\n"
        "    def test_total(self):\n"
        "        self.assertEqual(total([1, 2]), 3)\n"),
    "pyproject.toml": "[project]\nname = \"shop\"\nversion = \"0.0.1\"\n",
    "README.md": "# shop\n\nA tiny cart, the demo's sample project.\n",
}

ISSUE = {
    "number": 1,
    "title": "Cart total ignores the discount",
    "body": "total() must apply a percentage discount.",
    "state": "OPEN",
    "labels": [],
    "assignees": [],
    "author": {"login": "demo"},
    "createdAt": "2026-10-22T00:00:00Z",
    "updatedAt": "2026-10-22T00:00:00Z",
    "url": "https://github.com/demo/shop/issues/1",
    "comments": [],
}


def harness(repo):
    """(doc, fragments) of the demo's harness: the python preset with unittest gates."""
    draft = stacks.draft("python", {
        "path": repo, "name_hint": "demo", "default_branch": "main", "forge_hint": "github",
        "remote_url": "https://github.com/demo/shop.git"})
    doc, fragments = draft["doc"], draft["fragments"]
    python = sys.executable
    doc["gate"] = [
        {"name": "install", "command": "%s -c pass" % python},
        {"name": "focused-tests", "command": "%s -m unittest -q <test_class>" % python},
        {"name": "integration-tests", "command": "%s -m unittest -q" % python},
    ]
    for key in stacks.GATE_FRAGMENT_KEYS:
        fragments.pop(key, None)
    fragments.update(stacks.gate_fragments(doc["gate"], doc["gate"][0]["command"], "python"))
    return doc, fragments


def gh_source():
    """The gh stand-in: the canned issue, a 404 for the parent and sub-issue links."""
    return (
        "#!%s\n"
        "import json\nimport sys\n\n"
        "ISSUE = json.loads(%r)\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == [\"issue\", \"view\"]:\n"
        "    print(json.dumps(ISSUE))\n"
        "    sys.exit(0)\n"
        "if args[:1] == [\"api\"]:\n"
        "    sys.stderr.write(\"gh: Not Found (HTTP 404)\\n\")\n"
        "    sys.exit(1)\n"
        "sys.exit(1)\n") % (sys.executable, json.dumps(ISSUE))
