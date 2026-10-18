export function words(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

export function shownText(raw, list) {
  return raw === null ? list.join(", ") : raw;
}
