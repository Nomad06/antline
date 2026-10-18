"""Command sequences the skill states in more than one place, written once as data.

`<!-- include: sequence/<name> -->` renders one into the skill skeleton or a forge fragment,
so stage 7 and the review loop cannot list different steps. tests/test_skill_contract.py
walks every sequence through the real ppl from the stage and status it starts in."""

CLOSE_OUT = ("ppl review approved --sha <SHA>", "ppl approval check",
             "ppl stage enter closure", "ppl transition done", "ppl stage enter merge_watch")
AFTER_MERGE = ("ppl merge observed --sha <merged sha> --url <url> --task <TASK-ID>",)

# name -> ((stage, lifecycle status) of a run when the sequence starts, its steps)
SEQUENCES = {
    "close-out": (("review", "in_progress"), CLOSE_OUT),
    # what is left of the close-out for a run already in closure
    "close-out-from-closure": (("closure", "in_progress"),
                               CLOSE_OUT[CLOSE_OUT.index("ppl stage enter closure") + 1:]),
    "after-merge": (("merge_watch", "done"), AFTER_MERGE),
}


def render(steps):
    return ", ".join("`%s`" % step for step in steps)
