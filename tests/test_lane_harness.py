import io
import os
import shutil
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import fixtures
from ppl import harness, lane_harness, paths
from ppl.errors import PplError

IDENT = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(directory, *args):
    out = subprocess.run(["git", *IDENT, "-C", str(directory), *args], check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def make_repo(files=None):
    repo = Path(tmphome.mkdtemp(prefix="pplharnessrepo-"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for name, text in (files or {"harness.toml": "a = 1\n"}).items():
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    return repo


class TestCommit(unittest.TestCase):
    def test_a_clean_local_harness_uses_head(self):
        repo = make_repo()
        self.assertEqual(lane_harness.commit(repo), git(repo, "rev-parse", "HEAD"))

    def test_a_harness_with_an_origin_uses_the_fetched_default_head(self):
        bare = Path(tmphome.mkdtemp(prefix="pplbare-"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        repo = make_repo()
        git(repo, "remote", "add", "origin", str(bare))
        git(repo, "push", "-q", "origin", "main")
        other = Path(tmphome.mkdtemp(prefix="pplother-"))
        shutil.rmtree(other)
        subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
        (other / "new.txt").write_text("x", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "second")
        git(other, "push", "-q", "origin", "main")
        pushed = git(other, "rev-parse", "HEAD")
        self.assertNotEqual(pushed, git(repo, "rev-parse", "HEAD"))
        self.assertEqual(lane_harness.commit(repo), pushed)

    def test_a_failed_fetch_is_external_blocked(self):
        repo = make_repo()
        git(repo, "remote", "add", "origin", str(repo / "missing"))
        with self.assertRaises(PplError) as caught:
            lane_harness.commit(repo)
        self.assertEqual(caught.exception.code, "EXTERNAL_BLOCKED")

    def test_a_dirty_harness_is_refused_naming_the_paths(self):
        repo = make_repo()
        (repo / "harness.toml").write_text("a = 2\n", encoding="utf-8")
        (repo / "extra.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            lane_harness.commit(repo)
        self.assertEqual(caught.exception.code, "OWNER_DECISION")
        self.assertIn("harness.toml", caught.exception.message)
        self.assertIn("extra.txt", caught.exception.message)
        self.assertIn("commit them in the harness repository", caught.exception.message)

    def test_a_plain_directory_is_refused_naming_the_reimport(self):
        plain = Path(tmphome.mkdtemp(prefix="pplplain-")) / "mine"
        plain.mkdir()
        with self.assertRaises(PplError) as caught:
            lane_harness.commit(plain)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        message = caught.exception.message
        self.assertIn("ppl harness add --source %s --name mine" % plain, message)
        self.assertIn("git -C %s init -b main && git -C %s add -A && git -C %s commit -m "
                      "\"Import mine\"" % (plain, plain, plain), message)

    def test_a_subdirectory_of_another_repository_is_not_a_harness_repository(self):
        repo = make_repo()
        (repo / "sub").mkdir()
        with self.assertRaises(PplError) as caught:
            lane_harness.commit(repo / "sub")
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")

    def test_a_git_that_cannot_start_is_a_ppl_error(self):
        repo = make_repo()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with self.assertRaises(PplError) as caught:
                lane_harness.commit(repo)
        self.assertEqual(caught.exception.code, "EXTERNAL_BLOCKED")

    def test_a_git_that_times_out_is_a_ppl_error(self):
        repo = make_repo()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            with self.assertRaises(PplError) as caught:
                lane_harness.commit(repo)
        self.assertEqual(caught.exception.code, "EXTERNAL_BLOCKED")


class TestExport(unittest.TestCase):
    def test_export_writes_the_tree_at_the_commit_not_the_working_copy(self):
        repo = make_repo({"harness.toml": "a = 1\n", "sub.txt": "kept\n"})
        sha = git(repo, "rev-parse", "HEAD")
        (repo / "harness.toml").write_text("a = 2\n", encoding="utf-8")
        root = Path(tmphome.mkdtemp(prefix="pplexport-"))
        out = lane_harness.export(repo, sha, root, "h")
        self.assertEqual(out, root / "h")
        self.assertEqual((out / "harness.toml").read_text(encoding="utf-8"), "a = 1\n")
        self.assertEqual((out / "sub.txt").read_text(encoding="utf-8"), "kept\n")
        self.assertFalse((out / ".git").exists())

    def test_export_refuses_a_member_escaping_the_destination(self):
        for member in ("../x", "/abs/x"):
            with self.subTest(member=member):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    info = tarfile.TarInfo(member)
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
                root = Path(tmphome.mkdtemp(prefix="pplexport-"))
                with mock.patch.object(lane_harness, "_archive", return_value=buffer.getvalue()):
                    with self.assertRaises(PplError) as caught:
                        lane_harness.export("unused", "sha", root, "h")
                self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
                self.assertFalse((root / "x").exists())
                self.assertFalse((root.parent / "x").exists())

    def test_export_refuses_a_symlink_pointing_outside(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../etc"
            archive.addfile(info)
        root = Path(tmphome.mkdtemp(prefix="pplexport-"))
        with mock.patch.object(lane_harness, "_archive", return_value=buffer.getvalue()):
            with self.assertRaises(PplError):
                lane_harness.export("unused", "sha", root, "h")


class TestAddImport(unittest.TestCase):
    def test_add_from_a_plain_directory_makes_a_git_repository(self):
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        with fixtures.git_identity():
            target = harness.add(str(source), name="plain-import")
        self.assertEqual(git(target, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(git(target, "log", "-1", "--format=%s"), "Import plain-import")
        self.assertEqual(git(target, "status", "--porcelain"), "")
        self.assertEqual(git(target, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(lane_harness.commit(target), git(target, "rev-parse", "HEAD"))

    def test_add_without_a_git_identity_is_refused(self):
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        empty_home = tmphome.mkdtemp(prefix="pplnohome-")
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": empty_home}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(PplError) as caught:
                harness.add(str(source), name="no-identity")
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("git config", caught.exception.message)
        self.assertIn("user.name", caught.exception.message)
        self.assertFalse(paths.harness_dir("no-identity").exists())

    def test_add_keeps_a_copied_git_repository_as_it_is(self):
        repo = make_repo()
        shutil.copy(fixtures.SOURCE / "defaults" / "harnesses" / "example-generic" / "harness.toml",
                    repo / "harness.toml")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "real")
        before = git(repo, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/dev/null",
                                          "GIT_CONFIG_NOSYSTEM": "1"}):
            target = harness.add(str(repo), name="kept-repo")
        self.assertEqual(git(target, "rev-parse", "HEAD"), before)



def planted(name):
    """(an executable script that appends to a marker file and fails, that marker file)."""
    folder = Path(tmphome.mkdtemp(prefix="pplplanted-"))
    marker = folder / ("%s.ran" % name)
    script = folder / name
    script.write_text("#!/bin/sh\necho ran >> %s\nexit 1\n" % marker, encoding="utf-8")
    script.chmod(0o755)
    return script, marker


class TestPinnedGit(unittest.TestCase):
    def test_an_fsmonitor_in_the_harness_config_does_not_run(self):
        repo = make_repo()
        script, marker = planted("fsmonitor")
        git(repo, "config", "core.fsmonitor", str(script))
        self.assertEqual(lane_harness.commit(repo), git(repo, "rev-parse", "HEAD"))
        self.assertFalse(marker.exists())

    def test_the_import_commit_signs_with_the_operators_gpg_program(self):
        # the pinned gpg.program= would fail every signed import, so the import commit keeps it
        script, marker = planted("gpg")
        config = Path(tmphome.mkdtemp(prefix="pplgitcfg-")) / "gitconfig"
        config.write_text("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = true\n"
                          "[gpg]\n\tprogram = %s\n" % script, encoding="utf-8")
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config),
                                          "GIT_CONFIG_NOSYSTEM": "1"}):
            with self.assertRaises(PplError) as caught:
                harness.add(str(source), name="signed-import")
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertTrue(marker.exists())

    def test_the_import_takes_every_file_whatever_the_operators_excludes_file(self):
        # ledger 93: the import's git add obeyed the operator's global core.excludesFile, so a
        # harness file it matched was left out of the import commit without a word
        ignore = Path(tmphome.mkdtemp(prefix="pplignore-")) / "ignore"
        ignore.write_text("*.toml\n", encoding="utf-8")
        config = Path(tmphome.mkdtemp(prefix="pplgitcfg-")) / "gitconfig"
        config.write_text("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = false\n"
                          "[core]\n\texcludesFile = %s\n" % ignore, encoding="utf-8")
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config),
                                          "GIT_CONFIG_NOSYSTEM": "1"}):
            harness.add(str(source), name="excluded-import")
        listed = git(paths.harness_dir("excluded-import"), "ls-files").split()
        self.assertIn("harness.toml", listed)


if __name__ == "__main__":
    unittest.main()
