"""A real terminal for the window's side panel: a shell on a Windows pseudo console (ConPTY, the API Windows
Terminal uses), stdlib ctypes only. The server owns it: leaving the chat or reloading the page keeps it, the page
reattaches and gets the output again from its buffer."""
import ctypes
import shutil
import threading
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
STILL_ACTIVE = 259
KEEP = 2_000_000            # characters of output kept for a reattaching page


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


def _sig(name, res, *args):
    f = getattr(kernel32, name)
    f.restype, f.argtypes = res, list(args)
    return f


_CreatePipe = _sig("CreatePipe", wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                   ctypes.c_void_p, wintypes.DWORD)
_CreatePseudoConsole = _sig("CreatePseudoConsole", ctypes.c_long, COORD, wintypes.HANDLE, wintypes.HANDLE,
                            wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
_ResizePseudoConsole = _sig("ResizePseudoConsole", ctypes.c_long, ctypes.c_void_p, COORD)
_ClosePseudoConsole = _sig("ClosePseudoConsole", None, ctypes.c_void_p)
_InitAttrs = _sig("InitializeProcThreadAttributeList", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD,
                  wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t))
_UpdateAttr = _sig("UpdateProcThreadAttribute", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                   ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
_DeleteAttrs = _sig("DeleteProcThreadAttributeList", None, ctypes.c_void_p)
_CreateProcess = _sig("CreateProcessW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                      ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                      ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION))
_ReadFile = _sig("ReadFile", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
_WriteFile = _sig("WriteFile", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
_CloseHandle = _sig("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_GetExitCode = _sig("GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_Terminate = _sig("TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_Wait = _sig("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)


def shell():
    """PowerShell 7 when installed, else Windows PowerShell."""
    return shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"


class Pty:
    """One shell on a pseudo console. Output accumulates in `text` (offset `base` is where it starts after
    trimming); readers wait on `changed`."""

    def __init__(self, cwd, cols=100, rows=30, command=None):
        self.cwd, self.cols, self.rows = cwd, max(2, cols), max(2, rows)
        self.text, self.base, self.exit = "", 0, None
        self.changed = threading.Condition()
        in_read, in_write, out_read, out_write = (wintypes.HANDLE() for _ in range(4))
        if not _CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0) or \
                not _CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0):
            raise OSError(ctypes.get_last_error(), "CreatePipe failed")
        self.hpc = ctypes.c_void_p()
        hr = _CreatePseudoConsole(COORD(self.cols, self.rows), in_read, out_write, 0, ctypes.byref(self.hpc))
        _CloseHandle(in_read)              # the pseudo console holds its own copies
        _CloseHandle(out_write)
        if hr != 0:
            raise OSError(hr, "CreatePseudoConsole failed")
        self.input, self.output = in_write, out_read
        size = ctypes.c_size_t()
        _InitAttrs(None, 1, 0, ctypes.byref(size))
        self.attrs = ctypes.create_string_buffer(size.value)
        info = STARTUPINFOEXW()
        info.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # STARTF_USESTDHANDLES with no handles: the shell uses the pseudo console, not the server's own
        # (redirected) stdout/stdin
        info.StartupInfo.dwFlags = 0x100
        info.lpAttributeList = ctypes.cast(self.attrs, ctypes.c_void_p)
        if not _InitAttrs(info.lpAttributeList, 1, 0, ctypes.byref(size)) or \
                not _UpdateAttr(info.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self.hpc,
                                ctypes.sizeof(ctypes.c_void_p), None, None):
            raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute failed")
        line = command or f'"{shell()}" -NoLogo'
        self.proc = PROCESS_INFORMATION()
        if not _CreateProcess(None, ctypes.create_unicode_buffer(line), None, None, False,
                              EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT, None, cwd,
                              ctypes.byref(info), ctypes.byref(self.proc)):
            raise OSError(ctypes.get_last_error(), "CreateProcess failed")
        _CloseHandle(self.proc.hThread)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        """The shell ended (exit): readers learn the code; the pseudo console stays until close()."""
        _Wait(self.proc.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        _GetExitCode(self.proc.hProcess, ctypes.byref(code))
        with self.changed:
            if self.exit is None:
                self.exit = code.value
            self.changed.notify_all()

    def _read(self):
        """Drains the pseudo console (it stalls when its output is not read) into `text`."""
        buf = ctypes.create_string_buffer(65536)
        got = wintypes.DWORD()
        pending = b""
        while _ReadFile(self.output, buf, len(buf), ctypes.byref(got), None) and got.value:
            data = pending + buf.raw[:got.value]
            # a UTF-8 character split between two reads waits for its tail
            cut = len(data)
            for back in range(1, min(4, len(data)) + 1):
                b = data[-back]
                if b & 0xC0 == 0xC0:           # a lead byte: complete only if enough bytes follow it
                    need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
                    if back < need:
                        cut = len(data) - back
                    break
                if b < 0x80:
                    break
            pending = data[cut:]
            self._add(data[:cut].decode("utf-8", "replace"))

    def _add(self, text):
        with self.changed:
            self.text += text
            if len(self.text) > KEEP:
                drop = len(self.text) - KEEP // 2
                self.text, self.base = self.text[drop:], self.base + drop
            self.changed.notify_all()

    def since(self, offset, timeout=25):
        """(new offset, output after `offset`, exit code or None); waits up to `timeout` s for something new."""
        with self.changed:
            if self.base + len(self.text) <= offset and self.exit is None:
                self.changed.wait(timeout)
            start = max(0, offset - self.base)
            return self.base + len(self.text), self.text[start:], self.exit

    def write(self, data):
        raw = data.encode("utf-8")
        done = wintypes.DWORD()
        _WriteFile(self.input, raw, len(raw), ctypes.byref(done), None)

    def resize(self, cols, rows):
        self.cols, self.rows = max(2, int(cols)), max(2, int(rows))
        _ResizePseudoConsole(self.hpc, COORD(self.cols, self.rows))

    def alive(self):
        code = wintypes.DWORD()
        return bool(_GetExitCode(self.proc.hProcess, ctypes.byref(code))) and code.value == STILL_ACTIVE

    def close(self):
        """Ends the shell and everything it started (closing the pseudo console ends its processes)."""
        if self.hpc:
            _ClosePseudoConsole(self.hpc)
            self.hpc = ctypes.c_void_p()
        if self.alive():
            _Terminate(self.proc.hProcess, 1)
        for h in (self.input, self.proc.hProcess):
            _CloseHandle(h)
        _DeleteAttrs(ctypes.cast(self.attrs, ctypes.c_void_p))
        with self.changed:
            if self.exit is None:
                self.exit = 0
            self.changed.notify_all()

