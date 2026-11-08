---
name: code-review
description: Use when the user asks to review, check or look over code changes (uncommitted diff, a commit, a branch or a file) before commit or merge. Finds bugs, not style nits.
---
# Code review

Target: $ARGUMENTS (empty = the uncommitted changes).

Current state:
- status: !`git status --short`
- changed: !`git --no-pager diff --stat HEAD`

Steps:
1. Get the diff with powershell: `git --no-pager diff HEAD -- <file>` file by file (a commit: `git --no-pager show <sha>`,
   a branch: `git --no-pager diff main...<branch>`). Always pass `--no-pager`. No git: review the named files.
2. For each changed hunk read enough surrounding code with read_file (offset/limit, not whole big files) to
   understand callers and data flow. Use grep to find other call sites of a changed function.
3. Look for real defects only:
   - wrong logic, off-by-one, inverted conditions, wrong variable;
   - unhandled None/empty/missing key, exceptions swallowed or not caught where needed;
   - callers broken by a changed signature, return type or behaviour;
   - resource leaks, race conditions, encoding (non-UTF-8 I/O), Windows paths;
   - removed checks, debug leftovers, secrets in code.
4. Verify each finding against the code before reporting it. Drop anything you are not sure of.

Do not edit files during the review unless the user asked for fixes.

Report, most severe first, each as: `path:line` — what is wrong — concrete input that breaks it — suggested fix.
If nothing real was found, say so in one line. No praise, no style comments.
