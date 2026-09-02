#!/usr/bin/env python3
"""
Simple random-image server for e-ink photoframes (rotation_mode: "url").

The frame issues an HTTP GET against its configured image_url on every
rotation. The URL tells the server which folder to serve from and whether
to rotate the image before sending:

    http://<server>:8080/<folder>                 random image, no rotation
    http://<server>:8080/<folder>?rotate=90cw     rotated 90 degrees clockwise
    http://<server>:8080/<folder>?rotate=90ccw    rotated 90 degrees counter-clockwise
    http://<server>:8080/<folder>?rotate=180      rotated 180 degrees

Example image_url values to configure on the frames:
    http://192.168.2.10:8080/1600x1200?rotate=90cw   (landscape BMP frame)
    http://192.168.2.10:8080/800x480                 (normal frame)

Images are picked at random from the named folder and never repeated twice
in a row (per folder). Rotation is applied in memory with Pillow; files on
disk are never modified. Without ?rotate, images are served byte-for-byte.

Console commands (typed into the terminal while the server runs):
    next <folder> <filename>   serve this exact file on the folder's next
                               request, then revert to random selection
    cancel <folder>            clear a queued file for a folder
    folders                    list available resolution folders
    help                       show the command list

Pillow is only required for rotated URLs:
    pip install pillow

Usage:
    python frame_image_server.py [--host 0.0.0.0] [--port 8080] [--images-dir ./images]
"""

import argparse
import io
import json
import mimetypes
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, parse_qs

try:
    from PIL import Image
    HAVE_PILLOW = True
except ImportError:
    Image = None
    HAVE_PILLOW = False

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
ALLOWED_ROTATIONS = {"90cw", "cw", "90ccw", "ccw", "180"}

# Pillow's ROTATE_90 is counter-clockwise; ROTATE_270 is clockwise.
ROTATION_METHODS = {
    "90cw": Image.Transpose.ROTATE_270,
    "cw":   Image.Transpose.ROTATE_270,
    "90ccw": Image.Transpose.ROTATE_90,
    "ccw":  Image.Transpose.ROTATE_90,
    "180":  Image.Transpose.ROTATE_180,
} if HAVE_PILLOW else {}


def is_image_file(folder_path, name):
    return (os.path.isfile(os.path.join(folder_path, name))
            and os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS)


