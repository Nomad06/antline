import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import os_sandbox, paths
from ppl.errors import PplError

PINS = {"gh": {"path": "/opt/x/gh", "realpath": "/opt/x/gh"},
        "git": {"path": "/usr/bin/git", "realpath": "/usr/bin/git"},
        "ppl": {"path": "/opt/y/bin/ppl", "realpath": "/opt/y/lib/ppl"}}


def doc(forge="github", tracker="jira", checkout="/srv/repo", **extra):
    out = {"repo": {"path": checkout, "default_branch": "main"},
           "forge": {"kind": forge}, "tracker": {"kind": tracker}}
    out.update(extra)
    return out


class Home(unittest.TestCase):
    def setUp(self):
        self._home = os.environ["PPL_HOME"]
        os.environ["PPL_HOME"] = os.path.realpath(tmphome.mkdtemp(prefix="pplhome-"))

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home


class TestNeeded(Home):
    def test_a_github_forge_or_tracker_needs_the_profile(self):
        self.assertTrue(os_sandbox.needed(doc(forge="github", tracker="jira")))
        self.assertTrue(os_sandbox.needed(doc(forge="gitlab", tracker="github-issues")))

    def test_a_gitlab_jira_harness_does_not(self):
        self.assertFalse(os_sandbox.needed(doc(forge="gitlab", tracker="jira")))

    def test_enabled_true_opts_a_gitlab_harness_in(self):
        self.assertTrue(os_sandbox.needed(
            doc(forge="gitlab", tracker="jira", os_sandbox={"enabled": True})))


class TestScope(Home):
    def test_write_scope_lists_runs_labels_scratch_checkout_and_worktrees_resolved(self):
        base = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplrepo-")))
        (base / "real").mkdir()
        (base / "link").symlink_to(base / "real")
        scope = os_sandbox.write_scope(doc(checkout=str(base / "link")))
        home = os.environ["PPL_HOME"]
        self.assertEqual(scope, [home + "/runs", home + "/labels", home + "/scratch",
                                 str(base / "real"), str(base / "real" / ".worktrees")])

    def test_allow_write_follows_the_scope_and_expands_home(self):
        scope = os_sandbox.write_scope(doc(os_sandbox={"allow_write": ["~/.npm"]}))
        self.assertEqual(scope[-1], os.path.realpath(os.path.expanduser("~/.npm")))

    def test_scratch_dir_is_per_task_under_the_root(self):
        self.assertEqual(os_sandbox.scratch_dir("T-1"), paths.home() / "scratch" / "T-1")


class TestProfile(Home):
    def profile(self, **kwargs):
        return os_sandbox.profile(doc(**kwargs), "/srv/ws", PINS)

    def test_profile_denies_github_and_takes_back_the_workspace(self):
        sandbox = self.profile()["sandbox"]
        self.assertEqual(sandbox["network"]["deniedDomains"], ["github.com", "*.github.com"])
        self.assertEqual(set(sandbox["network"]["deniedDomainReasons"]),
                         {"github.com", "*.github.com"})
        self.assertIn(os.path.realpath("/srv/ws"), sandbox["filesystem"]["denyWrite"])
        self.assertIs(sandbox["allowUnsandboxedCommands"], True)
        self.assertIs(sandbox["failIfUnavailable"], True)
        self.assertIs(sandbox["enabled"], True)

    def test_profile_carries_the_harness_domains_and_write_scope(self):
        got = self.profile(os_sandbox={"allowed_domains": ["pypi.org"]})["sandbox"]
        self.assertEqual(got["network"]["allowedDomains"], ["pypi.org"])
        self.assertEqual(got["filesystem"]["allowWrite"], os_sandbox.write_scope(doc()))

    def test_profile_allow_entries_sit_on_the_pinned_paths(self):
        allow = self.profile()["allow"]
        self.assertIn("Bash(/opt/x/gh pr create *)", allow)
        self.assertIn("Bash(/opt/x/gh auth status)", allow)
        self.assertIn("Bash(/usr/bin/git -c core.hooksPath=/dev/null *)", allow)
        self.assertIn("Bash(/opt/y/bin/ppl doctor --forge)", allow)
        self.assertEqual(len(allow), 12)
        self.assertFalse([e for e in allow if e.startswith(("Bash(gh ", "Bash(git ", "Bash(ppl "))])

    def test_pin_dirs_are_write_denied_both_path_and_realpath(self):
        deny = self.profile()["sandbox"]["filesystem"]["denyWrite"]
        for entry in ("/opt/x", "/usr/bin", "/opt/y/bin", "/opt/y/lib"):
            self.assertIn(os.path.realpath(entry), deny)


