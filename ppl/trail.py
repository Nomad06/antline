"""`ppl trail export`: one run's documents as a static, redacted trail.

The run's documents are copied from an allowlist, never by walking the run
directory blindly: `usage.json` holds transcript paths and a build log can
hold anything, so whatever the allowlist does not name is left out and listed
in the manifest's `skipped`. Paths are always redacted, people by default and
URLs on request. Then every written file is checked for what must not leave
the machine (the repo path, the home directory, PPL_HOME, a redacted person, a
token pattern), and a trail that still holds one is refused whole: it is
built in a sibling temporary directory and renamed onto `--out` only once
that check passes [R-TRAIL-REDACTED].
"""
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import __version__, metrics, paths, store, usage
from .errors import PplError

# Relative paths under runs/<task>, in a readable form. `<sha>` is hexadecimal,
# `<check>` a check name, `<n>` a history number and `<id>` a finding id.
ALLOWLIST = (
    "state.json", "log.jsonl", "brief.json", "challenge.json", "boundary.json",
    "baseline.json", "harness.json", "threads.json", "parking.json",
    "tracker/issue.json", "tracker/issue.md",
    "evidence/<sha>/<check>.json",
    "verdicts/<sha>.json", "verdicts/<sha>/<n>.json",
    "findings/<id>.json",
)

_SLOTS = {"<sha>": "[0-9a-fA-F]+", "<check>": "[A-Za-z0-9_-][A-Za-z0-9._-]*",
          "<n>": "[0-9]+", "<id>": "[A-Za-z0-9_-]+"}


def _compile(entry):
    pattern = re.escape(entry)
    for slot, part in _SLOTS.items():
        pattern = pattern.replace(re.escape(slot), part)
    return re.compile(pattern)


_ALLOWED = tuple(_compile(entry) for entry in ALLOWLIST)

