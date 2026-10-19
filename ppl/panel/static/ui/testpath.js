const FALLBACK = "src/test/FooTest.java";

export function exampleTestPath(sources) {
  const first = Array.isArray(sources) ? sources.find((glob) => typeof glob === "string" && glob) : null;
  if (!first) {
    return FALLBACK;
  }
  const parts = first.split("/");
  while (parts.length > 1 && parts[0] === "**") {
    parts.shift();
  }
  const last = parts.length - 1;
  const named = [];
  parts.forEach((part, at) => {
    if (part === "**") {
      if (at === last) {
        named.push("example");
      }
    } else {
      named.push(part.split("*").join("example"));
    }
  });
  return named.join("/");
}
