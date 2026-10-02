import os
import shutil
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import config, harness, paths, routing
from ppl.errors import PplError
from ppl.studio import drafts, files

GATES = "harness:demo:skill/gates.md"


class FilesTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        harness.add(str(paths.defaults_dir() / "harnesses" / "example-generic"), name="demo")


class TestKeys(FilesTestCase):
    def test_the_routing_key_and_a_harness_file_key_resolve(self):
        self.assertEqual(files.resolve("routing"), paths.routing_file())
        self.assertEqual(files.resolve(GATES), paths.harness_dir("demo") / "skill" / "gates.md")
        self.assertEqual(files.resolve("harness:demo:templates/ppl-verifier.part"),
                         paths.harness_dir("demo") / "templates" / "ppl-verifier.part")

    def test_a_key_naming_a_path_outside_the_harness_files_is_refused(self):
        # rule: R-STUDIO-NO-RAW-PATHS
        for key in ("/etc/passwd", "harness:demo:../../config.toml", "harness:demo:hooks/x.py",
                    "harness:../x:harness.toml", "harness:nope:harness.toml",
                    "harness:demo:templates/ppl-rogue.part", "config", None):
            with self.assertRaises(PplError) as caught:
                files.resolve(key)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", key)

    def test_a_history_version_that_is_not_a_timestamp_is_refused(self):
        # rule: R-STUDIO-NO-RAW-PATHS
        for version in ("../../routing.toml", "latest", "", None):
            with self.assertRaises(PplError) as caught:
                files.version_text("routing", version)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID")


class TestKeyContainment(FilesTestCase):
    def test_a_harness_name_that_draft_names_refuse_is_refused(self):
        # rule: R-STUDIO-NO-RAW-PATHS
        for reserved in ("demo.replacing", "demo.replaced", "demo.git"):
            shutil.copytree(paths.harness_dir("demo"), paths.harnesses_dir() / reserved)
            with self.assertRaises(PplError) as caught:
                files.resolve("harness:%s:harness.toml" % reserved)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", reserved)

    def test_a_key_carrying_a_line_break_is_refused(self):
        # rule: R-STUDIO-NO-RAW-PATHS
        for key in (GATES + "\n", "routing\n", "harness:demo\n:harness.toml"):
            with self.assertRaises(PplError) as caught:
                files.resolve(key)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", repr(key))

    def test_a_symlink_leaving_the_harness_directory_is_refused(self):
        # rule: R-STUDIO-NO-RAW-PATHS
        outside = Path(tmphome.mkdtemp(prefix="pploutside-"))
        (outside / "secret.md").write_text("secret\n", encoding="utf-8")
        folder = paths.harness_dir("demo")
        (folder / "skill" / "gates.md").unlink()
        (folder / "skill" / "gates.md").symlink_to(outside / "secret.md")
        shutil.rmtree(folder / "templates")
        (folder / "templates").symlink_to(outside, target_is_directory=True)
        for key in (GATES, "harness:demo:templates/ppl-verifier.part"):
            for call in (files.resolve, files.read):
                with self.assertRaises(PplError) as caught:
                    call(key)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID", key)
        with self.assertRaises(PplError) as caught:
            files.save(GATES, "overwritten\n", files.digest(b"secret\n"))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual((outside / "secret.md").read_text(encoding="utf-8"), "secret\n")
        with self.assertRaises(PplError) as caught:
            files.harness_fragments("demo")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_the_fragments_of_an_installed_harness_are_read_by_name_only(self):
        fragments = files.harness_fragments("demo")
        self.assertEqual(sorted(fragments), ["skill/baseline.md", "skill/gates.md",
                                             "templates/ppl-code-worker.part",
                                             "templates/ppl-test-worker.part"])
        self.assertEqual(fragments["skill/gates.md"], files.read(GATES)["text"])
        for name in ("../demo", "../../x", "demo.git", None, "nope"):
            with self.assertRaises(PplError) as caught:
                files.harness_fragments(name)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", name)

    def test_a_symlink_inside_the_harness_directory_is_followed(self):
        folder = paths.harness_dir("demo")
        (folder / "skill" / "extra-gates.md").symlink_to("gates.md")
        self.assertEqual(files.read("harness:demo:skill/extra-gates.md")["text"],
                         files.read(GATES)["text"])


