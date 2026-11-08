---
name: commit
description: Use when the user asks to commit changes (make a git commit, write a commit message). Checks the diff and tests first.
---
# Commit

Extra instructions: $ARGUMENTS

Current state:
- branch: !`git branch --show-current`
- status: !`git status --short`
- recent commits: !`git --no-pager log --oneline -n 5`

Steps:
1. Look at what will be committed: `git --no-pager diff HEAD --stat`, then the diff of files you did not
   change yourself in this session. Always pass `--no-pager`.
2. Do not commit: secrets (.env, keys, tokens), large binaries, build output, logs, personal data. If such
   files are in the status, leave them out and tell the user.
3. If the project has tests and code changed, run the relevant tests. If they fail, stop and report;
   do not commit broken code unless the user says so.
4. Stage explicit paths: `git add -- <file> <file>`. Use `git add -A` only if every changed file belongs
   in the commit.
5. Message: follow the style of the recent commits above (language, prefixes like `fix:`/`feat:`). First line
   up to 72 chars says what and why; add a short body only if the change is not obvious.
   On Windows pass a multi-line message with several `-m` flags: `git commit -m "title" -m "body"`.
6. Show the result: `git --no-pager log --oneline -n 1` and `git status --short`.

Never push, amend, rebase, reset or use --no-verify unless the user explicitly asked. If a hook fails,
fix the cause and make a new commit.
