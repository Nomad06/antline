export function words(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

export const idleField = { raw: null };

export function fieldInput(text) {
  return { raw: text, list: words(text) };
}

export function fieldBlur() {
  return idleField;
}

export function shownText(field, list) {
  return field.raw === null ? list.join(", ") : field.raw;
}
