"""Read encoded clipboard images from the local Linux desktop."""
import asyncio
import os
from pathlib import Path
import shutil

from .documents import normalize_image
from .models import ServiceError, run_sync

IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")
MAX_CLIPBOARD_BYTES = 64 * 1024 * 1024

# Use the distro's GTK introspection bindings without adding them to the client venv.
_GTK_READER = """
import sys
import gi
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

limit = int(sys.argv[1])
display = Gdk.Display.open(None)
if display is None:
    sys.exit(1)

loop = GLib.MainLoop()
status = 2
remaining = limit + 1

def read_chunk(stream, result):
    global remaining, status
    try:
        chunk = stream.read_bytes_finish(result).get_data()
        if not chunk:
            status = 0
            loop.quit()
            return
        sys.stdout.buffer.write(chunk)
        remaining -= len(chunk)
        if remaining:
            stream.read_bytes_async(min(remaining, 65536), GLib.PRIORITY_DEFAULT, None, read_chunk)
        else:
            status = 3
            loop.quit()
    except GLib.Error:
        loop.quit()

def read(clipboard, result):
    try:
        stream, mime = clipboard.read_finish(result)
        if stream is not None:
            stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, read_chunk)
            return
    except GLib.Error:
        pass
    loop.quit()

clipboard = display.get_clipboard()
clipboard.read_async(sys.argv[2:], GLib.PRIORITY_DEFAULT, None, read)
loop.run()
sys.exit(status)
"""


async def clipboard_command(command, *, backend=None):
    env = {**os.environ, "GDK_BACKEND": backend} if backend else None
    try:
        process = await asyncio.create_subprocess_exec(
            *command, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        async with asyncio.timeout(4):
            try:
                await process.stdout.readexactly(MAX_CLIPBOARD_BYTES + 1)
            except asyncio.IncompleteReadError as error:
                if await process.wait() == 0:
                    return error.partial
            else:
                raise ServiceError("source_limit", "Clipboard image exceeds 64 MiB. Copy a smaller image.")
    except TimeoutError:
        return None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
    return None


async def read_clipboard_image():
    wayland = os.environ.get("WAYLAND_DISPLAY")
    x11 = os.environ.get("DISPLAY")
    if not (wayland or x11):
        raise ServiceError("clipboard_unavailable", "The desktop clipboard is unavailable. Use Browse to open an image.")

    if wayland and (wl_paste := shutil.which("wl-paste")):
        mime_list = await clipboard_command((wl_paste, "--list-types"))
        if mime_list:
            offered = mime_list.decode(errors="replace").splitlines()
            for mime in IMAGE_MIME_TYPES:
                if mime in offered:
                    data = await clipboard_command((wl_paste, "--no-newline", "--type", mime))
                    if data:
                        return await run_sync(normalize_image, data)

    if x11 and (xclip := shutil.which("xclip")):
        targets = await clipboard_command((xclip, "-selection", "clipboard", "-out", "-target", "TARGETS"))
        if targets:
            offered = targets.decode(errors="replace").splitlines()
            for mime in IMAGE_MIME_TYPES:
                if mime in offered:
                    data = await clipboard_command((xclip, "-selection", "clipboard", "-out", "-target", mime))
                    if data:
                        return await run_sync(normalize_image, data)

    python = Path("/usr/bin/python3")
    if python.is_file():
        command = (str(python), "-c", _GTK_READER, str(MAX_CLIPBOARD_BYTES), *IMAGE_MIME_TYPES)
        for backend, available in (("x11", x11), ("wayland", wayland)):
            if available and (data := await clipboard_command(command, backend=backend)):
                return await run_sync(normalize_image, data)

    hint = " On Wayland, install wl-clipboard if your desktop blocks clipboard access." if wayland else ""
    raise ServiceError("clipboard_unavailable", "Copy a PNG, JPEG, or WebP image, then try again." + hint)
