# IO layer: screen capture + mouse input for Papers, Please

Module: `src/tod_papers/io_win.py`  ·  venv: `.venv-io`  ·  deps: `requirements-io.txt`

This layer finds the game window, captures its **client area** in physical
pixels, and drives the mouse (move / click / drag) in **client-relative
physical pixel** coordinates. Everything was built and empirically verified on
this machine (Windows 11 10.0.26200, Python 3.13.7, display at **250% scaling**,
two monitors).

## TL;DR results

- **DPI round-trip verified EXACTLY.** Commanding a drag from client (604,639)
  to (1037,814) produced a stroke whose detected endpoints matched to **0–1 px**
  in the captured image. Capture dimensions equal the client rect exactly (no
  title bar, no offset).
- **dxcam capture latency ≈ 6–9 ms per fresh frame** (region = client rect).
  A static/unchanged frame returns in ~2.6 ms (cached). **mss fallback ≈ 76 ms.**
- **PrintWindow** (PW_RENDERFULLCONTENT) works for normal windows; keep it only
  as an occluded-window diagnostic — it can return black for GPU swapchains.

## DPI: the one thing that must be right

The display is at 250% scaling (`AppliedDPI` 240). A DPI-**unaware** process sees
a virtual/logical coordinate space (the primary monitor reports 1382×864) while
the real framebuffer is ~3456×2160. If capture, `GetWindowRect`, and `SendInput`
disagree about which space they're in, every click lands in the wrong place and
the capture is shifted/cropped.

Fix: declare the process **Per-Monitor-v2 DPI aware before any user32/win32
call**. `io_win` does this at import:

```python
user32.SetProcessDpiAwarenessContext(-4)   # PER_MONITOR_AWARE_V2 (Win10 1703+)
# fallback: shcore.SetProcessDpiAwareness(2) # per-monitor (Win8.1+)
```

Verified: with awareness on, `GetSystemMetrics(SM_CXVIRTUALSCREEN..)` reports the
**physical** virtual desktop `5376×3240`, `GetWindowRect`/`ClientToScreen` return
device pixels, dxcam captures device pixels, and `SendInput` absolute coords map
over the physical virtual desktop. All three agree → pixel-exact round-trip.

Important: awareness is process-wide and set at **import time**. Import
`io_win` before anything else that touches the Win32 GUI. (PowerShell's
`System.Windows.Forms.Screen` is DPI-unaware, which is why it reports 1382×864 —
do not use those numbers as physical pixels.)

## Coordinate model

All public input/geometry functions use **client-relative physical pixels**:
origin = top-left of the window's client area, unit = real device pixel — the
exact same space the `Grabber` returns. So a feature found at pixel `(cx, cy)` in
a captured frame is clicked with `click(hwnd, cx, cy)` directly. No scaling math
in caller code.

`client_rect_physical(hwnd) -> (x, y, w, h)`: `(x, y)` is the client origin in
screen device pixels (via `ClientToScreen((0,0))`), `(w, h)` is the client size
(via `GetClientRect`). Example measured for a Paint window: window rect
1760×1250, client origin (66,122), client size 1728×1162 — i.e. the capture
correctly excludes the 16 px borders and ~60 px title bar.

## Capture (`Grabber`)

- dxcam (Desktop Duplication API) is the primary backend; **mss** is the
  automatic fallback. Both return a contiguous **BGR uint8** `(h, w, 3)` array of
  the client area.
- The dxcam camera is created once on the monitor containing the window
  (`EnumDisplayMonitors` picks the output) and reused; `grab()` captures the
  client sub-region.
- dxcam's Desktop Duplication **deduplicates**: `grab()` returns `None` when the
  frame is unchanged. `Grabber` returns the last good frame in that case (cheap).
  For a live game the frame changes constantly, so this is the fast path.
- Measured, region = 1728×1162 client:
  - dxcam fresh frame: **~6–9 ms**
  - dxcam unchanged (cached): **~2.6 ms**
  - mss: **~76 ms**

## Input

Raw `ctypes` `SendInput` with `MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`
is used for exact pixel positioning. The target screen pixel is normalized to the
0..65535 range over the **virtual** desktop:

```
ax = (sx - SM_XVIRTUALSCREEN) * 65535 / (SM_CXVIRTUALSCREEN - 1)
```

This is more reliable than pydirectinput/pyautogui for absolute placement at high
DPI, because those move in the logical cursor space. (pydirectinput/pyautogui are
installed as fallbacks and for scancode keyboard input Unity accepts, but the
mouse path here is raw SendInput.) `click()` re-asserts the absolute position on
the button-down event so the press lands exactly where intended.

### Drags must be interpolated (Unity)

`drag(hwnd, ax, ay, bx, by, duration=0.25, steps=16)`:
mouse-down at A → **16 interpolated moves** (smoothstep eased) over ~250 ms →
mouse-up at B.

Why a single jump fails in Unity: Unity's `Input` samples cursor position and
button state **once per frame** in `Update`. If you press at A, teleport to B,
and release within one or two frames, Unity sees the button go down and up at
roughly the same place and registers a **click, not a drag** — it never observes
the cursor travelling with the button held, so drag-begin/drag-update callbacks
(and any velocity/throw calc) don't fire. Spreading the motion over many
`SendInput` moves with ~15 ms gaps gives Unity several frames of held-button
motion, which it reliably interprets as a drag. ~10–20 steps over ~150–300 ms is
the sweet spot: enough frames at 60 fps, not so slow it feels laggy. Papers,
Please is drag-heavy (dragging documents/passports around the desk), so this is
the critical path — and it's the one measured exact to 0–1 px.

## Focus

`focus(hwnd)` calls `SetForegroundWindow`, and if Windows' foreground lock
blocks it, uses the `AttachThreadInput` + stray-ALT-keypress trick to steal
foreground. Needed because an unfocused window won't receive game input.

## Gotchas for the Unity game

1. **Set DPI awareness before importing win32 / creating any window.** `io_win`
   does it at import; import it first.
2. **Interpolate drags** (above) or Unity treats them as clicks.
3. **dxcam region must be on the right monitor.** The game may open on the
   secondary 1280×720 display; `Grabber` resolves the output from the window's
   screen position, but confirm with a saved PNG the first time.
4. **Fullscreen exclusive can break Desktop Duplication / block SendInput
   overlay.** Run the game **windowed** (as planned). Borderless windowed is
   fine; true exclusive fullscreen is risky for both capture and input.
5. **dxcam dedup returns None on static frames** — don't treat `None` as an
   error; `Grabber` already reuses the last frame.
6. **PrintWindow may be black** for the Unity GPU swapchain; use `Grabber`
   (Desktop Duplication) for the live game, not `print_window`. PrintWindow is
   only for grabbing an occluded/background normal window.
7. **Coordinates are physical pixels.** If the window is resized, re-read
   `client_rect_physical`; template/pixel coordinates from a different client
   size won't match.
8. Colors are **BGR** (OpenCV convention), not RGB.

## Verified test procedure

Throwaway test harnesses launch MS Paint, find its window, capture the client
area with dxcam and mss (PNGs saved and visually confirmed — client area only,
no chrome), then command drags/clicks and locate the resulting marks in a
re-capture. Diagonal drag err **0.0 px**, horizontal drag err **1.0 px**, single
click landed on its exact target pixel (centroid noise in the full-frame diff
came from Paint's own animated UI — Copilot icon, selection handles, status-bar
coordinate readout — not from input error). Paint is force-closed afterward.