class ImageStore:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.lock = threading.Lock()
        self.last_sent = {}    # folder name -> filename last served from that folder
        self.forced_next = {}  # folder name -> filename to serve on the next request

    def available_folders(self):
        try:
            return sorted(d for d in os.listdir(self.root)
                          if os.path.isdir(os.path.join(self.root, d)))
        except FileNotFoundError:
            return []

    def folder_path(self, name):
        """Resolve a folder name from the URL to a safe path under root."""
        if (not name or ".." in name or os.sep in name
                or (os.altsep and os.altsep in name)):
            return None
        path = os.path.join(self.root, name)
        if os.path.commonpath([self.root, path]) != self.root:
            return None
        return path

    def list_images(self, folder):
        folder_path = self.folder_path(folder)
        if folder_path is None:
            return None
        try:
            return sorted(f for f in os.listdir(folder_path) if is_image_file(folder_path, f))
        except FileNotFoundError:
            return None

    def pick(self, folder):
        """Return (path, error). On success error is None.

        If a file was queued via force_next(), it is served exactly once and
        the queue entry is consumed; otherwise a random file is chosen that
        differs from the last one sent from this folder.
        """
        folder_path = self.folder_path(folder)
        if folder_path is None:
            return None, f"invalid folder name '{folder}'"
        with self.lock:
            try:
                files = [f for f in os.listdir(folder_path) if is_image_file(folder_path, f)]
            except FileNotFoundError:
                return None, (
                    f"folder '{folder}' not found under {self.root} "
                    f"(available folders: {', '.join(self.available_folders()) or 'none'})")
            if not files:
                return None, (
                    f"folder '{folder}' has no usable image files directly in it "
                    f"(accepted extensions: {', '.join(sorted(IMAGE_EXTENSIONS))})")

            forced = self.forced_next.get(folder)
            if forced in files:
                choice = forced            # explicit operator pick, served once
                del self.forced_next[folder]
            else:
                # Random pick; never repeat the last file we sent from this folder.
                # (If the folder holds exactly one file, it must be repeated.)
                last = self.last_sent.get(folder)
                candidates = [f for f in files if f != last] or files
                choice = random.choice(candidates)
            self.last_sent[folder] = choice
        return os.path.join(folder_path, choice), None

    def force_next(self, folder, filename):
        """Queue a specific file as the folder's next image. Returns (ok, message)."""
        folder_path = self.folder_path(folder)
        if folder_path is None:
            return False, f"invalid folder name '{folder}'"
        with self.lock:
            try:
                entries = os.listdir(folder_path)
            except FileNotFoundError:
                return False, (f"folder '{folder}' not found "
                               f"(available: {', '.join(self.available_folders()) or 'none'})")
            # Exact match first, then case-insensitive.
            target = None
            if is_image_file(folder_path, filename):
                target = filename
            else:
                matches = [e for e in entries
                           if e.lower() == filename.lower() and is_image_file(folder_path, e)]
                if len(matches) == 1:
                    target = matches[0]
                elif len(matches) > 1:
                    return False, f"ambiguous filename '{filename}' (matches: {', '.join(sorted(matches))})"
            if target is None:
                images = [e for e in entries if is_image_file(folder_path, e)]
                return False, (f"'{filename}' is not an image in '{folder}' "
                               f"(images: {', '.join(sorted(images)) or 'none'})")
            self.forced_next[folder] = target
        return True, f"next image for '{folder}' will be '{target}' (reverts to random afterwards)"

    def cancel_next(self, folder):
        """Clear a queued file. Returns (ok, message)."""
        with self.lock:
            if self.forced_next.pop(folder, None) is None:
                return False, f"no queued image for '{folder}'"
        return True, f"cleared queued image for '{folder}'"


class ConsoleThread(threading.Thread):
    """Reads operator commands from stdin while the server runs."""

    def __init__(self, store):
        super().__init__(daemon=True)
        self.store = store

    def run(self):
        print("console ready -- type 'help' for commands (Ctrl-C stops the server)")
        while True:
            try:
                line = input("frame-image> ").strip()
            except EOFError:
                return  # stdin closed (e.g. running detached); server keeps running
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()
            try:
                if cmd in ("help", "?"):
                    print("commands:")
                    print("  next <folder> <filename>  queue this file as the folder's next image")
                    print("  cancel <folder>           clear a queued file")
                    print("  folders                   list available resolution folders")
                    print("  help                      show this list")
                elif cmd == "folders":
                    folders = self.store.available_folders()
                    print(", ".join(folders) if folders else "(no folders)")
                elif cmd == "next":
                    if len(parts) < 3:
                        print("usage: next <folder> <filename>")
                        continue
                    ok, msg = self.store.force_next(parts[1], " ".join(parts[2:]))
                    print(("queued: " if ok else "error: ") + msg)
                elif cmd == "cancel":
                    if len(parts) != 2:
                        print("usage: cancel <folder>")
                        continue
                    ok, msg = self.store.cancel_next(parts[1])
                    print(("ok: " if ok else "error: ") + msg)
                else:
                    print(f"unknown command '{cmd}' -- type 'help'")
            except Exception as e:  # never let a typo kill the console loop
                print(f"error: {e}")


def make_etag(path, tag=""):
    st = os.stat(path)
    # tag distinguishes rotated variants so a 304 can never serve a stale one.
    return f'"{st.st_mtime_ns}-{st.st_size}{tag}"'


def etag_matches(if_none_match, etag):
    """RFC 7232: does the If-None-Match header match our ETag?"""
    expected = etag.strip().strip('"')
    for token in if_none_match.split(","):
        token = token.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:].strip()
        if token.strip('"') == expected:
            return True
    return False


