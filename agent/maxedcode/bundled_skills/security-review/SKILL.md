---
name: security-review
description: Use when the user asks for a security review or audit of code or changes (injection, secrets, unsafe input handling, permissions).
---
# Security review

Target: $ARGUMENTS (empty = the uncommitted changes; "all" = the whole project, start from entry points).

Changed files: !`git --no-pager diff --stat HEAD`

Trace untrusted input (user input, files, network, environment, command-line args, model output) to where
it is used. Check:
1. Command injection: shell=True, os.system, subprocess with joined strings, PowerShell/cmd strings built
   from input, Invoke-Expression.
2. Code execution: eval/exec, pickle/yaml.load/marshal on untrusted data, dynamic imports from input.
3. Path traversal: file paths joined from input without resolve() and a check that they stay inside the
   allowed folder; zip/tar extraction without member path checks.
4. SQL/NoSQL injection: queries built with f-strings or + instead of parameters.
5. Secrets: API keys, tokens, passwords in code, logs, error messages, config committed to git.
6. Network: verify=False, http instead of https for credentials, SSRF (fetching URLs from input).
7. Web: XSS (unescaped output), missing auth checks, CSRF, open redirects.
8. Unsafe defaults: debug on, wide permissions, temp files with predictable names.

Use grep to find patterns (e.g. `shell=True|eval\(|exec\(|pickle|verify=False|api_key|password`), then read
each hit in context. Report only what is reachable from untrusted input; say how it is reachable.

Report, most severe first: severity (high/medium/low) — `path:line` — the issue — how an attacker reaches
it — the fix. Do not edit files unless asked.
