A piped Maven command returns the exit code of the pipe tail. Every gate below writes `$?`
to a file immediately, and `ppl evidence add` reads that file. Build with `-pl :<module> -am`
so sibling modules resolve from the reactor, not from a stale local repository. Failsafe
reports land in the root `target/test-reports`, not under the module.
