import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import harness
from ppl.errors import PplError
from ppl.studio import project


def real(path):
    return os.path.realpath(str(path))


class TestInspect(unittest.TestCase):
    def test_inspect_reads_the_git_facts(self):
        repo = panel_client.make_repo({"package.json": "{}\n"}, branch="trunk",
                                      remote="git@github.com:someone/demo-app.git")
        info = project.inspect(str(repo))
        self.assertEqual(info["path"], real(repo))
        self.assertEqual(info["default_branch"], "trunk")
        self.assertEqual(info["forge_hint"], "github")
        self.assertEqual(info["stack"]["stack"], "node")
        self.assertTrue(harness._NAME_RE.match(info["name_hint"]))

    def test_inspect_prefers_origin_head(self):
        repo = panel_client.make_repo(remote="https://gitlab.example.com/team/app.git")
        subprocess.run(["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD",
                        "refs/remotes/origin/develop"], check=True)
        info = project.inspect(str(repo))
        self.assertEqual(info["default_branch"], "develop")
        self.assertEqual(info["forge_hint"], "gitlab")

    def test_inspect_from_a_subdirectory_gives_the_checkout_root(self):
        repo = panel_client.make_repo({"src/main.c": "int main;\n"})
        self.assertEqual(project.inspect(str(repo / "src"))["path"], real(repo))

    def test_inspect_refuses_a_directory_outside_git(self):
        with self.assertRaises(PplError) as caught:
            project.inspect(tmphome.mkdtemp(prefix="pplnogit-"))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("git", caught.exception.message)

    def test_inspect_refuses_a_missing_or_empty_path(self):
        for raw in ("", "   ", "/no/such/dir/anywhere", None):
            with self.assertRaises(PplError):
                project.inspect(raw)

    def test_inspect_resolves_a_symlinked_path(self):
        repo = panel_client.make_repo()
        link = Path(tmphome.mkdtemp(prefix="ppllink-")) / "checkout"
        link.symlink_to(repo)
        info = project.inspect(str(link))
        self.assertEqual(info["path"], real(repo))
        self.assertTrue(Path(info["path"]).is_dir())

    def test_inspect_accepts_a_path_with_spaces(self):
        parent = Path(tmphome.mkdtemp(prefix="ppl space-")) / "My Project"
        parent.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(parent)], check=True)
        info = project.inspect(str(parent))
        self.assertEqual(info["path"], real(parent))
        self.assertTrue(harness._NAME_RE.match(info["name_hint"]))

    def test_forge_hint_reads_the_remote_host(self):
        cases = (("git@github.com:o/r.git", "github"), ("https://github.com/o/r", "github"),
                 ("ssh://git@gitlab.company.io/o/r.git", "gitlab"),
                 ("https://code.example.org/o/r.git", None), (None, None), ("not a url", None))
        for remote, expected in cases:
            self.assertEqual(project.forge_hint(remote), expected, remote)

    def test_forge_hint_ignores_a_remote_urlparse_refuses(self):
        self.assertIsNone(project.forge_hint("https://[broken/o/r.git"))


