---
name: simplify
description: Use when the user asks to simplify, clean up or refactor recently changed code without changing what it does (duplication, dead code, needless complexity).
---
# Simplify

Target: $ARGUMENTS (empty = the uncommitted changes).

Changed files: !`git --no-pager diff --stat HEAD`

Rules:
- Behaviour must stay exactly the same: same inputs give the same outputs, errors and side effects.
- Touch only the target code. Do not reformat or rename things the change did not touch.
- Match the surrounding style: naming, comment density, idioms.

Look for:
1. Duplicated code that an existing helper already does (grep the project for it before writing a new one).
2. Dead code: unused variables, imports, parameters, unreachable branches, commented-out code.
3. Needless complexity: nested ifs that can be early returns, flags that can be a direct return,
   manual loops that are one comprehension or one library call, wrappers that only pass through.
4. Repeated work: the same file read, regex compiled or value computed several times.

Steps: read the diff (`git --no-pager diff HEAD -- <file>`), make small edits with edit_file, then run the
project's tests or at least a syntax check of each changed file. If a test fails, revert that edit.

Report: one line per change (`path:line` — what was simplified), and anything you left alone on purpose.
