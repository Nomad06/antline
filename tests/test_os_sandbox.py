import json
import os
import subprocess
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


def fake_worktree(checkout, worktree):
    """`worktree` laid out as git worktree add leaves it: a .git file naming its git dir in
    the checkout's .git/worktrees, which holds gitdir and commondir. Returns the git dir."""
    git_dir = Path(checkout) / ".git" / "worktrees" / Path(worktree).name
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "gitdir").write_text(str(Path(worktree) / ".git") + "\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    Path(worktree).mkdir(parents=True, exist_ok=True)
    (Path(worktree) / ".git").write_text("gitdir: %s\n" % git_dir, encoding="utf-8")
    return git_dir


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

    def test_a_lanes_write_scope_is_its_own_run_worktree_and_git_dir(self):
        # rule: R-LANE-CROSS-WRITE
        # R25, R35: git in a worktree writes the checkout's .git objects, refs and logs and its
        # own worktrees/<id>, and the lane writes nothing else of the checkout's .git, of the
        # checkout or of another lane's worktree. R44: of runs/ and scratch/ only its own run's
        base = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplrepo-")))
        (base / "real").mkdir()
        (base / "link").symlink_to(base / "real")
        worktree = base / "link" / ".worktrees" / "real-KEY-7"
        fake_worktree(base / "link", worktree)
        fake_worktree(base / "link", base / "link" / ".worktrees" / "real-KEY-8")
        scope = os_sandbox.write_scope(doc(checkout=str(base / "link"),
                                           os_sandbox={"allow_write": ["~/.npm"]}),
                                       worktree=worktree, task="KEY-7")
        home = os.environ["PPL_HOME"]
        git_dir = base / "real" / ".git"
        self.assertEqual(scope, [home + "/runs/KEY-7", home + "/labels", home + "/scratch/KEY-7",
                                 str(git_dir / "objects"), str(git_dir / "refs"),
                                 str(git_dir / "logs"), str(git_dir / "worktrees" / "real-KEY-7"),
                                 str(base / "real" / ".worktrees" / "real-KEY-7"),
                                 os.path.realpath(os.path.expanduser("~/.npm"))])
        for entry in (base / "real", base / "real" / ".worktrees", git_dir,
                      git_dir / "worktrees", git_dir / "worktrees" / "real-KEY-8",
                      git_dir / "packed-refs", home + "/runs", home + "/runs/KEY-8",
                      home + "/runs/_archive", home + "/scratch", home + "/scratch/KEY-8"):
            self.assertNotIn(str(entry), scope)
        with self.assertRaises(ValueError):
            os_sandbox.write_scope(doc(checkout=str(base / "link")), worktree=worktree)

    def test_a_worktree_with_no_git_dir_of_the_checkout_is_refused(self):
        # rule: R-LANE-CROSS-WRITE
        base = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplrepo-")))
        elsewhere = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplother-")))
        worktree = base / ".worktrees" / "repo-KEY-7"
        worktree.mkdir(parents=True)
        for text in (None, "not a gitdir\n", "gitdir: %s/.git/worktrees/x\n" % elsewhere):
            if text is not None:
                (worktree / ".git").write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(PplError) as caught:
                os_sandbox.write_scope(doc(checkout=str(base)), worktree=worktree, task="KEY-7")
            self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")

    def test_a_lanes_profile_write_denies_the_shared_git_config_hooks_and_info(self):
        # rule: R-LANE-SHARED-GIT-DIR
        # R33, R35: what every lane and the operator's own git read and run in the checkout's
        # .git, and the gitdir and commondir of the lane's own git dir, are write-denied, and
        # a denyWrite entry wins over an allowWrite one
        base = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplrepo-")))
        worktree = base / ".worktrees" / "repo-KEY-7"
        own = fake_worktree(base, worktree)
        lane_doc = doc(forge="gitlab", checkout=str(base))
        denied = [str(base / ".git" / name) for name in ("config", "hooks", "info")]
        denied += [str(own / "gitdir"), str(own / "commondir")]
        lane = os_sandbox.profile(lane_doc, "/srv/ws", PINS, worktree=worktree, task="KEY-7")
        filesystem = lane["sandbox"]["filesystem"]
        self.assertIn(str(own), filesystem["allowWrite"])
        self.assertNotIn(str(base / ".git"), filesystem["allowWrite"])
        for entry in denied:
            self.assertIn(entry, filesystem["denyWrite"])
            self.assertNotIn(entry, filesystem["allowWrite"])
        facts = os_sandbox.facts(lane_doc, "/srv/ws", PINS, worktree=worktree, task="KEY-7")
        self.assertEqual(facts["write_scope"], filesystem["allowWrite"])
        for entry in denied:
            self.assertIn(entry, facts["deny_write"])
        # outside a lane the whole checkout is in scope, as before
        plain = os_sandbox.profile(lane_doc, "/srv/ws", PINS)["sandbox"]["filesystem"]
        for entry in denied:
            self.assertNotIn(entry, plain["denyWrite"])

    def test_git_in_a_worktree_writes_only_objects_refs_logs_and_its_git_dir(self):
        # R33, R35: what a lane runs in its worktree (add, commit, branch, stash, checkout,
        # rebase, reset, switch) writes in the checkout's .git only objects, refs, logs and
        # its own worktrees/<id>, so the profile leaves the rest out of the lane's reach: the
        # shared config, hooks, info and packed-refs, and another lane's git dir
        root = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplgit-")))
        checkout, worktree = root / "checkout", root / "worktree"
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

        def git(directory, *args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                            "-c", "commit.gpgsign=false", "-c", "gc.auto=0",
                            "-c", "maintenance.auto=false", "-C", str(directory)] + list(args),
                           check=True, capture_output=True, env=env, timeout=60)

        checkout.mkdir()
        git(checkout, "init", "-q", "-b", "main")
        (checkout / "a.txt").write_text("a\n", encoding="utf-8")
        git(checkout, "add", "a.txt")
        git(checkout, "commit", "-q", "-m", "base")
        git(checkout, "worktree", "add", "-q", "-b", "lane", str(worktree))
        git(checkout, "worktree", "add", "-q", "-b", "other", str(root / "other"))
        common = checkout / ".git"
        own = Path(os_sandbox.worktree_git_dir(doc(checkout=str(checkout)), worktree))
        allowed = [common / name for name in ("objects", "refs", "logs")] + [own]

        def outside():
            found = {}
            for path in sorted(common.rglob("*")):
                if path.is_file() and not any(path == top or top in path.parents
                                              for top in allowed):
                    found[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
            return found

        before = outside()
        (worktree / "b.txt").write_text("b\n", encoding="utf-8")
        git(worktree, "add", "b.txt")
        git(worktree, "commit", "-q", "-m", "lane work")
        git(worktree, "branch", "side", "main")
        (worktree / "a.txt").write_text("changed\n", encoding="utf-8")
        git(worktree, "stash", "push", "-q")
        git(worktree, "stash", "pop", "-q")
        git(worktree, "checkout", "-q", "--", "a.txt")
        git(worktree, "checkout", "-q", "side")
        (worktree / "c.txt").write_text("c\n", encoding="utf-8")
        git(worktree, "add", "c.txt")
        git(worktree, "commit", "-q", "-m", "side work")
        git(worktree, "checkout", "-q", "lane")
        git(worktree, "rebase", "-q", "side")
        git(worktree, "reset", "-q", "--soft", "HEAD~1")
        git(worktree, "reset", "-q", "--hard", "HEAD")
        git(worktree, "switch", "-q", "-c", "lane-2")
        self.assertEqual(outside(), before)

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
                      os.path.realpath("/srv/ws")):
            self.assertIn(os.path.realpath(entry), deny)
        self.assertEqual(len(deny), len(set(deny)))

    def test_the_profile_denies_reading_the_github_and_ssh_credentials(self):
        # what a role could copy into a commit or a scratch body the orchestrator publishes
        moved = {"GH_CONFIG_DIR": "/srv/ghcfg", "XDG_CONFIG_HOME": "/srv/xdg"}
        with mock.patch.dict(os.environ, moved):
            deny = self.profile()["sandbox"]["filesystem"]["denyRead"]
        for entry in ("~/.config/gh", "~/.git-credentials", "~/.config/git/credentials",
                      "~/.netrc", "~/.ssh", "/srv/ghcfg", "/srv/xdg/gh",
                      "/srv/xdg/git/credentials"):
            self.assertIn(os.path.realpath(os.path.expanduser(entry)), deny)
        self.assertEqual(len(deny), len(set(deny)))

    def test_allow_ssh_read_keeps_ssh_readable(self):
        deny = self.profile(os_sandbox={"allow_ssh_read": True})["sandbox"]["filesystem"][
            "denyRead"]
        self.assertNotIn(os.path.realpath(os.path.expanduser("~/.ssh")), deny)
        self.assertIn(os.path.realpath(os.path.expanduser("~/.config/gh")), deny)

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
