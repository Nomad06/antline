"""The trail's readable documents: the timeline page and the three markdown files.

Everything here renders documents `ppl.trail` has already redacted, so a value reaches
the page exactly as it reached the JSON files, and the leak check that runs over every
written file covers these too."""
import html
from collections import Counter

COUNTS = ("input", "cache_write", "cache_read", "output")

THEME = """:root { --paper: #f6f6f3; --surface: #ffffff; --sunk: #f0efea; --ink: #211915;
  --muted: #6b5f56; --line: #e2ded7; --trail: #b4a89c; --hole: #211915; --amber: #e5a019;
  --amber-ink: #8a5800; --on-amber: #211915; --leaf: #3d7a2a; --leaf-soft: #e7f1e0;
  --formica: #b0351c; --formica-soft: #fbebe6; color-scheme: light; }
@media (prefers-color-scheme: dark) {
  :root { --paper: #17120e; --surface: #211a15; --sunk: #1b1511; --ink: #f1ebe4;
    --muted: #b3a698; --line: #3a2f27; --trail: #6e6055; --hole: #0b0806; --amber: #f0b23a;
    --amber-ink: #f3c260; --on-amber: #1d150f; --leaf: #86c46a; --leaf-soft: #1f2e18;
    --formica: #ff7f61; --formica-soft: #3a1c15; color-scheme: dark; }
}"""

STYLE = THEME + """
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: var(--paper); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 820px; margin: 0 auto; }
h1 { margin: 0 0 4px; padding-bottom: 8px; border-bottom: 3px dotted var(--trail); }
h2 { margin: 28px 0 8px; font-size: 17px; }
.muted { color: var(--muted); }
table { border-collapse: collapse; width: 100%; background: var(--surface); }
th, td { text-align: left; padding: 6px 10px; border: 1px solid var(--line); vertical-align: top; }
th { background: var(--sunk); font-weight: 600; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
ol { list-style: none; margin: 0; padding: 0; border-left: 3px dotted var(--trail); }
li { margin: 0 0 6px 12px; padding: 4px 10px; background: var(--surface); overflow-wrap: anywhere;
  border: 1px solid var(--line); border-left: 4px solid var(--trail); }
li.good { border-left-color: var(--leaf); background: var(--leaf-soft); }
li.bad { border-left-color: var(--formica); background: var(--formica-soft); }
li.mark { border-left-color: var(--amber); }
time, code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13px; }
.kind { font-weight: 600; margin: 0 6px; }
a { color: var(--amber-ink); }
"""

_SHOWN = ("stage_enter", "usage", "evidence", "verdict", "brief_challenge", "deny",
          "finding_add", "finding_status", "review_reply", "approval", "mr",
          "merge_observed", "park", "lifecycle")


def _s(value, none="none"):
    """A value as one line of text."""
    if value is None or value == "":
        return none
    if isinstance(value, (dict, list)):
        return ", ".join("%s" % item for item in value) if isinstance(value, list) else str(value)
    return " ".join(str(value).split())


def _short(sha):
    return _s(sha)[:7] if isinstance(sha, str) else _s(sha)


def _total(models):
    total = 0
    for counts in models.values() if isinstance(models, dict) else ():
        for name in COUNTS:
            value = counts.get(name) if isinstance(counts, dict) else None
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
    return total


