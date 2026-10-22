# Stage 3 - tests

`ppl stage enter tests`, dispatch `ppl-test-worker` with the acceptance criteria, the
described behaviour, the corner cases and the brief's interfaces only. Do not pass it the diff
or describe how the code works. The guard hook denies its own Read, Grep, Glob and Bash reads
of main sources inside the repo [R-TEST-WORKER-READ-TOOLS] [R-TEST-WORKER-BASH-READS], and it
denies your dispatch when the prompt carries patch syntax or two or more lines the
implementation added [R-TEST-WORKER-DISPATCH-LEAK]. A denied dispatch names what to remove:
remove it and dispatch again, never reword the code to get past the check
[R-TEST-WORKER-NO-DIFF n/a]. A paraphrase is not detected, so that part is on you
**regardless of whether the hook is armed** [R-TEST-WORKER-NO-DIFF n/a]. All of these denials
are, like the write boundary of the implementation half (`references/stage-3-implementation.md`),
only live when `ppl doctor` reported the hook registered [R-GUARD-NOT-ARMED n/a]. Do not let
one agent do both halves.
