maxedcode - setup (one file: Maxedcode_setup.exe; nothing else is needed)

What gets installed:
  + maxedcode app      its own window, like a chat app: chats, projects, a real terminal and a browser
  + maxedcode CLI      the same agent in any terminal: type maxed (maxed --print "prompt" for scripts)
  + browser extension  the agent in your own Chrome / Edge (turn it on in the app's Settings)

1. Extract Maxedcode_setup.zip.
2. Run Maxedcode_setup.exe. Windows may say "Windows protected your PC" (the file comes from
   the internet and has no paid signature): click "More info" - "Run anyway". If it is blocked
   with no such button (Smart App Control): right-click the file - Properties - tick "Unblock"
   - OK, and run it again.
   The setup works on a fresh Windows 10 / 11 with nothing installed:
     - it finds Python 3.10+, or installs Python 3.12 for this user (winget, or the python.org
       installer when there is no winget; no admin rights needed). The Microsoft Store Python
       is never used: Windows keeps its files in a private copy, so nothing would appear
     - the setup window opens: install folder, model server, model and options, Install
     - the Python packages come from the internet (1-2 minutes)
     - Microsoft Edge WebView2 Runtime (the app window needs it; Windows 11 has it) is
       installed when it is missing
   Python without Tk, or Maxedcode_setup.exe --console: the same questions in the console
   (Up/Down arrows, Enter selects, Space ticks a checkbox, Esc goes back).
   Something went wrong? Every run writes %TEMP%\maxedcode-setup.log - send that file.
3. "Where does the model run?" - setup first looks at this computer and lists what it
   found: Strata, LM Studio, Ollama, llama.cpp, Jan, vLLM ... - running ones with their model
   and context, installed ones that are not running (setup never starts them: their address
   is saved and maxedcode connects once you start the server yourself). Just pressing Next
   everywhere takes the server that runs (Strata first), with:
     - install folder %LOCALAPPDATA%\Programs\maxedcode (chats and settings: C:\Users\YOU\.maxedcode)
     - model and context read from the server (Strata / llama.cpp: at every start, so
       switching the model there needs no new setup; LM Studio / Ollama: the loaded model)
     - Python packages, the "maxedcode app" (window) and "maxedcode CLI" (terminal)
       shortcuts, and program\bin in PATH (so maxed works from any folder)
   The model server does NOT have to run during setup: its address is saved and you
   start it later, whenever you like.
4. Prompt cache: the agent keeps the prompt byte-identical between requests, so every server
   reuses it. The server must hold the whole window, otherwise it cuts the prompt and the
   cache breaks; setup checks this and says what to change:
     Ollama:    4096 tokens and unloading after 5 minutes by default - setup offers to set
                OLLAMA_CONTEXT_LENGTH and OLLAMA_KEEP_ALIVE for you (then restart Ollama)
     LM Studio: load the model with a large enough context (lms load <model> --context-length 32768)
     llama.cpp: -c is split between -np slots: -c 65536 -np 2 gives each slot 32768
5. Start. The install folder has the two launchers, README.txt and program\ (everything else):

     maxedcode app.bat   the app: its own window with chats, projects, a terminal and a browser
     maxedcode CLI.bat   the CLI: the same agent in a terminal window

   The "maxedcode app" and "maxedcode CLI" shortcuts (desktop and Start menu) run the same.
   Deleted a shortcut? Type maxed shortcuts (or double-click program\bin\shortcuts.bat).
   The app needs nothing extra: Microsoft Edge WebView2 (built into Windows 11). Its window
   (maxedcode.exe) is compiled and signed on this PC during setup; if Windows still blocks
   it, the same window starts through Windows PowerShell instead.
   Each chat without a project works in its own folder under C:\Users\YOU\.maxedcode\chats;
   a project is any folder you pick above the input.
   README.txt in the install folder repeats all of this, and says where the browser extension is.

From any folder (open a NEW CMD or PowerShell window after setup):
  maxed                    the CLI, working in the folder you are in
  maxed app                the app window
  maxed --print "prompt"   one answer without the UI (for scripts)
  maxed configure          change the model server / model
  maxed shortcuts          make the desktop / Start shortcuts again
Without PATH: <install folder>\program\bin\maxed.bat app. Skip PATH: Maxedcode_setup.exe --no-path.

Other servers (choose them in the "Model server" list):
  - LM Studio / Ollama / llama.cpp on this computer - found by itself
  - another computer on the network - type its IP: 192.168.0.10 or 192.168.0.10:1234
  - scan the local network for model servers
  - any URL - http://host:port/v1 or https://...
  - Cloud API - OpenAI, Anthropic (Claude), Google Gemini, DeepSeek, Groq, Mistral, xAI,
    OpenRouter, Together AI, Cerebras (an API key from the provider is needed)
  The port and /v1 are found automatically; the list of models and the context
  size are read from the server.

Change the server / model later: maxed configure (or program\bin\configure.bat in the install folder)
(or run the setup again - it keeps sessions and settings).

The model runs on another computer? On THAT computer the server must listen on the network:
  Strata:    Serving - allow other devices (port 8080)
  LM Studio: Developer - Settings - "Serve on Local Network" (port 1234)
  Ollama:    set the environment variable OLLAMA_HOST=0.0.0.0, restart Ollama (port 11434)
  llama.cpp: llama-server --host 0.0.0.0 --port 8080
and Windows Firewall there must allow incoming connections on that port.

Settings are in program\config.json in the install folder:
  base_url, api_key, model (no model = the one the server has loaded), ctx
  (fallback context size when the server does not report one, default 32768)
Environment variables AGENT_BASE_URL / AGENT_MODEL / AGENT_CTX override config.json.

Silent install (Strata defaults, no questions):
  Maxedcode_setup.exe --yes
Silent install with another server:
  Maxedcode_setup.exe --yes --dir D:\Maxedcode --base-url 192.168.0.10:1234 --model qwen3 --ctx 32768
Skip PATH or the shortcut: Maxedcode_setup.exe --yes --no-path --no-shortcut