def timeline(log_events):
    """One entry per shown event, in log order: {at, kind, text, link}."""
    entries = []
    verdicts = Counter()
    for event in log_events:
        if not isinstance(event, dict) or event.get("event") not in _SHOWN:
            continue
        kind = event["event"]
        link = None
        sha = event.get("sha")
        if kind == "stage_enter":
            text = "%s to %s" % (_s(event.get("from")), _s(event.get("to")))
        elif kind == "usage":
            if "error" in event or not isinstance(event.get("models"), dict):
                text = "%s dispatched, usage not recorded (%s)" % (
                    _s(event.get("role")), _s(event.get("error"), "no models"))
            else:
                text = "%s dispatched, %s tokens" % (_s(event.get("role")),
                                                     format(_total(event["models"]), ","))
        elif kind == "evidence":
            text = "%s %s on %s" % (_s(event.get("check")), _s(event.get("result")), _short(sha))
            link = "docs/evidence/%s/%s.json" % (sha, event.get("check"))
        elif kind == "verdict":
            verdicts[sha] += 1
            text = "%s on %s" % (_s(event.get("verdict")), _short(sha))
            link = "docs/verdicts/%s/%d.json" % (sha, verdicts[sha])
        elif kind == "brief_challenge":
            text = "brief challenge %s" % _s(event.get("verdict"))
            link = "docs/challenge.json"
        elif kind == "deny":
            text = "%s was denied %s (%s)" % (_s(event.get("role"), "a caller"),
                                              _s(event.get("tool")), _s(event.get("code")))
        elif kind in ("finding_add", "finding_status"):
            what = ("%s finding" % _s(event.get("severity"))) if kind == "finding_add" \
                else "finding now %s" % _s(event.get("status"))
            text = "%s %s" % (what, _short(event.get("finding_id")))
            link = "docs/findings/%s.json" % event.get("finding_id")
        elif kind == "review_reply":
            text = "thread %s on %s: %s" % (_s(event.get("thread")), _short(sha),
                                            _s(event.get("classification")))
            link = "docs/threads.md"
        elif kind == "approval":
            text = "approved %s" % _short(sha)
        elif kind == "mr":
            text = "opened #%s %s" % (_s(event.get("iid")), _s(event.get("url"), ""))
        elif kind == "merge_observed":
            text = "merged %s %s" % (_short(sha), _s(event.get("url"), ""))
        elif kind == "park":
            text = "parked: %s" % _s(event.get("blocker"))
        else:
            text = "run is %s" % _s(event.get("to"))
        entries.append({"at": event.get("at"), "kind": kind, "text": text.strip(), "link": link})
    return entries


def _e(value):
    return html.escape(_s(value), quote=False)


def _num(value):
    return format(value, ",") if isinstance(value, int) and not isinstance(value, bool) else "?"


def _undefined(measures, key):
    return "undefined: %s" % _s((measures.get("reasons") or {}).get(key), "no data")


def _measure_rows(measures):
    rows = []
    wall = measures.get("wall_clock_seconds")
    rows.append(("wall clock", ("%.1f s" % wall) if isinstance(wall, (int, float))
                 else _undefined(measures, "wall_clock_seconds")))
    usd = measures.get("usd")
    rows.append(("dollars", ("$%.4f" % usd) if isinstance(usd, (int, float))
                 else _undefined(measures, "usd")))
    rows.append(("denials", _num(measures.get("denials"))))
    rows.append(("rework rounds", _num(measures.get("rework_rounds"))))
    outcomes = measures.get("verdicts") or {}
    rows.append(("verdicts", ", ".join("%s %s" % (k, _num(v)) for k, v in outcomes.items())
                 or "none recorded"))
    return rows


def _tokens_table(measures):
    roles = measures.get("tokens_by_role")
    if not isinstance(roles, dict) or not roles:
        return '<p class="muted">tokens: %s</p>' % html.escape(
            _undefined(measures, "tokens_by_role"), quote=False)
    head = "".join("<th>%s</th>" % name.replace("_", " ") for name in COUNTS)
    body = []
    for role, counts in roles.items():
        counts = counts if isinstance(counts, dict) else {}
        cells = "".join('<td class="n">%s</td>' % _num(counts.get(name)) for name in COUNTS)
        body.append('<tr><th>%s</th>%s<td class="n">%s</td></tr>'
                    % (_e(role), cells, _num(sum(counts.get(n) for n in COUNTS
                                                 if isinstance(counts.get(n), int)))))
    return ("<table>\n<tr><th>role</th>%s<th>total</th></tr>\n%s\n</table>"
            % (head, "\n".join(body)))


def _item(entry):
    outcome = entry["text"].split(" ", 1)[0]
    shade = {"PASS": "good", "ACCEPTED": "good", "REWORK": "bad", "BLOCKED": "bad"}.get(outcome)
    if entry["kind"] in ("deny", "park"):
        shade = "bad"
    elif entry["kind"] in ("merge_observed", "approval"):
        shade = "good"
    link = ""
    if entry.get("link"):
        link = ' <a href="%s">%s</a>' % (html.escape(entry["link"], quote=True),
                                         html.escape(entry["link"], quote=False))
    return ('<li class="%s"><time>%s</time><span class="kind">%s</span>%s%s</li>'
            % (shade or "mark", _e(entry.get("at")), _e(entry["kind"].replace("_", " ")),
               _e(entry["text"]), link))


