function copy(value) {
  return JSON.parse(JSON.stringify(value));
}

export function swapTracker(doc, parked, offers, kind) {
  const current = doc.tracker.kind;
  if (kind === current) {
    return { doc, parked };
  }
  const target = (parked && parked[kind]) || (offers && offers[kind]);
  if (!target) {
    return null;
  }
  const saved = Object.assign({}, parked, { [current]: { tracker: copy(doc.tracker), task: copy(doc.task) } });
  const next = copy(doc);
  next.tracker = copy(target.tracker);
  next.task = copy(target.task);
  return { doc: next, parked: saved };
}

export function retargetGithub(doc, offer, repo, prefix) {
  const next = copy(doc);
  next.tracker.task_prefix = prefix;
  if (repo) {
    next.tracker.repo = repo;
  } else {
    delete next.tracker.repo;
  }
  const drafted = offer.tracker.repo || "<repo>";
  next.task.key_pattern = "^" + prefix + "-[0-9]+$";
  next.task.url_template = offer.task.url_template.split(drafted).join(repo || "<repo>");
  return next;
}
