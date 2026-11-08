---
name: debug
description: Use when something fails or behaves wrong and the cause is not obvious - an error, traceback, failing test, crash, hang or wrong output. Systematic reproduce, isolate, fix, verify.
---
# Debug

Problem: $ARGUMENTS

Work in this order. Do not jump to a fix before step 3.

1. Reproduce. Find the exact command, test or input that shows the problem and run it. Save the exact error
   text. If it does not reproduce, ask the user for the missing detail instead of guessing.
2. Read the evidence. Read the traceback bottom-up; open the deepest frame that is in the project's own code
   (read_file with offset around the line). Note the actual values vs what was expected.
3. Isolate. Form one hypothesis at a time and test it cheaply: a py_run snippet, a print, a smaller input,
   `git --no-pager log -p -n 5 -- <file>` to see what changed recently. Discard the hypothesis if the test
   says so. Repeat until you can name the root cause in one sentence.
4. Fix the root cause, not the symptom. Smallest change that fixes it; no unrelated cleanups.
   Do not silence the error (bare except, skipping the test) unless that is the actual correct behaviour.
5. Verify. Re-run the exact reproduction from step 1, then the related tests. If it still fails, go back to 3.

Stop and report after 3 failed hypotheses: what you tried, what you ruled out, what you need.

Final report: root cause (one sentence), the fix (`path:line`), how it was verified (command + result).
