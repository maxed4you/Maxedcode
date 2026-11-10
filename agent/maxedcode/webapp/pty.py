"""A real terminal for the window's side panel, owned by the server: leaving the chat or reloading the page keeps
it, the page reattaches and gets the output again from its buffer. Windows: a pseudo console (ConPTY, conpty.py);
macOS / Linux: a pseudo terminal (posixpty.py). Both give Pty(cwd, cols, rows) with since/write/resize/alive/close."""
import os

if os.name == "nt":
    from .conpty import Pty, shell
else:
    from .posixpty import Pty, shell

__all__ = ["Pty", "shell"]
