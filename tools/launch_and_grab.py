import sys, time, os, subprocess
sys.path.insert(0, 'src')
from tod_papers import io_win
import win32gui, cv2

EXE = r"C:\Program Files (x86)\Steam\steamapps\common\PapersPlease\PapersPlease.exe"
ARGS = ["-screen-fullscreen", "0", "-screen-width", "1710", "-screen-height", "960", "-popupwindow"]

def unity_windows():
    out = []
    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == "UnityWndClass":
            out.append(h)
    win32gui.EnumWindows(cb, None)
    return out

if not unity_windows():
    subprocess.Popen([EXE] + ARGS)
    for _ in range(40):
        time.sleep(1)
        if unity_windows():
            break
    time.sleep(6)
hs = unity_windows()
print("unity windows:", [(h, win32gui.GetWindowText(h)) for h in hs])
h = hs[0]
print("client rect phys:", io_win.client_rect_physical(h))
g = io_win.Grabber(h)
f = g.grab()
print("frame", f.shape)
name = sys.argv[1] if len(sys.argv) > 1 else "launch_00"
cv2.imwrite(f"captures/{name}.png", f)
print("saved", name)
