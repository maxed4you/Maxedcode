# Contributing

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Reporting a bug

Open an issue with the bug template. The most useful things to include:

- the model and server (e.g. "Qwen3 30B IQ3 on llama.cpp, 32k context")
- what you asked and what happened; the session file from `%USERPROFILE%\.maxedcode\sessions\` helps a lot
- for setup problems: `%TEMP%\maxedcode-setup.log`

Remove anything private (paths, keys, code) before you attach it.

## Development

```
git clone https://github.com/maxed4you/Maxedcode
cd Maxedcode\agent
pip install "openai>=2.0" "ddgs>=3.0" "textual>=2.0" "mcp>=2" pillow rich
python local_agent.py              # the CLI from source
python local_agent.py --app        # the app's server
python run_all_tests.py            # all test suites (a few minutes)
python run_all_tests.py loopguard  # only the suites whose name matches
```

`tests/live_eval/` runs real tasks against a model server (`AGENT_EVAL_URL`, default `http://127.0.0.1:8080/v1`).

## Rules for changes

- **Keep the prompt prefix stable.** The system prompt and the tool list are built once per session. Don't rebuild
  them mid-session, and don't add user/system messages between the assistant's steps of a turn: local servers
  lose their prompt cache and get several times slower. Late context goes inside the latest tool result
  (`util.attach_late()`).
- **Behaviour must not depend on English or Russian word lists** when it can use a structural signal instead.
- **Don't slow the agent down.** For changes to the loop, measure model time and number of calls before and after.
- **English** for code, comments, docstrings and UI text (the app has an English / Russian switch; regexes that
  understand user phrases in other languages are fine).
- Match the style around you: short functions, `contextlib.suppress`, CRLF files keep their line endings.
- The `--print --output-format stream-json` format is a contract with the app window
  (`maxedcode/webapp/static/app.js`): change both together.
- Add or update a test in `agent/tests/` and run `python run_all_tests.py` before opening a pull request.

## License

By contributing you agree that your contribution is licensed under the [Apache-2.0](LICENSE) license.
