import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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

    def test_inspect_refuses_a_home_directory_it_cannot_expand(self):
        with self.assertRaises(PplError) as caught:
            project.inspect("~nosuchuser/x")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("expand", caught.exception.message)

    def test_inspect_strips_credentials_from_an_url_remote(self):
        repo = panel_client.make_repo(remote="https://oauth2:glpat-x@gitlab.com/a/b.git")
        info = project.inspect(str(repo))
        self.assertEqual(info["remote_url"], "https://gitlab.com/a/b.git")
        self.assertEqual(info["forge_hint"], "gitlab")

    def test_inspect_strips_a_token_given_as_the_user_name(self):
        repo = panel_client.make_repo(remote="https://ghp_secret@github.com/a/b.git")
        self.assertEqual(project.inspect(str(repo))["remote_url"], "https://github.com/a/b.git")

    def test_inspect_keeps_an_scp_style_remote_as_it_is(self):
        repo = panel_client.make_repo(remote="git@github.com:a/b.git")
        info = project.inspect(str(repo))
        self.assertEqual(info["remote_url"], "git@github.com:a/b.git")
        self.assertEqual(info["forge_hint"], "github")

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

    def test_find_md_lists_only_regular_files_inside_the_repository(self):
        repo = panel_client.make_repo({"b/CLAUDE.md": "# b\n", "a/README.md": "x\n"})
        outside = Path(tmphome.mkdtemp(prefix="pplout-")) / "notes.md"
        outside.write_text("private\n", encoding="utf-8")
        (repo / "AGENTS.md").symlink_to(outside)
        (repo / "a" / "CLAUDE.md").symlink_to(repo / ".git" / "config")
        if hasattr(os, "mkfifo"):
            os.mkfifo(repo / "CLAUDE.md")
        self.assertEqual([item["path"] for item in project.find_md(repo)], ["b/CLAUDE.md"])

    def test_collect_refuses_a_selected_symlink_that_leaves_the_repository(self):
        repo = panel_client.make_repo({"README.md": "demo\n"})
        outside = Path(tmphome.mkdtemp(prefix="pplout-")) / "notes.md"
        outside.write_text("private\n", encoding="utf-8")
        (repo / "CLAUDE.md").symlink_to(outside)
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual(files, [])
        self.assertEqual([item["reason"] for item in skipped],
                         ["not one of the files the project inspection found"])

    def test_collect_refuses_an_imported_symlink_that_leaves_the_repository(self):
        repo = panel_client.make_repo({"CLAUDE.md": "@notes.md\n"})
        outside = Path(tmphome.mkdtemp(prefix="pplout-")) / "notes.md"
        outside.write_text("private\n", encoding="utf-8")
        (repo / "notes.md").symlink_to(outside)
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md"])
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

    def test_collect_never_follows_an_import_into_a_skipped_directory(self):
        repo = panel_client.make_repo({
            "CLAUDE.md": "@.git/config @.env @node_modules/x/README.md @.worktrees/w/NOTES.md "
                         "@.claude/rules.md @docs/link.md\n",
            ".env": "TOKEN=secret\n", "node_modules/x/README.md": "readme\n",
            ".worktrees/w/NOTES.md": "notes\n", ".claude/rules.md": "rules\n"})
        (repo / "docs").mkdir()
        (repo / "docs" / "link.md").symlink_to(repo / ".git" / "HEAD")
        with mock.patch.object(project, "open", create=True, side_effect=open) as opener:
            files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md", ".claude/rules.md"])
        self.assertEqual(sorted((item["path"], item["reason"]) for item in skipped),
                         sorted((path, "in a skipped directory") for path in (
                             ".git/config", ".env", "node_modules/x/README.md",
                             ".worktrees/w/NOTES.md", ".git/HEAD")))
        self.assertEqual([Path(call.args[0]).name for call in opener.call_args_list],
                         ["CLAUDE.md", "rules.md"])

    def test_collect_skips_absolute_and_home_imports_without_reading_them(self):
        outside = Path(tmphome.mkdtemp(prefix="pplout-")) / "secret.md"
        outside.write_text("private\n", encoding="utf-8")
        repo = panel_client.make_repo({"CLAUDE.md": "@%s and @~/.ssh/id_rsa and @%s\n" % (outside, outside)})
        with mock.patch.object(project, "open", create=True, side_effect=open) as opener:
            files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md"])
        self.assertEqual(skipped, [{"path": str(outside), "reason": "outside the repository"},
                                   {"path": "~/.ssh/id_rsa", "reason": "outside the repository"}])
        self.assertEqual(len(opener.call_args_list), 1)

    def test_collect_tells_a_missing_import_from_one_that_is_not_a_file(self):
        repo = panel_client.make_repo({"CLAUDE.md": "@docs @gone.md\n", "docs/a.md": "a\n"})
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md"])
        self.assertEqual(skipped, [{"path": "docs", "reason": "not a regular file"},
                                   {"path": "gone.md", "reason": "no such file"}])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_collect_does_not_open_an_imported_fifo(self):
        repo = panel_client.make_repo({"CLAUDE.md": "@pipe\n"})
        os.mkfifo(repo / "pipe")
        with mock.patch.object(project, "open", create=True, side_effect=open) as opener:
            files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual(skipped, [{"path": "pipe", "reason": "not a regular file"}])
        self.assertEqual(len(opener.call_args_list), 1)

    def test_collect_caps_the_skipped_list(self):
        imports = " ".join("@missing%d.md" % number for number in range(project.MAX_SKIPPED * 3))
        repo = panel_client.make_repo({"CLAUDE.md": imports + "\n"})
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md"])
        self.assertEqual(len(skipped), project.MAX_SKIPPED)
        self.assertEqual(skipped[0], {"path": "missing0.md", "reason": "no such file"})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any file")
    def test_collect_skips_a_file_it_cannot_read(self):
        repo = panel_client.make_repo({"CLAUDE.md": "@locked.md\n", "locked.md": "x\n"})
        locked = repo / "locked.md"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o644)
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md"])
        self.assertEqual(skipped, [{"path": "locked.md", "reason": "cannot be read"}])

    def test_collect_stops_at_the_size_cap(self):
        big = "x" * (project.MAX_BYTES - 10) + "\n"
        repo = panel_client.make_repo({"CLAUDE.md": "@big.md @small.md\n", "big.md": big,
                                       "small.md": "y\n"})
        files, skipped = project.collect(repo, ["CLAUDE.md"])
        # the file over the budget is skipped, a small one after it still fits
        self.assertEqual([item["path"] for item in files], ["CLAUDE.md", "small.md"])
        self.assertEqual([(item["path"], "byte budget" in item["reason"]) for item in skipped],
                         [("big.md", True)])


class TestMakeRepo(unittest.TestCase):
    def test_make_repo_ignores_the_hosts_global_git_config(self):
        scratch = Path(tmphome.mkdtemp(prefix="pplgitcfg-"))
        hooks = scratch / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        config = scratch / "gitconfig"
        config.write_text("[commit]\n    gpgsign = true\n[gpg]\n    program = false\n"
                          "[core]\n    hooksPath = %s\n" % hooks, encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            repo = panel_client.make_repo()
        count = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(count, "1")


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
