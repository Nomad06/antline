"""The browser wizard's view of ppl setup: the same six steps and the same
config.toml step state, run through setup.run with the browser's answers,
never interactively. Whatever the terminal wizard refuses, this refuses."""
from .. import config, paths, setup
from ..errors import PplError

# Test seams: the model probe and the forge check setup.run takes. None means
# setup's own defaults, which call the real claude and the real forge.
PROBE = None
FORGE_CHECK = None

_JSON_TYPES = {type(None): "null", bool: "a boolean", int: "a number", float: "a number",
               list: "a list", dict: "an object"}


def check_step(step):
    if step not in setup.STEPS:
        raise PplError("SCHEMA_INVALID", "unknown setup step %r (steps: %s)"
                       % (step, ", ".join(setup.STEPS)))


def check_answers(answers):
    if not isinstance(answers, dict):
        raise PplError("SCHEMA_INVALID", "answers must be an object of key: value")
    unknown = sorted(key for key in answers if key not in setup.ANSWER_KEYS
                     and not str(key).startswith(setup.TIER_PREFIX))
    if unknown:
        raise PplError("SCHEMA_INVALID", "unknown answer key %s (keys: %s, %s<name>)"
                       % (", ".join(unknown), ", ".join(setup.ANSWER_KEYS), setup.TIER_PREFIX))
    wrong = ["%s (%s)" % (key, _JSON_TYPES.get(type(value), type(value).__name__))
             for key, value in answers.items() if not isinstance(value, str)]
    if wrong:
        raise PplError("SCHEMA_INVALID", "answer values must be strings: %s" % ", ".join(wrong))
    return {str(key): value for key, value in answers.items()}


def _next(steps):
    for name in setup.STEPS:
        if name not in steps["done"] and name not in steps["skipped"]:
            return name
    return None


def state():
    steps = config.steps_state()
    try:
        code_root = str(paths.root())
    except PplError:
        code_root = None
    rows = []
    for name in setup.STEPS:
        state_word = ("done" if name in steps["done"] else
                      "skipped" if name in steps["skipped"] else "todo")
        rows.append({"name": name, "intro": setup.STEP_INTROS[name], "state": state_word})
    return {"steps": rows, "next": _next(steps), "data_root": str(paths.home()),
            "code_root": code_root, "workspace": config.section("claude").get("workspace")}


def run_step(step, answers, emit):
    check_step(step)
    return setup.run(steps=[step], answers=check_answers(answers), non_interactive=True,
                     emit=emit, probe=PROBE, forge_check=FORGE_CHECK)


def skip_step(step):
    """Skipping a step done earlier leaves it done, as `ppl setup --skip` does."""
    check_step(step)
    if step not in config.steps_state()["done"]:
        config.mark_step(step, "skipped")
    return state()
