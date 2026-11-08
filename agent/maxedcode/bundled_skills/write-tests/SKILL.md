---
name: write-tests
description: Use when the user asks to write, add or extend tests for some code (unit tests, regression test for a bug, coverage of a new feature).
---
# Write tests

What to test: $ARGUMENTS

1. Find how the project tests: existing test folders (`tests/`, `test_*.py`, `*_test.py`, `*.spec.ts`),
   the runner (pytest/unittest config, run_all_tests.py, package.json scripts, Makefile). Read one existing
   test file near the target and copy its style: framework, fixtures, naming, imports, helpers.
   No tests yet: Python - unittest or pytest (whichever is installed), one `tests/test_<module>.py`.
2. Read the code under test. List the cases before writing:
   - the normal path with typical input;
   - edges: empty, None, zero, one item, very large, unicode/Cyrillic, Windows paths with spaces;
   - error paths: invalid input must raise or return the documented error;
   - for a bug: a test that fails on the old code and passes on the fixed code.
3. Write focused tests: one behaviour per test, clear names (`test_<what>_<when>`), exact assertions on
   values, not just "no exception". Mock only external things (network, model API, clock, subprocess);
   use temp dirs for files. No sleeps, no dependence on test order or on the user's machine state.
4. Run only the new tests, then the whole related suite. A new test that fails exposes either a test
   mistake (fix the test) or a real bug (report it, do not change the code under test unless asked).

Report: the test file(s), the cases covered, the run command and its result.
