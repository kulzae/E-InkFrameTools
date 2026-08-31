# E-InkFrameTools
Image server and dithering tool for E-Ink picture frames

This repo contains some simple tools for E-Ink picture frames intended to be used with the firmware from here: https://github.com/aitjcize/esp32-photoframe

E-Ink Image Preprocessor is a browser-based tool (HTML/JS) that prepares images for display on 6-color e-paper devices.

Features:

Dithering with measured colors — Dithering decisions and the live preview use measured palette values (what the e-paper physically displays), which are significantly darker/more muted than theoretical RGB values. This avoids the common problem of images looking washed out or incorrect on actual e-paper hardware.

BMP/PNG export with theoretical values — The exported BMP/PNG writes theoretical palette RGB values so the device's firmware can match every pixel exactly with its color lookup table.

S-curve tone mapping — Applies an adjustable S-curve (Strength, Shadow Boost, Highlight Compress, Midpoint) to remap a photo's full tonal range into the e-paper's narrow displayable range (~90:1 dynamic range), preserving detail in shadows and highlights that would otherwise be crushed.
    

The UI supports drag-and-drop loading of PNG/JPG files, pan/zoom on the preview canvas, and adjustable parameters in a side panel with a one-click reset. Its very similar to the built in processing in the esp32 firmware but allows you to pre-format each image.



Simple Image Server is a lightweight Python HTTP server that feeds random images to e-ink photoframes URL mode. Each frame's image_url names the folder to serve from, and the server picks a random image from that folder on every rotation request, never repeating the last image it sent. Requires Pillow for automatic image rotation for portrait/landscape orientations.

Features:
    
URL-driven routing — http://<server>:8080/<folder> serves from that folder (e.g. /LivingRoom, /Hallway); each frame is configured independently.

Optional per-request rotation — append ?rotate=90cw, ?rotate=90ccw, or ?rotate=180 to a frame's URL to pre-rotate images in memory (via Pillow).

Random selection, no repeats — one image at random per request, never the same file twice in a row per folder.

Specify next image — type commands into the terminal while it runs: next <folder> <filename> to force one specific image (reverts to random afterwards), cancel, folders, help.