def page(task_id, entries, measures, manifest_head):
    """index.html as ASCII text: measures, then the timeline with relative links."""
    rows = "\n".join("<tr><th>%s</th><td>%s</td></tr>" % (label, _e(value))
                     for label, value in _measure_rows(measures))
    items = "\n".join(_item(entry) for entry in entries) or '<li class="mark">no events</li>'
    head = _short(manifest_head) if manifest_head else "no candidate"
    text = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>antline trail %(task)s</title>\n<style>\n%(style)s</style>\n</head>\n<body>\n"
        '<main>\n<h1>%(task)s</h1>\n<p class="muted">head %(head)s. Every link opens a '
        'document of this trail. <a href="docs/brief.md">brief</a>, '
        '<a href="docs/verdicts.md">verdicts</a>, <a href="docs/threads.md">threads</a>, '
        '<a href="MANIFEST.json">manifest</a></p>\n'
        "<h2>Measures</h2>\n<table>\n%(rows)s\n</table>\n<h2>Tokens</h2>\n%(tokens)s\n"
        "<h2>Timeline</h2>\n<ol>\n%(items)s\n</ol>\n</main>\n</body>\n</html>\n"
        % {"task": _e(task_id), "style": STYLE, "head": _e(head), "rows": rows,
           "tokens": _tokens_table(measures), "items": items})
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _line(value):
    return html.escape(_s(value), quote=False)


def _block(value):
    return html.escape(str(value), quote=False) if value else "none"


def _bullets(values):
    items = values if isinstance(values, list) else []
    return "\n".join("- %s" % _line(v if not isinstance(v, dict) else
                                    v.get("question") or v.get("name") or v.get("shape") or v)
                     for v in items) or "none"


def brief_md(brief, challenge):
    brief = brief if isinstance(brief, dict) else {}
    out = ["# brief", "", "## Purpose", "", _block(brief.get("purpose")), "",
           "## Success outcome", "", _block(brief.get("success_outcome")), "",
           "## Acceptance", "", _bullets(brief.get("acceptance")), "",
           "## Governing requirements", "", _bullets(brief.get("governing_requirements")), "",
           "## May write", "", _bullets(brief.get("allowed_write_paths")), "",
           "## May not touch", "", _bullets(brief.get("forbidden_paths")), "",
           "## Verification", "", _bullets(brief.get("verification")), "",
           "## Open decisions", "", _bullets(brief.get("open_decisions")), ""]
    if isinstance(challenge, dict):
        out += ["## Challenge", "", "Verdict: %s" % _line(challenge.get("verdict")), "",
                "Open decisions:", "", _bullets(challenge.get("open_decisions")), ""]
    else:
        out += ["## Challenge", "", "none recorded", ""]
    return "\n".join(out)


def _findings_line(item):
    if isinstance(item, dict):
        return "%s %s %s" % (_s(item.get("severity")), _s(item.get("status")),
                             _s(item.get("evidence"), ""))
    return _s(item)


def verdicts_md(history):
    """Each verdict with its SHA, outcome, producer and findings, oldest first."""
    out = ["# verdicts", ""]
    for number, doc in enumerate(history, 1):
        doc = doc if isinstance(doc, dict) else {}
        producer = doc.get("producer") if isinstance(doc.get("producer"), dict) else {}
        found = doc.get("findings") if isinstance(doc.get("findings"), list) else []
        out += ["## %d. %s on %s" % (number, _line(doc.get("verdict")),
                                      _line(doc.get("candidate_sha"))), "",
                "- SHA: %s" % _line(doc.get("candidate_sha")),
                "- Outcome: %s" % _line(doc.get("verdict")),
                "- Producer: %s %s" % (_line(producer.get("provider")),
                                       _line(producer.get("model"))),
                "- Findings: %s" % (", ".join(_line(_findings_line(f)) for f in found)
                                    or "none recorded"), ""]
    if not history:
        out += ["none recorded", ""]
    return "\n".join(out)


def threads_md(threads):
    """thread id, SHA, classification, time. threads = {thread: {sha: {...}}}."""
    out = ["# threads", ""]
    rows = []
    for thread, by_sha in sorted((threads if isinstance(threads, dict) else {}).items()):
        for sha, reply in sorted((by_sha if isinstance(by_sha, dict) else {}).items()):
            reply = reply if isinstance(reply, dict) else {}
            rows.append("- %s on %s: %s at %s" % (
                _line(thread), _line(sha), _line(reply.get("classification")),
                _line(reply.get("at"))))
    return "\n".join(out + (rows or ["none recorded"]) + [""])