class TestMdFiles(unittest.TestCase):
    def test_find_md_lists_root_nested_and_dot_claude_files_and_skips_build_dirs(self):
        repo = panel_client.make_repo({
            "CLAUDE.md": "# root\n", "AGENTS.md": "# agents\n", ".claude/CLAUDE.md": "# dot\n",
            "service/CLAUDE.md": "# service\n", "node_modules/pkg/CLAUDE.md": "# no\n",
            "build/CLAUDE.md": "# no\n", ".hidden/CLAUDE.md": "# no\n"})
        paths = [item["path"] for item in project.find_md(repo)]
        self.assertEqual(paths, ["AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md", "service/CLAUDE.md"])

    def test_find_md_stops_at_the_depth_limit(self):
        repo = panel_client.make_repo({"a/b/c/CLAUDE.md": "# three\n", "a/b/c/d/CLAUDE.md": "# four\n"})
        self.assertEqual([item["path"] for item in project.find_md(repo)], ["a/b/c/CLAUDE.md"])

    def test_find_md_skips_a_broken_symlink(self):
        repo = panel_client.make_repo({"AGENTS.md": "# agents\n"})
        (repo / "CLAUDE.md").symlink_to(repo / "gone.md")
        self.assertEqual([item["path"] for item in project.find_md(repo)], ["AGENTS.md"])

    def test_collect_reads_a_symlinked_md_once_through_its_target(self):
        repo = panel_client.make_repo({"AGENTS.md": "# agents\n"})
        (repo / "CLAUDE.md").symlink_to("AGENTS.md")
        files, skipped = project.collect(repo, [item["path"] for item in project.find_md(repo)])
        self.assertEqual([item["path"] for item in files], ["AGENTS.md"])
        self.assertEqual(skipped, [])

    def test_collect_refuses_a_symlink_that_leaves_the_repository(self):
        repo = panel_client.make_repo({"README.md": "demo\n"})
        outside = Path(tmphome.mkdtemp(prefix="pplout-")) / "notes.md"
        outside.write_text("private\n", encoding="utf-8")
        (repo / "CLAUDE.md").symlink_to(outside)
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual(files, [])
        self.assertEqual([item["reason"] for item in skipped], ["outside the repository"])

    def test_collect_follows_imports_inside_the_repo_only(self):
        repo = panel_client.make_repo({"CLAUDE.md": "See @docs/build.md and @../outside.md.\n",
                                       "docs/build.md": "Run `make test`.\n"})
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md", "docs/build.md"])
        self.assertEqual([item["reason"] for item in skipped], ["outside the repository"])

    def test_collect_follows_each_import_once(self):
        repo = panel_client.make_repo({"CLAUDE.md": "@AGENTS.md\n", "AGENTS.md": "@CLAUDE.md\n"})
        files, skipped = project.collect(repo, ["CLAUDE.md", "AGENTS.md"])
        self.assertEqual(sorted(item["path"] for item in files), ["AGENTS.md", "CLAUDE.md"])
        self.assertEqual(skipped, [])

    def test_collect_refuses_a_selection_the_inspection_did_not_list(self):
        repo = panel_client.make_repo({"CLAUDE.md": "# root\n", "secret.md": "x\n"})
        files, skipped = project.collect(repo, ["../../etc/passwd", "secret.md"])
        self.assertEqual(files, [])
        self.assertEqual(len(skipped), 2)

    def test_collect_skips_non_utf8_and_binary_files(self):
        repo = panel_client.make_repo({"CLAUDE.md": "@a.md @b.md\n", "a.md": b"\xff\xfe bad",
                                       "b.md": b"text\x00more"})
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md"])
        self.assertEqual(sorted(item["reason"] for item in skipped), ["binary file", "not UTF-8 text"])

    def test_collect_stops_at_the_size_cap(self):
        big = "x" * (project.MAX_BYTES - 10) + "\n"
        repo = panel_client.make_repo({"CLAUDE.md": "@big.md @small.md\n", "big.md": big,
                                       "small.md": "y\n"})
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        # the file over the budget is skipped, a small one after it still fits
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md", "small.md"])
        self.assertEqual([(item["path"], "byte budget" in item["reason"]) for item in skipped],
                         [("big.md", True)])


class TestProjectRoutes(panel_client.StudioTestCase):
    def test_the_inspect_route_returns_the_project(self):
        repo = panel_client.make_repo({"CLAUDE.md": "# hi\n", "go.mod": "module x\n"})
        status, info, _ = panel_client.call("POST", "/api/project/inspect", body={"path": str(repo)})
        self.assertEqual(status, 200)
        self.assertEqual(info["stack"]["stack"], "go")
        self.assertEqual([item["path"] for item in info["md_files"]], ["CLAUDE.md"])

    def test_the_inspect_route_refuses_a_non_repository(self):
        status, payload, _ = panel_client.call("POST", "/api/project/inspect",
                                               body={"path": tmphome.mkdtemp(prefix="pplnogit-")})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_the_preset_route_drafts_from_the_detected_stack_or_a_chosen_one(self):
        repo = panel_client.make_repo({"Cargo.toml": "[package]\n"})
        status, draft, _ = panel_client.call("POST", "/api/harness/preset", body={"path": str(repo)})
        self.assertEqual(status, 200)
        self.assertEqual(draft["stack"], "rust")
        status, draft, _ = panel_client.call("POST", "/api/harness/preset",
                                             body={"path": str(repo), "stack": "make"})
        self.assertEqual(draft["stack"], "make")

    def test_the_preset_route_refuses_an_unknown_or_malformed_stack(self):
        repo = panel_client.make_repo()
        for stack in ("cobol", ["node"]):
            status, payload, _ = panel_client.call("POST", "/api/harness/preset",
                                                   body={"path": str(repo), "stack": stack})
            self.assertEqual(status, 400, stack)
            self.assertEqual(payload["error_code"], "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()
