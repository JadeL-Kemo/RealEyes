"""ADB phone source for RealEyes (MVP interface + screenshot reader).

Enable in config::

    phone:
      enabled: true
      adb_serial: ""     # empty = the single connected device
      fps: 1

Requirements: Android platform-tools ``adb`` (this machine:
``D:\\Program Files\\Android\\SDK\\platform-tools\\adb.exe``), USB debugging
enabled, and the phone must authorize this computer (accept the RSA prompt).

The reader grabs ``adb exec-out screencap -p`` and decodes the PNG with OpenCV.
It is intentionally stateless: each read is a fresh screenshot, so no fragile
long-lived ADB pipe is required for the MVP.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

ADB_CANDIDATES = [
    r"D:\Program Files\Android\SDK\platform-tools\adb.exe",
    r"C:\Program Files\Android\SDK\platform-tools\adb.exe",
]


def adb_path() -> str | None:
    env = os.environ.get("ADB_PATH")
    if env and Path(env).is_file():
        return env
    found = shutil.which("adb")
    if found:
        return found
    for cand in ADB_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def device_state(adb: str | None = None) -> str:
    adb = adb or adb_path()
    if not adb:
        return "no_adb"
    try:
        out = subprocess.run([adb, "get-state"], capture_output=True, text=True,
                             timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return "error"
    state = (out.stdout or "").strip().lower()
    if state == "device":
        return "device"
    # unauthorized / offline are reported on stderr by adb
    err = (out.stderr or "").strip().lower()
    for known in ("unauthorized", "offline", "no devices"):
        if known in err or known in state:
            return known.replace(" ", "_")
    return "unknown"


def screencap(adb: str | None = None, timeout: int = 15) -> bytes | None:
    adb = adb or adb_path()
    if not adb or device_state(adb) != "device":
        return None
    try:
        out = subprocess.run([adb, "exec-out", "screencap", "-p"],
                             capture_output=True, timeout=timeout,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    data = out.stdout or b""
    return data if data.startswith(b"\x89PNG") else None


class PhoneReader:
    """cv2.VideoCapture-like reader returning (ok, frame_bgr)."""

    def __init__(self, serial: str = "") -> None:
        self.serial = serial or None
        self.ok = device_state() == "device"

    def isOpened(self) -> bool:
        return self.ok

    def read(self):
        data = screencap()
        if not data:
            return False, None
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False, None
        return True, img

    def release(self) -> None:
        self.ok = False


def open_reader(cfg: dict):
    phone_cfg = cfg.get("phone") or {}
    reader = PhoneReader(str(phone_cfg.get("adb_serial") or ""))
    return reader if reader.isOpened() else None
