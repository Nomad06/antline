# test sources are never audited by checkstyle, so audit them here: any byte outside
# printable ASCII (a tab, a CR, a non-ASCII character) in a test file this branch touched.
# The rc comes from the log being empty, like the secret scan below - a while loop's own
# exit code is that of its LAST iteration, which read the wrong way round here before.
git -C <worktree> diff --name-only origin/<default-branch>...HEAD | grep -E 'src/test/.*\.java$' \
  | while read -r f; do LC_ALL=C grep -n '[^ -~]' <worktree>/"$f" && echo "non-ascii, tab or CRLF in $f"; done \
  > <scratch>/<TASK-ID>/testlint.log 2>&1; test ! -s <scratch>/<TASK-ID>/testlint.log; echo $? > <scratch>/<TASK-ID>/testlint.rc
ppl evidence add --check test-lint --result <pass|fail from testlint.rc> --sha <SHA> \
  --log <scratch>/<TASK-ID>/testlint.log --rc <scratch>/<TASK-ID>/testlint.rc --generation <GENERATION> \
  --from orchestrator