class TestSave(FilesTestCase):
    def test_a_save_with_the_current_hash_writes_and_keeps_history(self):
        current = files.read(GATES)
        new_hash = files.save(GATES, "Targets: `make all`.\n", current["hash"])
        self.assertEqual(files.read(GATES)["text"], "Targets: `make all`.\n")
        self.assertEqual(new_hash, files.digest(b"Targets: `make all`.\n"))
        versions = files.versions(GATES)
        self.assertEqual(len(versions), 1)
        self.assertEqual(files.version_text(GATES, versions[0]["version"]), current["text"])

    def test_a_save_with_a_stale_hash_is_refused(self):
        # rule: R-STUDIO-CHANGED-ON-DISK
        current = files.read("routing")
        paths.routing_file().write_text(current["text"] + "\n# edited in a terminal\n",
                                        encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            files.save("routing", current["text"], current["hash"])
        self.assertEqual(caught.exception.code, "CHANGED_ON_DISK")
        self.assertIn("edited in a terminal", paths.routing_file().read_text(encoding="utf-8"))

    def test_a_batch_with_one_stale_hash_writes_nothing(self):
        # rule: R-STUDIO-CHANGED-ON-DISK
        gates = files.read(GATES)
        with self.assertRaises(PplError) as caught:
            files.save_many([(GATES, "new\n", gates["hash"]),
                             ("harness:demo:skill/baseline.md", "new\n", "0" * 64)])
        self.assertEqual(caught.exception.code, "CHANGED_ON_DISK")
        self.assertEqual(files.read(GATES)["text"], gates["text"])
        self.assertEqual(files.versions(GATES), [])

    def test_an_unchanged_save_keeps_no_history(self):
        current = files.read(GATES)
        files.save(GATES, current["text"], current["hash"])
        self.assertEqual(files.versions(GATES), [])

    def test_a_missing_file_is_read_as_empty_and_can_be_created(self):
        key = "harness:demo:templates/ppl-verifier.part"
        current = files.read(key)
        self.assertEqual(current["text"], "")
        files.save(key, "- Check the migration notes.\n", current["hash"])
        self.assertEqual(files.read(key)["text"], "- Check the migration notes.\n")

    def test_history_keeps_only_the_newest_versions(self):
        with mock.patch.object(files, "KEEP", 3):
            for index in range(5):
                current = files.read(GATES)
                files.save(GATES, "version %d\n" % index, current["hash"])
        texts = [files.version_text(GATES, item["version"]) for item in files.versions(GATES)]
        self.assertEqual(texts, ["version 3\n", "version 2\n", "version 1\n"])

    def test_restore_goes_through_validation(self):
        current = files.read("routing")
        broken = current["text"].replace('tier = "good"', 'tier = "giant"', 1)
        files.save("routing", broken, current["hash"])
        fixed = files.read("routing")
        files.save("routing", current["text"], fixed["hash"])
        bad_version = files.versions("routing")[0]["version"]
        with self.assertRaises(PplError) as caught:
            files.restore("routing", bad_version, files.read("routing")["hash"])
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        good_version = files.versions("routing")[1]["version"]
        files.restore("routing", good_version, files.read("routing")["hash"])
        self.assertEqual(files.read("routing")["text"], current["text"])


class TestSaveHardening(FilesTestCase):
    def test_a_batch_naming_one_key_twice_is_refused(self):
        gates = files.read(GATES)
        with self.assertRaises(PplError) as caught:
            files.save_many([(GATES, "first\n", gates["hash"]), (GATES, "second\n", gates["hash"])])
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(files.read(GATES)["text"], gates["text"])

    def test_content_that_is_not_text_or_a_hash_that_is_not_a_string_is_refused(self):
        gates = files.read(GATES)
        for text, expected in ((b"bytes", gates["hash"]), ("lone \ud800 surrogate", gates["hash"]),
                               ("new\n", None), ("new\n", 7)):
            with self.assertRaises(PplError) as caught:
                files.save(GATES, text, expected)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", (text, expected))
        self.assertEqual(files.read(GATES)["text"], gates["text"])

    def test_a_failed_write_leaves_the_file_and_no_temporary_behind(self):
        gates = files.read(GATES)
        baseline = files.read("harness:demo:skill/baseline.md")
        with mock.patch.object(files.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                files.save_many([(GATES, "new\n", gates["hash"]),
                                 ("harness:demo:skill/baseline.md", "new\n", baseline["hash"])])
        self.assertEqual(files.read(GATES), gates)
        self.assertEqual(files.read("harness:demo:skill/baseline.md"), baseline)
        self.assertEqual(sorted(item.name for item in paths.harness_dir("demo").rglob("*.studio-tmp")),
                         [])

    def test_a_save_in_a_harness_folder_that_does_not_exist_yet_creates_it(self):
        folder = paths.harness_dir("demo")
        shutil.rmtree(folder / "templates")
        key = "harness:demo:templates/ppl-verifier.part"
        files.save(key, "- Check the migration notes.\n", files.read(key)["hash"])
        self.assertEqual((folder / "templates" / "ppl-verifier.part").read_text(encoding="utf-8"),
                         "- Check the migration notes.\n")


def snapshot(folder):
    return {str(item.relative_to(folder)): item.read_bytes()
            for item in sorted(Path(folder).rglob("*")) if item.is_file()}


def nameless(text):
    return text.replace('name = "example-generic"\n', "", 1)


class TestSaveThroughLinks(FilesTestCase):
    def test_a_save_through_a_symlinked_routing_file_keeps_the_link(self):
        outside = Path(tmphome.mkdtemp(prefix="pploutside-"))
        real = outside / "routing.toml"
        real.write_bytes(paths.routing_file().read_bytes())
        real.chmod(0o640)
        paths.routing_file().unlink()
        paths.routing_file().symlink_to(real)
        current = files.read("routing")
        edited = current["text"] + "\n# edited in the studio\n"
        files.save("routing", edited, current["hash"])
        self.assertTrue(paths.routing_file().is_symlink())
        self.assertEqual(os.readlink(paths.routing_file()), str(real))
        self.assertEqual(real.read_text(encoding="utf-8"), edited)
        self.assertEqual(real.stat().st_mode & 0o777, 0o640)
        self.assertEqual(sorted(item.name for item in outside.iterdir()), ["routing.toml"])
        self.assertEqual(files.version_text("routing", files.versions("routing")[0]["version"]),
                         current["text"])

    def test_a_save_through_an_in_harness_fragment_link_keeps_the_link(self):
        folder = paths.harness_dir("demo")
        link = folder / "skill" / "extra-gates.md"
        link.symlink_to("gates.md")
        key = "harness:demo:skill/extra-gates.md"
        files.save(key, "Targets: `make all`.\n", files.read(key)["hash"])
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "gates.md")
        self.assertEqual((folder / "skill" / "gates.md").read_text(encoding="utf-8"),
                         "Targets: `make all`.\n")
        self.assertEqual(sorted(item.name for item in (folder / "skill").iterdir()),
                         ["baseline.md", "extra-gates.md", "gates.md"])

    def test_a_target_that_resolves_outside_the_harness_after_the_key_check_is_refused(self):
        # rule: R-STUDIO-NO-RAW-PATHS
        outside = Path(tmphome.mkdtemp(prefix="pploutside-"))
        secret = outside / "gates.md"
        secret.write_bytes(files.resolve(GATES).read_bytes())
        gates = files.read(GATES)
        with mock.patch.object(files, "_target", return_value=secret.resolve()):
            with self.assertRaises(PplError) as caught:
                files.save(GATES, "overwritten\n", gates["hash"])
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(secret.read_bytes(), files.resolve(GATES).read_bytes())
        self.assertEqual(sorted(item.name for item in outside.iterdir()), ["gates.md"])
        self.assertEqual(files.read(GATES), gates)

    def test_a_save_keeps_the_permission_bits_of_a_regular_file(self):
        path = paths.harness_dir("demo") / "skill" / "gates.md"
        path.chmod(0o600)
        files.save(GATES, "Targets: `make all`.\n", files.read(GATES)["hash"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_batch_naming_two_links_to_one_file_is_refused(self):
        folder = paths.harness_dir("demo")
        (folder / "skill" / "extra-gates.md").symlink_to("gates.md")
        gates = files.read(GATES)
        link = "harness:demo:skill/extra-gates.md"
        with self.assertRaises(PplError) as caught:
            files.save_many([(GATES, "first\n", gates["hash"]), (link, "second\n", gates["hash"])])
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(files.read(GATES)["text"], gates["text"])


class TestHistoryOrder(FilesTestCase):
    def test_a_clock_stepped_back_does_not_sort_a_new_version_below_older_ones(self):
        folder = paths.home() / "studio" / "history" / "routing"
        folder.mkdir(parents=True)
        (folder / "29990101T000000000000Z").write_bytes(b"from the future\n")
        current = files.read("routing")
        files.save("routing", current["text"] + "\n# edited\n", current["hash"])
        newest = files.versions("routing")[0]["version"]
        self.assertGreater(newest, "29990101T000000000000Z")
        self.assertEqual(files.version_text("routing", newest), current["text"])

    def test_nothing_is_pruned_when_a_replace_of_the_batch_fails(self):
        real_replace = os.replace
        with mock.patch.object(files, "KEEP", 2):
            for index in range(2):
                files.save(GATES, "version %d\n" % index, files.read(GATES)["hash"])
            self.assertEqual(len(files.versions(GATES)), 2)
            target = files.resolve(GATES).resolve()

            def replace(source, destination):
                if Path(destination) == target:
                    raise OSError("disk gone")
                return real_replace(source, destination)
            with mock.patch.object(files.os, "replace", side_effect=replace):
                with self.assertRaises(OSError):
                    files.save(GATES, "never lands\n", files.read(GATES)["hash"])
            self.assertEqual(len(files.versions(GATES)), 3)
            files.save(GATES, "lands\n", files.read(GATES)["hash"])
            self.assertEqual(len(files.versions(GATES)), 2)


class TestCheckHarness(FilesTestCase):
    def test_the_installed_harness_checks_clean_and_a_change_is_checked_with_it(self):
        self.assertTrue(files.check_harness("demo")["ok"])
        self.assertTrue(files.check_harness("demo", [("skill/gates.md", "Targets: `make all`.\n")])["ok"])
        result = files.check_harness("demo", [("harness.toml", nameless(
            files.read("harness:demo:harness.toml")["text"]))])
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_a_copy_that_does_not_load_comes_back_as_a_result_never_a_raise(self):
        for text in ("this is = not toml [", "", "[harness]\n"):
            result = files.check_harness("demo", [("harness.toml", text)])
            self.assertFalse(result["ok"], text)
            self.assertTrue(result["errors"], text)

    def test_a_refusal_names_the_installed_harness_not_the_scratch_copy(self):
        result = files.check_harness("demo", [("harness.toml", "")])
        self.assertFalse(result["ok"])
        for message in result["errors"]:
            self.assertNotIn(".check-", message)
            self.assertIn(str(paths.harness_dir("demo") / "harness.toml"), message)

    def test_a_value_of_the_wrong_type_that_breaks_rendering_comes_back_as_a_result(self):
        key = "harness:demo:harness.toml"
        broken = files.read(key)["text"].replace(
            'worktrees = "/home/dev/src/project/.worktrees"', "worktrees = 1", 1)
        self.assertIn("worktrees = 1", broken)
        result = files.check_harness("demo", [("harness.toml", broken)])
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        with self.assertRaises(PplError) as caught:
            files.validate(key, broken)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_a_fragment_that_is_not_utf8_text_comes_back_as_a_result(self):
        (paths.harness_dir("demo") / "skill" / "gates.md").write_bytes(b"\xff\xfe not text")
        result = files.check_harness("demo")
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        with self.assertRaises(PplError) as caught:
            files.validate("harness:demo:skill/baseline.md", "fine\n")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_a_change_to_a_file_or_a_harness_that_is_not_there_is_refused(self):
        for name, changes in (("nope", ()), ("demo.git", ()), ("demo", [("hooks/x.py", "x\n")]),
                              ("demo", [("skill/gates.md", None)])):
            with self.assertRaises(PplError) as caught:
                files.check_harness(name, changes)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", (name, changes))

    def test_the_check_leaves_the_harness_and_a_wizard_draft_of_the_same_name_alone(self):
        doc = tomllib.loads(files.read("harness:demo:harness.toml")["text"])
        drafts.write("demo", doc, files.harness_fragments("demo"))
        draft = drafts.drafts_dir() / "demo"
        (draft / "skill" / "gates.md").write_text("a wizard edit\n", encoding="utf-8")
        before = (snapshot(draft), snapshot(paths.harness_dir("demo")), sorted(
            item.name for item in drafts.drafts_dir().iterdir()))
        files.validate(GATES, "Targets: `make all`.\n")
        files.validate("harness:demo:harness.toml", files.read("harness:demo:harness.toml")["text"])
        files.check_harness("demo", [("skill/gates.md", "changed\n")])
        with self.assertRaises(PplError):
            files.validate("harness:demo:harness.toml", "this is = not toml [")
        after = (snapshot(draft), snapshot(paths.harness_dir("demo")), sorted(
            item.name for item in drafts.drafts_dir().iterdir()))
        self.assertEqual(after, before)

    def test_the_copy_holds_the_exact_bytes_of_the_change_and_follows_links(self):
        folder = paths.harness_dir("demo")
        (folder / "skill" / "extra-gates.md").symlink_to("gates.md")
        seen = {}
        real_verify = drafts.verify

        def verify(path, name):
            seen["parent"] = Path(path).parent.parent
            seen["name"] = Path(path).name
            seen["files"] = snapshot(path)
            return real_verify(path, name)
        with mock.patch.object(drafts, "verify", side_effect=verify):
            files.check_harness("demo", [("skill/extra-gates.md", "no trailing newline")])
        self.assertEqual(seen["parent"], drafts.drafts_dir())
        self.assertEqual(seen["name"], "demo")
        self.assertEqual(seen["files"]["skill/gates.md"], b"no trailing newline")
        self.assertEqual(seen["files"]["skill/extra-gates.md"], b"no trailing newline")
        self.assertEqual(seen["files"]["skill/baseline.md"],
                         (folder / "skill" / "baseline.md").read_bytes())
        self.assertEqual((folder / "skill" / "gates.md").read_text(encoding="utf-8").strip(),
                         files.read(GATES)["text"].strip())

    def test_a_harness_toml_with_no_name_is_refused_before_it_is_saved(self):
        key = "harness:demo:harness.toml"
        broken = nameless(files.read(key)["text"])
        self.assertNotEqual(broken, files.read(key)["text"])
        with self.assertRaises(PplError) as caught:
            files.validate(key, broken)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_restoring_a_harness_toml_with_no_name_is_refused_and_leaves_the_file(self):
        key = "harness:demo:harness.toml"
        good = files.read(key)
        files.save(key, nameless(good["text"]), good["hash"])
        files.save(key, good["text"], files.read(key)["hash"])
        nameless_version = files.versions(key)[0]["version"]
        before = files.read(key)
        with self.assertRaises(PplError) as caught:
            files.restore(key, nameless_version, before["hash"])
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertEqual(files.read(key), before)
        self.assertEqual(len(files.versions(key)), 2)


class TestValidate(FilesTestCase):
    def test_a_routing_file_ppl_could_not_load_is_refused(self):
        current = files.read("routing")
        roles_as_text = "[roles]\n" + "".join('%s = "x"\n' % role for role in routing.REQUIRED_ROLES)
        for text in ("this is = not toml [", current["text"].replace('tier = "good"', 'tier = "giant"', 1),
                     "", roles_as_text):
            with self.assertRaises(PplError) as caught:
                files.validate("routing", text)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", text[:20])
        files.validate("routing", current["text"])

    def test_a_harness_file_that_leaves_the_harness_invalid_is_refused(self):
        current = files.read("harness:demo:harness.toml")
        for text in ("this is = not toml [",
                     current["text"].replace('path = "/home/dev/src/project"', 'path = "relative"', 1)):
            with self.assertRaises(PplError) as caught:
                files.validate("harness:demo:harness.toml", text)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", text[:20])
        files.validate("harness:demo:harness.toml", current["text"])

    def test_a_fragment_is_validated_with_the_harness_on_disk(self):
        files.validate(GATES, "Targets: `make all`.\n")
        paths.harness_dir("demo").joinpath("harness.toml").write_text("not toml [", encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            files.validate(GATES, "Targets: `make all`.\n")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_a_key_that_names_no_file_is_refused_and_so_is_content_that_is_not_text(self):
        for key, text in (("harness:nope:skill/gates.md", "x\n"), (GATES, None), ("routing", None)):
            with self.assertRaises(PplError) as caught:
                files.validate(key, text)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", key)


class TestFileRoutes(FilesTestCase):
    def test_a_file_and_its_history_are_read_by_key(self):
        status, current, _ = panel_client.call("GET", "/api/files?key=" + GATES)
        self.assertEqual(status, 200)
        files.save(GATES, "changed\n", current["hash"])
        status, history, _ = panel_client.call("GET", "/api/history?key=" + GATES)
        self.assertEqual(status, 200)
        version = history["versions"][0]["version"]
        status, old, _ = panel_client.call(
            "GET", "/api/history/version?key=%s&version=%s" % (GATES, version))
        self.assertEqual(old["text"], current["text"])

    def test_a_restore_through_the_api_needs_the_current_hash(self):
        current = files.read(GATES)
        files.save(GATES, "changed\n", current["hash"])
        version = files.versions(GATES)[0]["version"]
        status, payload, _ = panel_client.call("POST", "/api/history/restore",
                                               body={"key": GATES, "version": version, "hash": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "CHANGED_ON_DISK")
        status, payload, _ = panel_client.call(
            "POST", "/api/history/restore",
            body={"key": GATES, "version": version, "hash": files.read(GATES)["hash"]})
        self.assertEqual(status, 200)
        self.assertEqual(files.read(GATES)["text"], current["text"])

    def test_an_unknown_key_through_the_api_is_refused(self):
        status, payload, _ = panel_client.call("GET", "/api/files?key=/etc/passwd")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_a_missing_key_or_a_bad_version_through_the_api_is_refused(self):
        status, payload, _ = panel_client.call("GET", "/api/files")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        status, payload, _ = panel_client.call("GET", "/api/history/version?key=routing&version=latest")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        status, payload, _ = panel_client.call(
            "GET", "/api/history/version?key=routing&version=20260101T000000000000Z")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
