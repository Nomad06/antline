import { useState } from "../vendor/htm-preact.mjs";
import { post, runJob } from "../api.js";

export function useStepRunner(step, onDone) {
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState([]);
  const [error, setError] = useState(null);
  const run = async (answers) => {
    setBusy(true);
    setError(null);
    setLog([]);
    try {
      const result = await runJob(post("/api/setup/" + step, { answers: answers || {} }),
        (lines) => setLog((old) => old.concat(lines)));
      if (result.failed && result.failed.length) {
        setError(result.reasons[step] || "the step did not complete");
      } else if (onDone) {
        await onDone(result);
      }
    } catch (e) {
      setError(e.message);
    }
    setBusy(false);
  };
  return { busy, log, error, run };
}
