"""Encoding-safe, immediately flushed progress messages for Windows consoles."""
import sys


def report(message: str, *, error: bool = False):
    stream = sys.stderr if error else sys.stdout
    try:
        print(message, file=stream, flush=True)
    except UnicodeEncodeError:
        # GitHub Actions Windows can use cp1252 even when the user's PC uses
        # UTF-8. Preserve the percent and timestamps; escape unsupported glyphs.
        encoding = getattr(stream, "encoding", None) or "ascii"
        safe = message.encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe, file=stream, flush=True)
