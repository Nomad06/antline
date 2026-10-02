const WRITE_HEADERS = { "Content-Type": "application/json", "X-PPL": "1" };

export class ApiError extends Error {
  constructor(status, payload) {
    super((payload && payload.message) || ("request failed with status " + status));
    this.status = status;
    this.code = (payload && payload.error_code) || "UNKNOWN";
  }
}

async function request(method, path, body) {
  const options = { method, credentials: "same-origin", headers: method === "GET" ? {} : WRITE_HEADERS };
  if (body !== undefined) {
    options.body = JSON.stringify(body);
  }
  const res = await fetch(path, options);
  let payload = null;
  try {
    payload = await res.json();
  } catch (e) {
    payload = null;
  }
  if (res.status === 403 && payload && payload.error_code === "SESSION_REQUIRED") {
    window.dispatchEvent(new CustomEvent("ppl-session-lost"));
  }
  if (!res.ok) {
    throw new ApiError(res.status, payload);
  }
  return payload;
}

export const get = (path) => request("GET", path);
export const post = (path, body) => request("POST", path, body || {});
export const put = (path, body) => request("PUT", path, body || {});

export async function runJob(start, onLog) {
  const { job } = await start;
  let seen = 0;
  for (;;) {
    const state = await get("/api/jobs/" + encodeURIComponent(job));
    const lines = state.log || [];
    if (onLog && lines.length > seen) {
      onLog(lines.slice(seen));
      seen = lines.length;
    }
    if (state.state === "done") {
      return state.result;
    }
    if (state.state === "failed") {
      throw new ApiError(400, state.error);
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}