def rotate_image(data, command):
    """Apply a rotation command to image bytes. Returns new bytes or None."""
    method = ROTATION_METHODS.get(command)
    if method is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            rotated = img.transpose(method)
            buf = io.BytesIO()
            save_kwargs = {"quality": 90} if img.format == "JPEG" else {}
            rotated.save(buf, format=img.format, **save_kwargs)
            return buf.getvalue()
    except Exception as e:
        print(f"warning: image rotation failed: {e}")
        return None


class Handler(BaseHTTPRequestHandler):
    store = None  # set in main()

    # Match the frame's documented per-attempt timeout so a dead or stalled
    # connection can't pin a server thread indefinitely.
    timeout = 120

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError) as e:
            # Client hung up mid-request (its own timeout, a canceled/duplicate
            # fetch, or a reboot). The frame retries transport errors on its own.
            self.log_message("client %s disconnected (%s); frame will retry",
                             self.client_address[0], type(e).__name__)

    def do_GET(self):
        self.log_message(
            "frame request %s (width=%s height=%s orientation=%s)",
            self.path,
            self.headers.get("X-Display-Width"),
            self.headers.get("X-Display-Height"),
            self.headers.get("X-Display-Orientation"))

        folder, rotate_cmd, error = self.parse_target()
        if error:
            return self.send_json(400, error)

        path, pick_error = self.store.pick(folder)
        if path is None:
            self.log_message("404 reason: %s", pick_error)
            return self.send_json(404, pick_error)

        etag = make_etag(path, tag=f"-{rotate_cmd}" if rotate_cmd else "")

        # Only short-circuit with 304 when the frame actually sent an If-None-Match.
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match and etag_matches(if_none_match, etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        with open(path, "rb") as f:
            data = f.read()  # raw bytes unless a rotation is requested
        if rotate_cmd:
            rotated = rotate_image(data, rotate_cmd)
            if rotated is None:
                self.log_message(
                    "warning: could not rotate %s (%s%s); sending unrotated",
                    os.path.basename(path), rotate_cmd,
                    "" if HAVE_PILLOW else ", Pillow not installed")
            else:
                data = rotated
                self.log_message("served %s rotated %s",
                                 os.path.basename(path), rotate_cmd)

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)

    def parse_target(self):
        """Extract (folder, rotate_cmd, error) from the request URL.

        Expected form: /<folder>[?rotate=<90cw|90ccw|180>]
        """
        parsed = urlsplit(self.path)
        segments = [s for s in unquote(parsed.path).split("/") if s]
        if not segments:
            return None, None, (
                "URL must name a folder, e.g. /LivingRoom or /800x480?rotate=90cw")
        folder = segments[-1]

        rotate_cmd = None
        params = parse_qs(parsed.query)
        if "rotate" in params:
            rotate_cmd = (params["rotate"][0] or "").lower()
            if rotate_cmd not in ALLOWED_ROTATIONS:
                return None, None, (
                    f"invalid rotate value '{rotate_cmd}' "
                    f"(allowed: {', '.join(sorted(ALLOWED_ROTATIONS))})")
        return folder, rotate_cmd, None

    def do_POST(self):
        # The frame only ever uses GET.
        self.send_json(405, "Method not allowed")

    def send_json(self, code, message):
        body = json.dumps({"status": "error", "message": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="Random image server for e-ink photoframes")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--images-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "images"))
    args = ap.parse_args()

    store = ImageStore(args.images_dir)
    Handler.store = store
    ConsoleThread(store).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving images from {store.root}")
    print(f"Listening on http://{args.host}:{args.port}")
    print("URL format: /<folder>[?rotate=<90cw|90ccw|180>]")
    if not HAVE_PILLOW:
        print("WARNING: Pillow not installed -- ?rotate URLs will serve unrotated images (pip install pillow)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
