"""Run as `python3 -m ppl.studio.playground_child` with PPL_HOME pointing at a
throwaway directory: it builds the fake run, and the lane that binds the
playground's hook call to it. Reads {"task", "repo", "snapshot", "boundary"} as
JSON on stdin."""
import json
import sys

from .. import lanes, paths, run, store


def main():
    spec = json.loads(sys.stdin.read())
    entry = lanes.claim(spec["task"], spec["snapshot"]["name"], "0" * 40, spec["repo"],
                        spec["repo"], "00000000-0000-4000-8000-000000000000", 1, 1)
    run.init(spec["task"], "https://tracker.invalid/%s" % spec["task"], spec["repo"],
             model_check="skipped", harness={"name": spec["snapshot"]["name"]},
             lane={"number": entry["lane"], "start_dir": entry["start_dir"],
                   "session_id": entry["session_id"]})
    folder = paths.run_dir(spec["task"])
    store.write_json(folder / "harness.json", spec["snapshot"])
    store.write_json(folder / "boundary.json", spec["boundary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