class TestPins(Home):
    def test_a_missing_binary_refuses_naming_it(self):
        which = lambda name: None if name == "gh" else "/usr/bin/" + name
        with self.assertRaises(PplError) as caught:
            os_sandbox.pins(which)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("gh", caught.exception.message)

    def test_pins_record_the_path_as_found_and_its_realpath(self):
        base = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplbin-")))
        (base / "real-gh").write_text("")
        (base / "gh").symlink_to(base / "real-gh")
        got = os_sandbox.pins(lambda name: str(base / "gh"))
        self.assertEqual(got["gh"], {"path": str(base / "gh"), "realpath": str(base / "real-gh")})
        self.assertEqual(sorted(got), ["gh", "git", "ppl"])


class TestFacts(Home):
    def test_facts_carry_repos_branch_scratch_and_pins(self):
        d = doc(tracker="github-issues")
        d["tracker"]["repo"] = "acme/widgets"
        got = os_sandbox.facts(d, "/srv/ws", PINS)
        self.assertEqual(set(got), {"version", "workspace", "checkout", "worktrees",
                                    "default_branch", "forge_repo", "tracker_repo", "scratch",
                                    "write_scope", "pins"})
        self.assertEqual(got["version"], 1)
        self.assertEqual(got["forge_repo"], "acme/widgets")
        self.assertEqual(got["tracker_repo"], "acme/widgets")
        self.assertEqual(got["default_branch"], "main")
        self.assertEqual(got["scratch"], os.environ["PPL_HOME"] + "/scratch")
        self.assertEqual(got["pins"], PINS)
        self.assertEqual(got["write_scope"], os_sandbox.write_scope(d))

    def test_the_forge_repo_comes_from_the_checkout_origin_with_a_jira_tracker(self):
        with mock.patch("ppl.setup._origin_repo", return_value="o/n") as origin:
            got = os_sandbox.facts(doc(), "/srv/ws", PINS)
        self.assertEqual((got["forge_repo"], got["tracker_repo"]), ("o/n", None))
        origin.assert_called_once()

    def test_a_gitlab_forge_has_no_forge_repo(self):
        got = os_sandbox.facts(doc(forge="gitlab", os_sandbox={"enabled": True}), "/w", PINS)
        self.assertIsNone(got["forge_repo"])


class TestLoadFacts(Home):
    def test_gh_path_is_the_pin_when_facts_exist_else_bare_gh(self):
        self.assertIsNone(os_sandbox.load_facts())
        self.assertEqual(os_sandbox.gh_path(), "gh")
        (paths.home() / os_sandbox.FACTS_FILE).write_text(
            json.dumps({"pins": {"gh": {"path": "/opt/x/gh"}}, "written_sandbox": {}}))
        self.assertEqual(os_sandbox.gh_path(), "/opt/x/gh")
        self.assertIn("written_sandbox", os_sandbox.load_facts())

    def test_a_malformed_facts_file_is_refused_and_gh_falls_back(self):
        (paths.home() / os_sandbox.FACTS_FILE).write_text("[1]")
        with self.assertRaises(PplError):
            os_sandbox.load_facts()
        self.assertEqual(os_sandbox.gh_path(), "gh")


if __name__ == "__main__":
    unittest.main()
