# Raspberry Pi Companion Display Design Notes

Design note only. Not implemented in this branch.

## Goal

Use the Raspberry Pi's attached LCD/display as a companion instruction panel for the Ultimate NFC launcher.

The Pi display can show launch status and help/instructions while the Commodore 64/128 performs the real launch/load/autoboot behavior. This avoids trying to force help text onto the Commodore screen, which is fragile for loaders, autoboot disks, C128 software, and programs that immediately take over the display.

The amusing side effect: the small Pi LCD may have more pixels than the C64/C128 display, so it is better suited for readable public-facing instructions.

## Motivation

Commodore-screen help currently requires brittle behavior:

- reset/wait for BASIC READY
- render text into C64/C128 screen RAM
- wait for user input
- avoid colliding with loaders
- handle autoboot disks that never give BASIC a chance
- avoid help being overwritten by the launched program

A companion display avoids that entire class of problems.

## Basic behavior

On each NFC launch:

1. NFC card is read.
2. Image row is looked up in the database.
3. U2/Ultimate launch is started.
4. Companion display is updated with:
   - title
   - launch mode / target machine
   - help text if available
   - disk swap or control notes if available
   - status such as "loading", "launched", "failed", etc.
5. Help remains visible until:
   - another card is scanned
   - display is explicitly cleared
   - launcher exits/reboots
   - optional timeout, if configured later

This is especially useful for C128 self-booting disks where the Commodore cannot safely show pre-launch help.

## Architecture idea

Create a standalone display renderer app, for example:

```text
pi_display.py
```

It opens a window/fullscreen display and listens locally for updates.

The NFC launcher/monitor sends display updates to it over localhost.

Example local API:

```text
POST http://127.0.0.1:8765/display
POST http://127.0.0.1:8765/clear
GET  http://127.0.0.1:8765/health
```

Example JSON payload:

```json
{
  "title": "Home Designer 128",
  "subtitle": "C128 self-booting disk",
  "body": "Watch the Commodore screen. The disk should boot automatically.\n\nUse joystick or keyboard as prompted.",
  "flash": true,
  "style": "help"
}
```

If the display service is unavailable, the launcher should continue silently.

## Rendering backend options

### pygame

Likely best first implementation.

Pros:

- good fullscreen/windowed support
- straightforward font and color rendering
- works on Raspberry Pi OS
- should work under WSLg/X11 for development
- no browser required

Possible package:

```bash
sudo apt install python3-pygame
```

### tkinter

Simple option, but less pleasant for kiosk-style rendering.

Possible package:

```bash
sudo apt install python3-tk
```

### Browser kiosk

Tiny HTTP server plus Chromium kiosk mode.

Pros:

- HTML/CSS rendering is flexible
- easy layout and styling

Cons:

- heavier
- more moving parts
- less ideal if the Pi should be a minimal appliance

## Display modes

Development machine / WSL:

```bash
./pi_display.py --windowed
```

Raspberry Pi appliance:

```bash
startx ./pi_display.py --fullscreen
```

or via systemd/X startup later.

The Pi does not need a full desktop environment if the display app can be launched directly under X11.

## Help content

Initial implementation can reuse existing script/help text files from `scripts/*.txt`.

Later, help files may gain explicit sections, for example:

```text
[pi-help]
Text intended for the Pi companion display.
[/pi-help]

[c64-help]
Text intended for Commodore screen rendering, if still needed.
[/c64-help]
```

But the first pass can render plain text.

## Attention behavior

When new help appears, the display renderer may briefly flash:

- border
- background
- title bar

This helps show visitors that the Pi screen contains relevant instructions.

## Relationship to Commodore-screen help

The Pi companion display should become the preferred help surface.

Commodore-screen help can remain for special cases, but should not be required for public-facing instructions. This avoids loader timing problems and supports autoboot disks cleanly.
