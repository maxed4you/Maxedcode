"""Packs the program (installer/app) into maxedcode.zip so that every system unpacks it the same way.

    python installer/zip_program.py installer/app installer/maxedcode.zip

Used by release.yml instead of Compress-Archive: entry names with forward slashes (some PowerShell versions write
backslashes, which Linux / macOS unpack as one file name), mode bits (755 for *.sh, 644 for the rest) and *.sh with
LF line ends (a CRLF shebang does not run). install.ps1, install.sh and auto-update unpack it as before; the
contents are the same as the Compress-Archive of installer/app/* (the folder's files at the top of the zip).
"""
import sys
import zipfile
from pathlib import Path


def pack(src, out):
    src = Path(src)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(src.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            data = path.read_bytes()
            script = path.suffix == ".sh"
            if script:
                data = data.replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo.from_file(path, path.relative_to(src).as_posix())
            info.external_attr = (0o100000 | (0o755 if script else 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)


if __name__ == "__main__":
    pack(sys.argv[1], sys.argv[2])