# The trail's own list. The secret-scan gate is a harness command, so ppl holds
# no patterns of its own to reuse.
TOKEN_PATTERNS = (
    r"gh[pousr]_[A-Za-z0-9]{36}",
    r"github_pat_[A-Za-z0-9_]{22,}",
    r"glpat-[A-Za-z0-9_-]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"sk-ant-[A-Za-z0-9_-]{20,}",
    r"xox[abpr]-[A-Za-z0-9-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)
_TOKENS = tuple((pattern, re.compile(pattern)) for pattern in TOKEN_PATTERNS)

REDACTIONS = ("paths", "people", "urls")

PERSON_KEYS = ("author", "assignee", "assignees", "reporter", "creator", "by", "login",
               "displayName")
_PERSON_FIELDS = ("login", "name", "displayName")
# The `by` values ppl itself writes for a control command: a program, not a person.
_NOT_PEOPLE = ("cli", "panel")

_URL_RE = re.compile(r"https?://[^\s<>\"'`(){}\[\]]*[^\s<>\"'`(){}\[\].,;:!?]")

RENDERED = ("brief.md", "verdicts.md", "threads.md")


def temp_roots():
    return (tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders")


def find_run(task_id):
    """The run's directory: the live one, else the newest archived one."""
    paths.check_task_id(task_id)
    found = [directory for found_id, directory in metrics._run_records() if found_id == task_id]
    live = paths.run_dir(task_id)
    if live in found:
        return live
    if found:
        return found[-1]
    raise PplError("SCHEMA_INVALID", "no run for %s, live or archived" % task_id)


def _allowed(rel):
    return any(pattern.fullmatch(rel) for pattern in _ALLOWED)


def collect(directory):
    """(docs, skipped): every allowlisted document parsed, keyed by its relative
    path, and the relative path of every other file. state.json loses
    `orchestrator` and a receipt its build `log` path."""
    directory = Path(directory)
    docs = {}
    skipped = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        linked = [d for d in dirs if os.path.islink(os.path.join(root, d))]
        for name in sorted(files) + linked:
            path = Path(root, name)
            rel = path.relative_to(directory).as_posix()
            if path.is_symlink() or not path.is_file() or not _allowed(rel):
                skipped.append(rel)
                continue
            if rel == "log.jsonl":
                docs[rel] = [e for e in store.read_log_at(directory) if isinstance(e, dict)]
            elif rel.endswith(".md"):
                docs[rel] = path.read_text(encoding="utf-8", errors="replace")
            else:
                doc = store.read_json(path)
                if rel == "state.json" and isinstance(doc, dict):
                    doc.pop("orchestrator", None)
                if rel.startswith("evidence/") and isinstance(doc, dict):
                    doc.pop("log", None)
                docs[rel] = doc
    return dict(sorted(docs.items())), sorted(skipped)


def _forms(value):
    """An absolute path as given and resolved, without a trailing slash."""
    if not isinstance(value, (str, os.PathLike)):
        return []
    text = os.fspath(value)
    if not text.startswith("/"):
        return []
    forms = []
    for form in (text, os.path.realpath(text)):
        form = form.rstrip("/")
        if form and form not in forms:
            forms.append(form)
    return forms


def _person_pattern(names):
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(r"@?(?<![\w-])(?P<name>%s)(?![\w-])" % alternatives, re.IGNORECASE)


class Redactor:
    """Paths always, people and URLs when asked. Paths are matched longest first,
    so a repo under the home under a temp root reads `<repo>`. People are found
    by `find_people` before anything is applied and become `@person-<n>` in the
    order of a sorted walk. URLs become `<url-<n>>` in the order they are met."""

    def __init__(self, state, home, ppl_home, temp_roots, people, urls):
        state = state if isinstance(state, dict) else {}
        named = [(state.get("repo"), "<repo>", "the repo path"),
                 (ppl_home, "<ppl-home>", "PPL_HOME"),
                 (home, "<home>", "the home directory")]
        named += [(root, "<tmp>", None) for root in temp_roots]
        self._placeholder = {}
        self._needles = []
        for value, placeholder, reason in named:
            for form in _forms(value):
                if form in self._placeholder:
                    continue
                self._placeholder[form] = placeholder
                if reason:
                    self._needles.append((form, reason))
        forms = "|".join(re.escape(form)
                         for form in sorted(self._placeholder, key=len, reverse=True))
        self._paths_re = re.compile(r"(?<![\w.-])(?:%s)" % forms) if forms else None
        self.people = bool(people)
        self.urls = bool(urls)
        self._aliases = {}
        self._spellings = []
        self._people_re = None
        self._url_aliases = {}
        self._used_paths = set()

    def find_people(self, docs):
        """Collects the people named under PERSON_KEYS in `docs`, in sorted order."""
        if not self.people:
            return
        for rel in sorted(docs):
            self._walk_people(docs[rel])
        if self._spellings:
            self._people_re = _person_pattern(self._spellings)

    def _walk_people(self, value):
        if isinstance(value, dict):
            for key in sorted(value):
                if key in PERSON_KEYS:
                    for name in _names(value[key]):
                        self._add_person(name)
                self._walk_people(value[key])
        elif isinstance(value, list):
            for item in value:
                self._walk_people(item)

    def _add_person(self, name):
        if not name.strip() or name.lower() in _NOT_PEOPLE:
            return
        if name not in self._spellings:
            self._spellings.append(name)
        key = name.casefold()
        if key not in self._aliases:
            self._aliases[key] = "@person-%d" % (len(self._aliases) + 1)

    def _path(self, match):
        placeholder = self._placeholder[match.group(0)]
        self._used_paths.add(placeholder)
        return placeholder

    def _url(self, match):
        url = match.group(0)
        if url not in self._url_aliases:
            self._url_aliases[url] = "<url-%d>" % (len(self._url_aliases) + 1)
        return self._url_aliases[url]

    def _person(self, match):
        found = match.group("name")
        alias = self._aliases.get(found.casefold())
        if alias is None:
            spelling = next(s for s in self._spellings
                            if re.fullmatch(re.escape(s), found, re.IGNORECASE))
            alias = self._aliases[spelling.casefold()]
        return alias

    def text(self, value):
        if self._paths_re is not None:
            value = self._paths_re.sub(self._path, value)
        if self.urls:
            value = _URL_RE.sub(self._url, value)
        if self._people_re is not None:
            value = self._people_re.sub(self._person, value)
        return value

    def apply(self, value):
        """`value` with every string redacted, dict keys included, keys walked sorted."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.apply(item) for item in value]
        if isinstance(value, dict):
            out = {}
            for key in sorted(value):
                out[self.text(key) if isinstance(key, str) else key] = self.apply(value[key])
            return out
        return value

    def applied(self):
        return {"paths": sorted(self._used_paths),
                "people": len(self._aliases) if self.people else False,
                "urls": len(self._url_aliases) if self.urls else False}


def _names(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value[field] for field in _PERSON_FIELDS if isinstance(value.get(field), str)]
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, (str, dict)):
                names.extend(_names(item))
        return names
    return []


def leaks(text, redactor):
    """Why `text` may not be published, empty when it may."""
    reasons = []
    for needle, reason in redactor._needles:
        if needle in text and reason not in reasons:
            reasons.append(reason)
    if redactor._people_re is not None and redactor._people_re.search(text):
        reasons.append("a redacted person")
    for pattern, compiled in _TOKENS:
        if compiled.search(text):
            reasons.append("a token matching %s" % pattern)
    return reasons


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _wall_clock(events):
    start = next((_parse_time(e.get("at")) for e in events if e.get("event") == "init"), None)
    if start is None:
        return None
    done = [e for e in events if e.get("event") == "lifecycle" and e.get("to") == "done"]
    ends = [_parse_time(e.get("at")) for e in (done[:1] or events[-1:])]
    if not ends or ends[0] is None:
        return None
    try:
        return (ends[0] - start).total_seconds()
    except TypeError:
        return None


def measures(directory):
    """The run's numbers for the manifest. A number with no data is null and
    `reasons` says why, never 0."""
    events = [e for e in store.read_log_at(directory) if isinstance(e, dict)]
    reasons = {}
    wall = _wall_clock(events)
    if wall is None:
        reasons["wall_clock_seconds"] = "the log has no init event with a readable time"
    roles = usage.run_usage(directory)
    tokens = {role: metrics._counts_of(models) for role, models in sorted(roles.items())} or None
    if tokens is None:
        reasons["tokens_by_role"] = ("no usage recorded - the usage hook (ppl setup claude) "
                                     "records it")
    try:
        usd, unpriced = usage.run_dollars(directory)
        if unpriced:
            reasons["usd"] = ("no price for model(s) %s - add a [prices.\"<model>\"] table to "
                              "config.toml" % ", ".join(unpriced))
        elif usd is None:
            reasons["usd"] = "no usage recorded"
    except PplError as exc:
        usd = None
        reasons["usd"] = exc.message
    verdicts = Counter(e.get("verdict") for e in events
                       if e.get("event") == "verdict" and isinstance(e.get("verdict"), str))
    return {
        "wall_clock_seconds": wall,
        "tokens_by_role": tokens,
        "usd": usd,
        "denials": sum(1 for e in events if e.get("event") == "deny"),
        "rework_rounds": verdicts.get("REWORK", 0),
        "verdicts": dict(sorted(verdicts.items())),
        "reasons": reasons,
    }


def _check_redact(redact):
    redact = tuple(redact)
    unknown = [name for name in redact if name not in REDACTIONS]
    if unknown:
        raise PplError("SCHEMA_INVALID", "--redact takes %s, got: %s"
                       % (", ".join(REDACTIONS), ", ".join(unknown)))
    if "paths" not in redact:
        raise PplError("SCHEMA_INVALID",
                       "--redact must include paths: path redaction is always on, since the "
                       "leak check refuses a trail holding the repo path or the home directory")
    return redact


def _serialize(rel, value):
    if rel == "log.jsonl":
        return "".join(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
                       for event in value)
    if rel.endswith(".md"):
        return value
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


_PAGE_STYLE = """
:root { --paper: #f6f6f3; --ink: #211915; --muted: #6b5f56; --trail: #b4a89c;
        --amber-ink: #8a5800; color-scheme: light; }
@media (prefers-color-scheme: dark) {
  :root { --paper: #17120e; --ink: #f1ebe4; --muted: #b3a698; --trail: #6e6055;
          --amber-ink: #f3c260; color-scheme: dark; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: var(--paper); color: var(--ink);
       font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 760px; margin: 0 auto; }
h1 { margin: 0 0 16px; padding-bottom: 8px; border-bottom: 3px dotted var(--trail); }
ul { padding-left: 20px; }
li { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 14px;
     overflow-wrap: anywhere; }
a { color: var(--amber-ink); }
"""


def _plain_page(docs):
    """index.html: the exported documents as relative links, nothing else."""
    items = "\n".join('<li><a href="%s">%s</a></li>' % (html.escape(rel, quote=True),
                                                       html.escape(rel))
                      for rel in sorted(docs))
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>antline trail</title>\n<style>%s</style>\n</head>\n<body>\n<main>\n"
            "<h1>antline trail</h1>\n<ul>\n%s\n</ul>\n</main>\n</body>\n</html>\n"
            % (_PAGE_STYLE, items))


def _write(root, rel, text):
    target = Path(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def export(task_id, out, redact=("paths", "people")):
    """Writes the trail to `out` and returns its manifest. Nothing is left behind
    on a refusal [R-TRAIL-REDACTED]."""
    redact = _check_redact(redact)
    directory = find_run(task_id)
    out = Path(out)
    if os.path.lexists(out):
        raise PplError("SCHEMA_INVALID", "%s already exists: a trail is written to a new "
                                         "directory, never over one" % out)
    docs, skipped = collect(directory)
    state = docs.get("state.json")
    state = state if isinstance(state, dict) else {}
    redactor = Redactor(state, home=str(Path.home()), ppl_home=str(paths.home()),
                        temp_roots=temp_roots(), people="people" in redact,
                        urls="urls" in redact)
    redactor.find_people(docs)
    texts = {}
    for rel, value in docs.items():
        texts["docs/" + rel] = _serialize(rel, redactor.apply(value))
    for name in RENDERED:
        texts["docs/" + name] = "# %s\n" % name[:-len(".md")]
    texts["index.html"] = _plain_page(sorted(texts))
    out.parent.mkdir(parents=True, exist_ok=True)
    building = tempfile.mkdtemp(dir=str(out.parent), prefix=".trail-")
    try:
        for rel in sorted(texts):
            _write(building, rel, texts[rel])
        files = [{"path": rel,
                  "sha256": hashlib.sha256(Path(building, rel).read_bytes()).hexdigest()}
                 for rel in sorted(texts)]
        manifest = {
            "task_id": task_id,
            "base_sha": state.get("base_sha"),
            "head_sha": state.get("candidate_sha"),
            "antline_version": __version__,
            "redactions": redactor.applied(),
            "skipped": skipped,
            "measures": measures(directory),
            "files": files,
        }
        _write(building, "MANIFEST.json",
               json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        found = []
        for rel in sorted(texts) + ["MANIFEST.json"]:
            reasons = leaks(Path(building, rel).read_text(encoding="utf-8"), redactor)
            if reasons:
                found.append("%s still holds %s" % (rel, ", ".join(reasons)))
        if found:
            raise PplError("SCHEMA_INVALID",
                           "trail for %s refused, nothing was written: %s. Remove the value "
                           "from the run's documents and export again"
                           % (task_id, "; ".join(found)))
        os.rename(building, out)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return manifest
