# Security

maxedcode runs commands and edits files on the user's PC, so security reports are taken seriously.

## Reporting a problem

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository.

Include what an attacker can do, the steps or a prompt / file that triggers it, and the maxedcode version
(`maxedcode --npm-version`, or the date of your install). You will get an answer within a week.

## In scope

- A way to make the agent run a command, write a file or send data **without** the confirmation its mode promises
  (see [Safety](README.md#safety)): bypassing the dangerous-command checks, protected paths or permission rules
- Prompt injection from a file, web page or tool result that leads to such an action
- `/cloud` sending project code or secrets despite its privacy filter
- The app's local server (`maxed app`) or the browser bridge being reachable by other users or websites
- The installers (`install.ps1`, npm, `Maxedcode_setup.exe`) running or downloading something they shouldn't

## Out of scope

- What the agent does in **Auto** mode or with `--yes` when the action is not in the protected list: that mode is
  meant to run without asking
- Bad answers from the model itself
