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

    def test_profile_allow_entries_are_exactly_the_nine_reads_on_the_pinned_paths(self):
        d = doc(checkout="/srv/repo")
        allow = os_sandbox.profile(d, "/srv/ws", PINS)["allow"]
        self.assertEqual(allow, [
            "Bash(/opt/x/gh issue view *)",
            "Bash(/opt/x/gh pr view *)",
            "Bash(/opt/x/gh pr checks *)",
            "Bash(/opt/x/gh api user --jq .login)",
            "Bash(/opt/x/gh auth status)",
            "Bash(/usr/bin/git -c core.hooksPath=/dev/null -c core.sshCommand=ssh "
            "-c credential.helper= -c core.askPass= -c protocol.ext.allow=never "
            "-c protocol.file.allow=never -c protocol.allow=never -c protocol.ssh.allow=always "
            "-c protocol.git.allow=never -c push.gpgSign=false -c gpg.program= "
            "-c core.alternateRefsCommand= -c core.fsmonitor=false -c gc.auto=0 "
            "-c maintenance.auto=false -C %s fetch --no-recurse-submodules origin main)"
            % os.path.realpath("/srv/repo"),
            "Bash(/opt/y/bin/ppl tracker fetch --task *)",
            "Bash(/opt/y/bin/ppl review fetch --pr *)",
            "Bash(/opt/y/bin/ppl doctor --forge)"])

    def test_no_allow_entry_covers_a_github_write(self):
        text = " ".join(self.profile()["allow"])
        # " push " as a git verb: the fetch entry's push.gpgSign=false is a config key
        for write in ("pr create", "issue comment", "pr close", "api *", " push "):
            self.assertNotIn(write, text)

    def test_protected_paths_are_write_denied_by_the_profile(self):
        # deny beats any allow from any settings source, so the boundary does not rest on
        # no other source granting a write there
        from ppl import escape_paths
        deny = self.profile()["sandbox"]["filesystem"]["denyWrite"]
        protected = escape_paths.protected_paths(
            paths.home(), {"workspace": os.path.realpath("/srv/ws"), "pins": PINS})
        for entry in protected:
            self.assertIn(entry, deny)
        for entry in (os.path.expanduser("~/.gitconfig"), os.path.expanduser("~/.ssh"),
                      str(paths.home() / "config.toml"), str(paths.home() / "os_sandbox.json"),
                      os.path.join(os.path.realpath("/srv/ws"), ".claude")):
            self.assertIn(os.path.realpath(entry), deny)
        self.assertEqual(len(deny), len(set(deny)))

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

    def test_a_relative_which_result_is_stored_absolute(self):
        base = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplbin-")))
        (base / "gh").write_text("")
        old = os.getcwd()
        os.chdir(base)
        try:
            got = os_sandbox.pins(lambda name: "./gh")
        finally:
            os.chdir(old)
        self.assertEqual(got["gh"]["path"], str(base / "gh"))

    def _fallback(self, name, executable=True):
        base = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplbin-")))
        script = base / name
        script.write_text("")
        script.chmod(0o755 if executable else 0o644)
        which = lambda n: None if n == "ppl" else "/usr/bin/" + n
        with mock.patch.object(sys, "argv", [str(script)]):
            return script, which

    def test_the_ppl_fallback_accepts_an_executable_file(self):
        script, which = self._fallback("ppl")
        with mock.patch.object(sys, "argv", [str(script)]):
            self.assertEqual(os_sandbox.pins(which)["ppl"]["path"], str(script))

    def test_the_ppl_fallback_refuses_a_py_file(self):
        script, which = self._fallback("ppl.py")
        with mock.patch.object(sys, "argv", [str(script)]):
            with self.assertRaises(PplError) as caught:
                os_sandbox.pins(which)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("PATH", caught.exception.message)

    def test_the_ppl_fallback_refuses_a_file_that_is_not_executable(self):
        script, which = self._fallback("ppl", executable=False)
        with mock.patch.object(sys, "argv", [str(script)]):
            with self.assertRaises(PplError) as caught:
                os_sandbox.pins(which)
        self.assertIn("PATH", caught.exception.message)


class TestFacts(Home):
    def test_facts_carry_repos_branch_scratch_and_pins(self):
        d = doc(tracker="github-issues")
        d["tracker"]["repo"] = "acme/widgets"
        got = os_sandbox.facts(d, "/srv/ws", PINS)
        self.assertEqual(set(got), {"version", "workspace", "checkout", "worktrees",
                                    "default_branch", "forge_repo", "tracker_repo", "scratch",
                                    "write_scope", "deny_write", "pins"})
        self.assertEqual(got["version"], 1)
        self.assertEqual(got["forge_repo"], "acme/widgets")
        self.assertEqual(got["tracker_repo"], "acme/widgets")
        self.assertEqual(got["default_branch"], "main")
        self.assertEqual(got["scratch"], os.environ["PPL_HOME"] + "/scratch")
        self.assertEqual(got["pins"], PINS)
        self.assertEqual(got["write_scope"], os_sandbox.write_scope(d))

    def test_facts_record_the_profiles_write_denials_realpathed(self):
        got = os_sandbox.facts(doc(), "/srv/ws", PINS)["deny_write"]
        denied = self.profile_deny()
        self.assertEqual(got, list(dict.fromkeys(os.path.realpath(entry) for entry in denied)))
        self.assertIn(os.path.realpath("/srv/ws"), got)

    def profile_deny(self):
        return os_sandbox.profile(doc(), "/srv/ws", PINS)["sandbox"]["filesystem"]["denyWrite"]

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


class TestSetupChecks(Home):
    def test_layout_problem_names_both_paths_for_nesting_either_way(self):
        ws = os.path.realpath(tmphome.mkdtemp(prefix="pplws-"))
        inside = doc(checkout=ws + "/repo")
        self.assertIn(ws + "/repo", os_sandbox.layout_problem(inside, ws))
        around = doc(checkout=os.path.dirname(ws))
        self.assertIn(os.path.dirname(ws), os_sandbox.layout_problem(around, ws))
        apart = doc(checkout="/srv/repo")
        apart["repo"]["worktrees"] = "/srv/wt"
        self.assertIsNone(os_sandbox.layout_problem(apart, ws))

    def test_a_sibling_with_a_common_name_prefix_is_not_nested(self):
        ws = os.path.realpath(tmphome.mkdtemp(prefix="pplws-"))
        apart = doc(checkout=ws + "-repo")
        apart["repo"]["worktrees"] = ws + "-wt"
        self.assertIsNone(os_sandbox.layout_problem(apart, ws))

    def test_repo_problem_covers_forge_and_tracker(self):
        with mock.patch("ppl.setup._origin_repo", lambda checkout: None):
            self.assertIn("forge_repo", os_sandbox.repo_problem(doc()))
            self.assertIn("[tracker] repo", os_sandbox.repo_problem(
                doc(forge="gitlab", tracker="github-issues")))
            self.assertIsNone(os_sandbox.repo_problem(doc(forge="gitlab")))
        with mock.patch("ppl.setup._origin_repo", lambda checkout: "acme/demo"):
            self.assertIsNone(os_sandbox.repo_problem(doc()))


if __name__ == "__main__":
    unittest.main()
