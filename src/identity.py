"""Non-face identity / presence baseline for RealEyes.

Design (see docs/identity.md):
- Presence is estimated from keyboard + mouse idle time (Windows ``LastInputInfo``).
- The active application (foreground window title) provides coarse "what the
  operator is doing" context without looking at the face.
- The machine is single-user: the operator is labelled with ``owner_label``.
  Confidence is a heuristic, not a biometric verification.
- ``BodyReID`` is the hook for the future OSNet / ONNX clothing-and-body
  re-identification backend; it is intentionally not implemented yet.

No image data is used by this module, so it also works while the camera is
covered / black.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    kernel32.GetTickCount64.restype = ctypes.c_uint64
except AttributeError:  # pragma: no cover - very old Windows
    pass


def _tick_count() -> int:
    """GetTickCount64 lives in kernel32 (not user32); fall back to 32-bit tick."""
    try:
        return int(kernel32.GetTickCount64())
    except AttributeError:  # pragma: no cover - very old Windows
        return int(kernel32.GetTickCount()) & 0xFFFFFFFF


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_ms() -> int:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    # dwTime carries the low 32 bits of the tick count; the subtraction wraps
    # correctly with a 32-bit mask for any idle period below 49.7 days.
    return max(0, (int(_tick_count()) - int(info.dwTime)) & 0xFFFFFFFF)


def foreground_title(limit: int = 120) -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(2, min(limit, length + 1)))
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value.strip()


def app_name(title: str) -> str:
    if not title:
        return ""
    for sep in (" - ", " — ", " – "):
        if sep in title:
            return title.rsplit(sep, 1)[-1].strip()[:40]
    return title[:40]


class IdentityTracker:
    def __init__(self, owner_label: str = "owner",
                 at_desk_idle_s: int = 60, away_idle_s: int = 180) -> None:
        self.owner_label = owner_label
        self.at_desk_idle_s = int(at_desk_idle_s)
        self.away_idle_s = int(away_idle_s)

    def snapshot(self) -> dict:
        idle = idle_ms()
        idle_s = idle // 1000
        if idle_s < self.at_desk_idle_s:
            presence, confidence = "at_desk", 0.9
        elif idle_s < self.away_idle_s:
            presence, confidence = "recent", 0.7
        else:
            presence, confidence = "away", 0.85
        title = foreground_title()
        return {
            "label": self.owner_label,
            "presence": presence,
            "confidence": confidence,
            "idle_s": idle_s,
            "foreground": title,
            "app": app_name(title),
            "basis": "input_idle+foreground_window",
        }


class BodyReID:
    """Future hook: OSNet / ONNX body re-identification (non-face)."""

    def __init__(self, model_path: str = "") -> None:
        self.model_path = model_path

    def available(self) -> bool:
        return False

    def embed(self, frame_bgr):  # pragma: no cover - interface only
        raise NotImplementedError("BodyReID backend not installed yet (see docs/identity.md)")
