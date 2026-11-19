#!/usr/bin/env python
"""
RealEyes MVP — 本机 24h 视觉观察服务（本地优先、零 API token）

数据流:
  屏幕/摄像头采集 -> L0 反射（运动/黑屏/冻结） -> 事件候选
    -> L1 本地 VLM（8322 网关上的 qwen3.5-9b 看关键帧） -> JSON 判定
    -> 事件落盘（JSONL + SQLite + 关键帧） -> 桌面通知（仅 warning+）
    -> 每 10 分钟心跳 + 每小时/每日 digest（本地模型生成，记忆层）

运行:
  python realeyes.py                 # 常驻
  python realeyes.py --check         # 抓一帧屏幕+摄像头，跑一次本地视觉，打印结果
  python realeyes.py --once          # 同上但写入事件库
  python realeyes.py --duration 90   # 跑 90 秒后自动退出（测试）
"""
from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import yaml

# RealEyes project modules (live next to this script; the deployed copy keeps layout)
import filewatch
import identity
import memory
import phone

try:
    import mss
except Exception:  # pragma: no cover
    mss = None

BASE_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("realeyes")
CREATE_NO_WINDOW = 0x08000000


def open_mss():
    """mss >= 10 推荐 MSS 类；旧版回退 mss.mss。"""
    if mss is None:
        raise RuntimeError("mss not installed")
    cls = getattr(mss, "MSS", None)
    return cls() if cls is not None else mss.mss()


# ---------------------------------------------------------------- capture sources

LIVE_HEADERS = "Referer: https://live.bilibili.com/\r\nUser-Agent: Mozilla/5.0\r\n"


def resolve_bilibili_live(room: str) -> str:
    """Resolve a bilibili live room id to a direct FLV URL (anonymous web quality)."""
    api = f"https://api.live.bilibili.com/room/v1/Room/playUrl?cid={room}&platform=web&qn=250"
    req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://live.bilibili.com/"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    durl = (data.get("data") or {}).get("durl") or []
    if not durl:
        raise RuntimeError(f"bilibili room {room}: no play url (code={data.get('code')})")
    return durl[0]["url"]


def find_window_rect(title_substr: str) -> tuple[int, int, int, int] | None:
    """Find a visible top-level window whose title contains title_substr; return (left, top, w, h)."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: dict = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_substr.lower() in buf.value.lower():
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found["rect"] = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            found["title"] = buf.value
            return False
        return True

    user32.EnumWindows(cb, 0)
    LOG.info("window lookup %r -> %s", title_substr, found.get("title", "not found"))
    return found.get("rect")


class ScreenRegionReader:
    """Fixed screen region as a camera-like source (mss grab)."""

    def __init__(self, region: list[int]):
        self.left, self.top, self.width, self.height = (int(v) for v in region[:4])
        self._mss = open_mss()
        self._closed = False

    def isOpened(self) -> bool:
        return not self._closed

    def read(self):
        shot = self._mss.grab({"left": self.left, "top": self.top,
                               "width": self.width, "height": self.height})
        frame = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)[:, :, :3].copy()
        return True, frame

    def get(self, prop):
        return 0.0

    def set(self, prop, val):
        return True

    def release(self):
        self._closed = True
        try:
            self._mss.close()
        except Exception:
            pass


class WindowCaptureReader(ScreenRegionReader):
    """Captures a window's rectangle, re-locating it by title every few seconds.

    window_crop: offsets inside the window (left/top/width/height); defaults to whole window.
    """

    def __init__(self, title_substr: str, crop: dict | None = None, refresh_s: float = 2.0):
        self.title_substr = title_substr
        self.crop = crop or {}
        self.refresh_s = float(refresh_s)
        self._last_lookup = 0.0
        rect = find_window_rect(title_substr)
        if rect is None:
            raise RuntimeError(f"window not found: {title_substr!r}")
        self._set_rect(rect)
        self._mss = open_mss()
        self._closed = False

    def _set_rect(self, rect: tuple[int, int, int, int]) -> None:
        wl, wt, ww, wh = rect
        c = self.crop
        self.left = int(wl + int(c.get("left", 0)))
        self.top = int(wt + int(c.get("top", 0)))
        self.width = max(16, int(c.get("width", ww)))
        self.height = max(16, int(c.get("height", wh)))
        LOG.info("window capture region: %dx%d at (%d,%d)", self.width, self.height, self.left, self.top)

    def read(self):
        now = time.time()
        if now - self._last_lookup >= self.refresh_s:
            self._last_lookup = now
            rect = find_window_rect(self.title_substr)
            if rect is not None:
                self._set_rect(rect)
        return super().read()


class FfmpegPipe:
    """Reads frames from a live/network URL through an ffmpeg rawvideo pipe.

    cv2.VideoCapture cannot open bilibili FLV/HLS URLs (needs Referer/UA headers and
    reconnects), so the media path is owned by ffmpeg and delivered as raw BGR frames.
    """

    def __init__(self, url: str, width: int, height: int, headers: str = ""):
        self.url, self.width, self.height = url, int(width), int(height)
        self.headers = headers
        self.proc = None
        self.buf_size = self.width * self.height * 3
        self._start()

    def _start(self) -> None:
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-fflags", "nobuffer", "-flags", "low_delay",
                "-probesize", "1000000", "-analyzeduration", "1000000",
                "-timeout", "10000000"]
        if self.headers:
            args += ["-headers", self.headers]
        args += ["-i", self.url, "-an", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-vf", f"scale={self.width}:{self.height}", "-"]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, bufsize=self.buf_size,
                                     creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        LOG.info("ffmpeg pipe started: %s", self.url[:100])

    def isOpened(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def read(self):
        try:
            data = self.proc.stdout.read(self.buf_size)
        except Exception:
            return False, None
        if not data or len(data) < self.buf_size:
            return False, None
        frame = np.frombuffer(data, dtype=np.uint8).reshape(self.height, self.width, 3).copy()
        return True, frame

    def get(self, prop):
        return 0.0

    def set(self, prop, val):
        return True

    def release(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None


class MediaFileReader:
    """Loops a local video file (test / replay source)."""

    def __init__(self, path: str):
        self.path = path
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open video file: {path}")

    def isOpened(self) -> bool:
        return self.cap.isOpened()

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        return ok, frame

    def get(self, prop):
        return self.cap.get(prop)

    def set(self, prop, val):
        return self.cap.set(prop, val)

    def release(self) -> None:
        self.cap.release()


# ---------------------------------------------------------------- helpers

def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def compact(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def stamp_short() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def load_config(path: str | None) -> dict:
    p = Path(path) if path else BASE_DIR / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_config_path"] = str(p)
    return cfg


def setup_logging(cfg: dict) -> None:
    level = getattr(logging, str(cfg.get("service", {}).get("log_level", "INFO")).upper(), logging.INFO)
    root = logging.getLogger("realeyes")
    root.setLevel(level)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        log_dir = Path(cfg["service"]["data_root"]) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "realeyes.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        root.exception("file logging disabled")


def ensure_dirs(root: Path) -> None:
    for sub in ("logs", "events", "digests", "state"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def encode_jpeg(frame: np.ndarray, max_width: int = 1600, quality: int = 80) -> bytes:
    img = frame
    if img.shape[1] > max_width:
        scale = max_width / float(img.shape[1])
        img = cv2.resize(img, (max_width, int(img.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes()


def b64_data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def small_gray(frame: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (160, 90), interpolation=cv2.INTER_AREA)


def save_keyframe(root: Path, event_id: str, idx: int, jpeg: bytes) -> str:
    day = now_local().strftime("%Y%m%d")
    d = root / "events" / day / event_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"kf_{idx:02d}.jpg"
    f.write_bytes(jpeg)
    return str(f)


def severity_rank(sev: str) -> int:
    return {"info": 0, "warning": 1, "critical": 2}.get(str(sev).lower(), 0)


class LastInput:
    """Windows GetLastInputInfo -> idle milliseconds."""

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    def __init__(self) -> None:
        try:
            self._user32 = ctypes.windll.user32
            self._kernel32 = ctypes.windll.kernel32
            self._ok = True
        except Exception:
            self._ok = False

    def idle_ms(self) -> int:
        if not self._ok:
            return -1
        try:
            info = self._LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(self._LASTINPUTINFO)
            if not self._user32.GetLastInputInfo(ctypes.byref(info)):
                return -1
            tick = self._kernel32.GetTickCount()
            return int(tick - info.dwTime) & 0xFFFFFFFF
        except Exception:
            return -1


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"gateway HTTP {status}: {message[:400]}")
        self.status = status


class Gateway:
    def __init__(self, cfg: dict):
        g = cfg.get("gateway", {})
        self.base_url = str(g.get("base_url", "http://127.0.0.1:8322/v1")).rstrip("/")
        self.timeout_s = int(g.get("timeout_s", 240))
        self.api_key_env = str(g.get("api_key_env", "QWEN_LOCAL_API_KEY"))
        self.model_attention = str(g.get("model_attention", "qwen3.5-9b"))
        self.model_digest = str(g.get("model_digest", self.model_attention))
        self.last_ok_ts: float = 0.0

    def _key(self) -> str:
        return os.environ.get(self.api_key_env) or "local"

    def chat(self, messages: list, model: str, max_tokens: int = 480, temperature: float = 0.1) -> str:
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {self._key()}"},
            method="POST",
        )
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.last_ok_ts = time.time()
                return payload["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                last_err = GatewayError(e.code, text)
                if e.code in (503, 502) and attempt < 2:
                    time.sleep(4)
                    continue
                raise last_err
            except Exception as e:  # network / timeout
                last_err = e
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise
        raise last_err  # pragma: no cover

    def vision(self, jpeg: bytes, prompt: str, model: str | None = None, max_tokens: int | None = None) -> str:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": b64_data_url(jpeg)}},
        ]
        return self.chat([{"role": "user", "content": content}], model or self.model_attention, max_tokens or 480)

    def text(self, prompt: str, model: str | None = None, max_tokens: int = 500) -> str:
        return self.chat([{"role": "user", "content": prompt}], model or self.model_digest, max_tokens=max_tokens)


# ---------------------------------------------------------------- storage

class EventStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.jsonl = root / "events" / "events.jsonl"
        self.db_path = root / "state" / "realeyes.db"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS events(
                 id TEXT PRIMARY KEY, ts TEXT, source TEXT, kind TEXT,
                 severity TEXT, summary TEXT, notified INTEGER, json TEXT)"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS digests(
                 id TEXT PRIMARY KEY, period TEXT, start_ts TEXT, end_ts TEXT,
                 path TEXT, summary TEXT)"""
        )
        self.conn.commit()

    def add(self, ev: dict, keyframes: list[str], notified: bool) -> None:
        with self.lock:
            line = json.dumps(ev, ensure_ascii=False)
            self.jsonl.parent.mkdir(parents=True, exist_ok=True)
            with open(self.jsonl, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self.conn.execute(
                "INSERT OR REPLACE INTO events(id,ts,source,kind,severity,summary,notified,json) VALUES(?,?,?,?,?,?,?,?)",
                (ev["id"], ev["ts"], ev.get("source", ""), ev.get("kind", ""), ev.get("severity", "info"),
                 ev.get("summary", ""), 1 if notified else 0, line),
            )
            self.conn.commit()

    def query(self, start_ts: str, end_ts: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT json FROM events WHERE ts >= ? AND ts < ? ORDER BY ts", (start_ts, end_ts)
            ).fetchall()
        out = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw))
            except Exception:
                pass
        return out

    def add_digest(self, rec: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO digests(id,period,start_ts,end_ts,path,summary) VALUES(?,?,?,?,?,?)",
                (rec["id"], rec["period"], rec["start_ts"], rec["end_ts"], rec["path"], rec["summary"]),
            )
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class Notifier:
    def __init__(self, cfg: dict):
        n = cfg.get("notify", {})
        self.enabled = bool(n.get("enabled", True))
        self.min_sev = severity_rank(n.get("min_severity", "warning"))
        self.script = BASE_DIR / "notify.ps1"
        self.lock = threading.Lock()
        self.last_notify: dict[str, float] = {}

    def maybe_notify(self, ev: dict, cooldown_s: int = 120) -> bool:
        if not self.enabled or severity_rank(ev.get("severity", "info")) < self.min_sev:
            return False
        key = ev.get("kind", "event")
        now = time.time()
        with self.lock:
            if now - self.last_notify.get(key, 0) < cooldown_s:
                return False
            self.last_notify[key] = now
        if not self.script.exists():
            LOG.warning("notify.ps1 missing, skip notification")
            return False
        title = "RealEyes 提醒"
        msg = f"[{ev.get('source', '')}] {ev.get('summary', '')}".strip()
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script),
                 "-Title", title, "-Message", msg[:500]],
                creationflags=CREATE_NO_WINDOW, timeout=25, capture_output=True,
            )
            return True
        except Exception:
            LOG.exception("notification failed")
            return False


# ---------------------------------------------------------------- service

class RealEyes:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        root = Path(cfg["service"]["data_root"])
        ensure_dirs(root)
        self.root = root
        self.stop = threading.Event()
        self.store = EventStore(root)
        self.gateway = Gateway(cfg)
        self.notifier = Notifier(cfg)
        self.idle = LastInput()
        self.started_at = now_local()

        self.latest_camera: np.ndarray | None = None
        self.latest_camera_ts = 0.0
        self.camera_ok = False
        self.latest_screen: np.ndarray | None = None
        self.latest_screen_ts = 0.0
        self.screen_ok = False
        self.screen_blackout = False

        self.tasks: queue.Queue = queue.Queue(maxsize=int(cfg["attention"].get("queue_size", 8)))
        self.call_times: list[float] = []
        self.rate_lock = threading.Lock()
        self.counters = {"motion_events": 0, "attention_calls": 0, "heartbeats": 0,
                         "notifications": 0, "errors": 0, "blackouts": 0, "freezes": 0}
        self.last_event_ts = ""

        # attention parallelism: several workers can consume the queue concurrently
        # (the gateway llama-server is started with --parallel N matching this).
        self.attention_workers = max(1, int(cfg.get("attention", {}).get("workers", 1)))
        pool = [str(m) for m in (cfg.get("attention", {}).get("model_pool") or []) if str(m).strip()]
        self.model_pool = pool or [self.gateway.model_attention]
        self._pool_lock = threading.Lock()
        self._pool_next = 0

        # audio (loopback capture of what the machine is playing; "ears" MVP)
        self.audio_ok = False
        self.audio_dbfs: float | None = None
        self.audio_ts = 0.0

        # identity baseline (non-face): presence from input idle + foreground app
        self.identity = identity.IdentityTracker(
            owner_label=str((cfg.get("identity") or {}).get("owner_label", "owner")),
            away_idle_s=int((cfg.get("identity") or {}).get("away_idle_s", 180)),
        )
        snap = self.identity.snapshot()
        self.identity_presence = snap["presence"]
        self.identity_foreground = snap["foreground"]
        self.identity_last_ts = time.time()

        # file-change watcher (MVP "file monitoring" source)
        fw_cfg = cfg.get("filewatch") or {}
        self.filewatch = None
        if fw_cfg.get("enabled", False):
            self.filewatch = filewatch.FileWatch(
                [str(p) for p in (fw_cfg.get("dirs") or [])],
                [str(p) for p in (fw_cfg.get("patterns") or ["*"])],
                int(fw_cfg.get("max_files", 5000)),
            )
        self.filewatch_last_scan = 0.0
        self.events_jsonl = self.root / "events" / "events.jsonl"

        self.threads: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------
    def start(self) -> None:
        LOG.info("RealEyes starting; data_root=%s", self.root)
        self._spawn("camera", self._camera_loop)
        self._spawn("screen", self._screen_loop)
        self._spawn("heartbeat", self._heartbeat_loop)
        for i in range(self.attention_workers):
            self._spawn(f"attention-{i + 1}", self._attention_loop)
        self._spawn("audio", self._audio_loop)
        self._spawn("identity", self._identity_loop)
        self._spawn("filewatch", self._file_loop)
        self._spawn("digest", self._digest_loop)
        self._spawn("retention", self._retention_loop)
        self._spawn("status", self._status_loop)

    def _spawn(self, name: str, fn) -> None:
        def runner():
            try:
                fn()
            except Exception:
                LOG.error("thread %s crashed\n%s", name, traceback.format_exc())

        t = threading.Thread(target=runner, name=name, daemon=True)
        t.start()
        self.threads.append(t)

    def shutdown(self) -> None:
        LOG.info("RealEyes stopping...")
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)
        try:
            self.store.close()
        except Exception:
            pass
        LOG.info("RealEyes stopped")

    # -- rate limit / enqueue --------------------------------------
    def _allow_call(self) -> bool:
        limit = int(self.cfg["attention"].get("max_calls_per_hour", 120))
        now = time.time()
        with self.rate_lock:
            self.call_times = [t for t in self.call_times if now - t < 3600]
            if len(self.call_times) >= limit:
                return False
            self.call_times.append(now)
            return True

    def _enqueue(self, task: dict) -> bool:
        try:
            self.tasks.put_nowait(task)
            return True
        except queue.Full:
            LOG.warning("attention queue full; drop task %s/%s", task.get("source"), task.get("kind"))
            return False

    def _save_event(self, ev: dict, jpegs: list[bytes], notified: bool) -> dict:
        kf_paths = []
        for i, j in enumerate(jpegs):
            try:
                kf_paths.append(save_keyframe(self.root, ev["id"], i, j))
            except Exception:
                LOG.exception("keyframe save failed")
        ev["keyframes"] = kf_paths
        self.store.add(ev, kf_paths, notified)
        self.last_event_ts = ev["ts"]
        return ev

    def _make_event(self, source: str, kind: str, severity: str, summary: str,
                    signals: dict | None = None, vlm: dict | None = None) -> dict:
        dt = now_local()
        return {
            "id": f"evt_{compact(dt)}_{uuid.uuid4().hex[:6]}",
            "ts": iso(dt),
            "source": source,
            "kind": kind,
            "severity": severity,
            "summary": summary,
            "signals": signals or {},
            "vlm": vlm or {},
            "budget": {"cloud_used_today": 0, "cloud_cap": int(self.cfg.get("cloud", {}).get("cap_per_day", 100))},
        }

    # -- camera ------------------------------------------------------
    def _open_camera(self):
        c = self.cfg["camera"]
        src = c.get("source", c.get("index", 0))
        w = int(c.get("width", 1280))
        h = int(c.get("height", 720))
        if isinstance(src, str):
            s = src.strip()
            low = s.lower()
            if low in ("phone", "adb"):
                reader = phone.open_reader(self.cfg)
                if reader is None:
                    LOG.warning("phone ADB source unavailable (state=%s)", phone.device_state())
                return reader
            if low.startswith("window:"):
                name = s.split(":", 1)[1].strip()
                try:
                    return WindowCaptureReader(name, c.get("window_crop") or {})
                except Exception as e:
                    LOG.warning("window capture open failed: %s", e)
                    return None
            if low.startswith("screen:"):
                try:
                    region = [int(v) for v in s.split(":", 1)[1].split(",")]
                    return ScreenRegionReader(region)
                except Exception as e:
                    LOG.warning("screen region source invalid (%s): %s", s, e)
                    return None
            if low.startswith("bilibili:"):
                try:
                    url = resolve_bilibili_live(s.split(":", 1)[1].strip())
                except Exception as e:
                    LOG.warning("bilibili live resolve failed: %s", e)
                    return None
                return FfmpegPipe(url, w, h, headers=LIVE_HEADERS)
            if re.match(r"^(https?|rtsp|rtmp)://", s, re.I):
                return FfmpegPipe(s, w, h)
            if os.path.isfile(s):
                try:
                    return MediaFileReader(s)
                except Exception as e:
                    LOG.warning("video file open failed: %s", e)
                    return None
            if s.isdigit():
                src = int(s)
            else:
                LOG.warning("unknown camera source: %s", s)
                return None
        cap = cv2.VideoCapture(int(src), cv2.CAP_DSHOW)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, int(c.get("fps", 5)))
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _camera_loop(self) -> None:
        c = self.cfg["camera"]
        if not c.get("enabled", True):
            LOG.info("camera disabled")
            return
        cap = None
        prev = None
        over = 0
        last_trigger = 0.0
        black_since = None
        black_reported = False
        fps = max(1, int(c.get("fps", 5)))
        period = 1.0 / fps
        while not self.stop.is_set():
            t0 = time.time()
            if cap is None:
                cap = self._open_camera()
                if cap is None:
                    self.camera_ok = False
                    LOG.warning("camera open failed; retry in 10s")
                    self.stop.wait(10)
                    continue
                self.camera_ok = True
                LOG.info("camera opened")
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                cap = None
                self.camera_ok = False
                LOG.warning("camera read failed; reopening")
                self.stop.wait(1)
                continue
            self.latest_camera = frame
            self.latest_camera_ts = time.time()
            g = small_gray(frame)
            luma = float(g.mean())
            black = luma < float(c.get("blackout_luma", 8))
            if black:
                if black_since is None:
                    black_since = time.time()
                elif not black_reported and time.time() - black_since >= float(c.get("blackout_dwell_s", 300)):
                    black_reported = True
                    if self._allow_call():
                        dwell = int(time.time() - black_since)
                        LOG.warning("camera covered (luma=%.1f)", luma)
                        self._enqueue({
                            "source": "camera", "kind": "camera_covered", "severity": "warning",
                            "summary": f"摄像头连续 {dwell} 秒全黑（luma {luma:.1f}），镜头可能被遮挡或被盖住",
                            "signal": {"luma": round(luma, 1), "dwell_s": dwell},
                            "no_vlm": True, "jpegs": [encode_jpeg(frame)], "notify": True,
                        })
            else:
                black_since = None
                black_reported = False
            if prev is not None:
                diff = cv2.absdiff(prev, g)
                ratio = float(np.count_nonzero(diff > 18)) / float(diff.size)
                if ratio > float(c.get("motion_threshold", 0.015)):
                    over += 1
                else:
                    over = 0
                if over >= int(c.get("motion_min_frames", 3)):
                    over = 0
                    now = time.time()
                    idle_ms = self.idle.idle_ms()
                    away = idle_ms < 0 or idle_ms > int(c.get("away_idle_s", 180)) * 1000
                    big = ratio >= float(c.get("big_motion", 0.08))
                    if (away or big) and now - last_trigger >= float(c.get("cooldown_s", 60)):
                        if self._allow_call():
                            last_trigger = now
                            self.counters["motion_events"] += 1
                            jpeg = encode_jpeg(frame)
                            self._enqueue({
                                "source": "camera", "kind": "motion",
                                "severity": "warning" if away else "info",
                                "signal": {"motion_ratio": round(ratio, 4), "away": away, "idle_s": (idle_ms // 1000 if idle_ms >= 0 else -1)},
                                "jpegs": [jpeg], "notify": True,
                            })
            prev = g
            dt = time.time() - t0
            if dt < period:
                self.stop.wait(period - dt)
        if cap is not None:
            cap.release()

    # -- screen ------------------------------------------------------
    def _screen_loop(self) -> None:
        c = self.cfg["screen"]
        if not c.get("enabled", True) or mss is None:
            LOG.info("screen disabled (or mss missing)")
            return
        fps = max(0.1, float(c.get("fps", 0.5)))
        period = 1.0 / fps
        black_since = None
        black_reported = False
        last_small = None
        last_change = time.time()
        frozen_reported = False
        with open_mss() as sct:
            monitor = sct.monitors[1]
            while not self.stop.is_set():
                t0 = time.time()
                try:
                    shot = sct.grab(monitor)
                    frame = np.asarray(shot)[:, :, :3].copy()
                    self.latest_screen = frame
                    self.latest_screen_ts = time.time()
                    self.screen_ok = True
                except Exception:
                    self.screen_ok = False
                    LOG.exception("screen grab failed")
                    self.stop.wait(3)
                    continue
                g = small_gray(frame)
                luma = float(g.mean())
                black = luma < float(c.get("blackout_luma", 12))
                if black:
                    if black_since is None:
                        black_since = time.time()
                    elif not black_reported and time.time() - black_since >= float(c.get("blackout_dwell_s", 45)):
                        black_reported = True
                        self.screen_blackout = True
                        self.counters["blackouts"] += 1
                        if self._allow_call():
                            jpeg = encode_jpeg(frame)
                            self._enqueue({
                                "source": "screen", "kind": "screen_blackout",
                                "severity": "warning",
                                "signal": {"luma": round(luma, 1), "dwell_s": int(time.time() - black_since)},
                                "jpegs": [jpeg], "notify": True,
                            })
                else:
                    if black_reported:
                        black_reported = False
                        self.screen_blackout = False
                        ev = self._make_event("screen", "screen_recovered", "info", "屏幕恢复亮度")
                        self._save_event(ev, [], False)
                    black_since = None
                if last_small is not None:
                    diff = cv2.absdiff(last_small, g)
                    changed = float(np.count_nonzero(diff > 8)) / float(diff.size)
                    if changed > 0.002:
                        last_change = time.time()
                        frozen_reported = False
                if (not black) and (time.time() - last_change >= float(c.get("freeze_dwell_s", 300))) and not frozen_reported:
                    frozen_reported = True
                    self.counters["freezes"] += 1
                    if self._allow_call():
                        jpeg = encode_jpeg(frame)
                        self._enqueue({
                            "source": "screen", "kind": "screen_frozen",
                            "severity": "warning",
                            "signal": {"frozen_s": int(time.time() - last_change)},
                            "jpegs": [jpeg], "notify": True,
                        })
                last_small = g
                dt = time.time() - t0
                if dt < period:
                    self.stop.wait(period - dt)

    # -- heartbeat ---------------------------------------------------
    def _heartbeat_loop(self) -> None:
        h = self.cfg["heartbeat"]
        if not h.get("enabled", True):
            return
        interval = float(h.get("interval_s", 600))
        next_run = time.time() + min(30.0, interval)
        while not self.stop.is_set():
            if time.time() >= next_run:
                next_run = time.time() + interval
                now = time.time()
                if h.get("camera", True) and self.latest_camera is not None and now - self.latest_camera_ts < 60:
                    self.counters["heartbeats"] += 1
                    self._enqueue({
                        "source": "camera", "kind": "heartbeat", "severity": "info",
                        "signal": {"idle_s": max(0, self.idle.idle_ms() // 1000),
                                   "audio_dbfs": self.audio_dbfs},
                        "jpegs": [encode_jpeg(self.latest_camera)], "notify": False,
                    })
                if h.get("screen", True) and self.latest_screen is not None and now - self.latest_screen_ts < 60:
                    self.counters["heartbeats"] += 1
                    self._enqueue({
                        "source": "screen", "kind": "heartbeat", "severity": "info",
                        "signal": {"idle_s": max(0, self.idle.idle_ms() // 1000),
                                   "audio_dbfs": self.audio_dbfs},
                        "jpegs": [encode_jpeg(self.latest_screen)], "notify": False,
                    })
            self.stop.wait(5)

    # -- audio ("ears" MVP: loopback level monitoring) ----------------
    def _audio_loop(self) -> None:
        a = self.cfg.get("audio") or {}
        if not a.get("enabled", False):
            LOG.info("audio disabled")
            return
        try:
            import soundcard as sc
        except Exception as e:  # noqa: BLE001
            LOG.warning("soundcard not available: %s", e)
            return
        try:
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
        except Exception as e:  # noqa: BLE001
            LOG.warning("audio loopback open failed: %s", e)
            return
        rate = int(a.get("samplerate", 32000))
        chunk_s = max(0.25, float(a.get("chunk_s", 0.5)))
        frames = max(1, int(rate * chunk_s))
        threshold = float(a.get("threshold_dbfs", -20.0))
        spike_need = max(1, int(a.get("spike_frames", 4)))
        cooldown = float(a.get("cooldown_s", 30.0))
        loud = 0
        last_evt = 0.0
        self.audio_ok = True
        LOG.info("audio loopback opened: %s (%d Hz, chunk %.2fs)", mic.name, rate, chunk_s)
        while not self.stop.is_set():
            try:
                data = mic.record(samplerate=rate, numframes=frames, channels=2)
            except Exception as e:  # noqa: BLE001
                self.audio_ok = False
                LOG.warning("audio record failed: %s", e)
                self.stop.wait(5)
                continue
            x = np.asarray(data, dtype=np.float32)
            rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
            db = 20.0 * np.log10(max(rms, 1e-9))
            self.audio_dbfs = round(db, 1)
            self.audio_ts = time.time()
            self.audio_ok = True
            loud = loud + 1 if db > threshold else 0
            now = time.time()
            if loud >= spike_need and now - last_evt >= cooldown and self._allow_call():
                last_evt = now
                loud = 0
                self._enqueue({
                    "source": "audio", "kind": "audio_loud", "severity": "info",
                    "summary": f"环境音突然变大（{db:.1f} dBFS，阈值 {threshold:.0f}）",
                    "signal": {"dbfs": round(db, 1), "threshold": threshold},
                    "no_vlm": True, "notify": False,
                })
            self.stop.wait(0.01)

    # -- identity baseline (non-face) --------------------------------
    def _identity_loop(self) -> None:
        icfg = self.cfg.get("identity") or {}
        if not icfg.get("enabled", True):
            LOG.info("identity disabled")
            return
        interval = float(icfg.get("interval_s", 120))
        last_presence = None
        last_app = None
        last_bucket = time.time()
        while not self.stop.is_set():
            try:
                snap = self.identity.snapshot()
            except Exception:
                LOG.exception("identity snapshot failed")
                self.stop.wait(interval)
                continue
            self.identity_presence = snap["presence"]
            self.identity_foreground = snap["foreground"]
            self.identity_last_ts = time.time()
            changed = (snap["presence"] != last_presence) or (snap["app"] != last_app)
            hourly = time.time() - last_bucket >= 3600
            if changed or hourly:
                last_presence, last_app = snap["presence"], snap["app"]
                last_bucket = time.time()
                if snap["presence"] == "at_desk":
                    what = "键鼠在动"
                else:
                    what = "空闲 {} 秒".format(snap["idle_s"])
                summary = "身份观测：{} · 前景 {}（{}）".format(
                    snap["label"], snap["app"] or "未知", what)
                self._enqueue({
                    "source": "identity", "kind": "identity_state", "severity": "info",
                    "summary": summary, "signal": snap,
                    "no_vlm": True, "notify": False,
                })
            self.stop.wait(interval)

    # -- file monitoring (MVP source) --------------------------------
    def _file_loop(self) -> None:
        fw = self.filewatch
        if fw is None:
            LOG.info("filewatch disabled")
            return
        interval = float((self.cfg.get("filewatch") or {}).get("interval_s", 30))
        while not self.stop.is_set():
            try:
                diff = fw.scan()
            except Exception:
                LOG.exception("filewatch scan failed")
                self.stop.wait(interval)
                continue
            self.filewatch_last_scan = time.time()
            new, changed = diff["new"], diff["changed"]
            if new or changed:
                sample = (new + changed)[:5]
                summary = "文件变化：新增 {}，修改 {}（最近：{}）".format(
                    len(new), len(changed), "、".join(Path(p).name for p in sample))
                self._enqueue({
                    "source": "file", "kind": "file_changed", "severity": "info",
                    "summary": summary,
                    "signal": {"new": len(new), "changed": len(changed), "sample": sample},
                    "no_vlm": True, "notify": False,
                })
            self.stop.wait(interval)

    # -- attention ---------------------------------------------------
    PROMPT = (
        "你是本机的视觉观察者，只能描述图片中可见的事实，禁止编造。"
        "请只输出一个 JSON 对象（不要 markdown、不要解释），字段："
        "summary(中文一句话，<=40字), people(整数), activity(字符串), "
        "risk(low|medium|high), notable(布尔), severity(info|warning|critical), notes(字符串)。"
        "来源={source}，类型={kind}。"
    )

    def _next_attention_model(self) -> str:
        with self._pool_lock:
            model = self.model_pool[self._pool_next % len(self.model_pool)]
            self._pool_next += 1
        return model

    def _attention_loop(self) -> None:
        while not self.stop.is_set():
            try:
                task = self.tasks.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process_task(task)
            except Exception:
                LOG.error("attention task failed\n%s", traceback.format_exc())
                self.counters["errors"] += 1
            finally:
                self.tasks.task_done()

    def _process_task(self, task: dict) -> None:
        source = task.get("source", "?")
        kind = task.get("kind", "?")
        jpegs = task.get("jpegs") or []
        if not self.cfg["attention"].get("enabled", True):
            return
        if task.get("no_vlm"):
            # rule/local-only events never need a frame and must not be dropped
            # just because no keyframe was attached.
            sev = str(task.get("severity") or "info").lower()
            ev = self._make_event(source, kind, sev, str(task.get("summary") or kind),
                                  signals=task.get("signal") or {})
            notified = self.notifier.maybe_notify(ev) if bool(task.get("notify", False)) else False
            if notified:
                self.counters["notifications"] += 1
            self._save_event(ev, jpegs, notified)
            LOG.info("rule event %s %s/%s sev=%s summary=%s", ev["id"], source, kind, sev, ev["summary"][:80])
            return
        if not jpegs:
            return
        self.counters["attention_calls"] += 1
        model = self._next_attention_model()
        verdict: dict = {}
        raw = ""
        t0 = time.time()
        try:
            prompt = self.PROMPT.format(source=source, kind=kind)
            raw = self.gateway.vision(
                jpegs[0], prompt,
                max_tokens=int(self.cfg["attention"].get("max_tokens", 480)),
                model=model,
            )
            verdict = parse_verdict(raw)
        except GatewayError as e:
            self.counters["errors"] += 1
            LOG.warning("gateway error on %s/%s: %s", source, kind, e)
            ev = self._make_event(source, kind + "_analysis_error", "warning",
                                  f"本地视觉模型不可用：HTTP {e.status}")
            ev["vlm"] = {"model": model, "error": str(e)[:300]}
            notified = self.notifier.maybe_notify(ev, cooldown_s=1800)
            self._save_event(ev, jpegs, notified)
            return
        except Exception as e:
            self.counters["errors"] += 1
            LOG.warning("attention error on %s/%s: %s", source, kind, e)
            ev = self._make_event(source, kind + "_analysis_error", "warning", f"本地视觉分析失败：{e}")
            self._save_event(ev, jpegs, False)
            return
        latency_ms = int((time.time() - t0) * 1000)
        rank = {"info": 0, "warning": 1, "critical": 2}
        base_sev = str(task.get("severity") or "info").lower()
        if base_sev not in rank:
            base_sev = "info"
        sev = str(verdict.get("severity") or base_sev).lower()
        if sev not in rank:
            sev = "info"
        # Rule-based severity is a floor: the VLM may escalate but must not downgrade it.
        if rank[base_sev] > rank[sev]:
            sev = base_sev
        summary = str(verdict.get("summary") or raw.strip()[:120] or f"{kind} 事件").strip()
        ev = self._make_event(source, kind, sev, summary,
                              signals=task.get("signal") or {},
                              vlm={"model": model, "latency_ms": latency_ms,
                                   "verdict": verdict, "raw": raw[:600]})
        notify_allowed = bool(task.get("notify", False))
        notified = self.notifier.maybe_notify(ev) if notify_allowed else False
        if notified:
            self.counters["notifications"] += 1
        self._save_event(ev, jpegs, notified)
        LOG.info("event %s %s/%s sev=%s summary=%s (%dms)", ev["id"], source, kind, sev, summary[:60], latency_ms)

    # -- digests -----------------------------------------------------
    def _digest_loop(self) -> None:
        d = self.cfg.get("digest", {})
        while not self.stop.is_set():
            try:
                now = now_local()
                next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                wait = (next_hour - now).total_seconds() + 70
                if self.stop.wait(min(wait, 600)):
                    continue
                if d.get("hourly", True):
                    self._digest_period(now_local() - timedelta(hours=1), now_local(), "hourly")
                cur = now_local()
                if d.get("daily", True) and cur.hour == 0:
                    self._digest_period((cur - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                                        cur.replace(hour=0, minute=0, second=0, microsecond=0), "daily")
            except Exception:
                LOG.error("digest loop error\n%s", traceback.format_exc())
                self.stop.wait(60)

    def _digest_period(self, start: datetime, end: datetime, period: str) -> None:
        events = self.store.query(iso(start), iso(end))
        if not events:
            return
        lines = []
        for e in events[-200:]:
            lines.append(f"{e['ts'][11:16]} [{e.get('source')}] {e.get('kind')} ({e.get('severity')}) {e.get('summary')}")
        prompt = (
            "你是本机监控的记忆整理员。根据下面的事件记录，用中文写一段摘要（<=200字），"
            "按时间顺序，指出异常或值得记住的事；没有异常就简述状态。只输出摘要正文。\n\n" + "\n".join(lines)
        )
        try:
            summary = self.gateway.text(prompt, model=self.gateway.model_digest, max_tokens=400).strip()
        except Exception as e:
            LOG.warning("digest model call failed: %s", e)
            summary = "（本地模型不可用，原始事件已入库）\n" + "\n".join(lines[:40])
        day = start.strftime("%Y%m%d")
        name = f"{period}-{day}"
        if period == "hourly":
            name = f"hourly-{start.strftime('%Y%m%d-%H')}"
        path = self.root / "digests" / f"{name}.md"
        body = f"# {period} digest {start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')}\n\n{summary}\n\n---\n事件数：{len(events)}\n"
        path.write_text(body, encoding="utf-8")
        self.store.add_digest({
            "id": f"dg_{uuid.uuid4().hex[:10]}", "period": period,
            "start_ts": iso(start), "end_ts": iso(end), "path": str(path), "summary": summary,
        })
        LOG.info("digest written: %s (%d events)", path, len(events))

    # -- retention ---------------------------------------------------
    def _retention_loop(self) -> None:
        s = self.cfg.get("storage", {})
        keep_days = int(s.get("keep_days", 14))
        min_free_gb = float(s.get("min_free_gb", 60))
        while not self.stop.wait(6 * 3600):
            try:
                events_dir = self.root / "events"
                cutoff = now_local() - timedelta(days=keep_days)
                removed = 0
                for day in sorted(events_dir.iterdir()):
                    if not day.is_dir() or len(day.name) != 8:
                        continue
                    try:
                        dt = datetime.strptime(day.name, "%Y%m%d").replace(tzinfo=now_local().tzinfo)
                    except ValueError:
                        continue
                    if dt < cutoff:
                        shutil.rmtree(day, ignore_errors=True)
                        removed += 1
                usage = shutil.disk_usage(self.root)
                free_gb = usage.free / (1024 ** 3)
                if free_gb < min_free_gb:
                    for day in sorted(events_dir.iterdir()):
                        if not day.is_dir() or len(day.name) != 8:
                            continue
                        shutil.rmtree(day, ignore_errors=True)
                        removed += 1
                        free_gb = shutil.disk_usage(self.root).free / (1024 ** 3)
                        if free_gb >= min_free_gb:
                            break
                if removed:
                    LOG.info("retention removed %d day dirs; free_gb=%.1f", removed, free_gb)
            except Exception:
                LOG.error("retention error\n%s", traceback.format_exc())

    # -- status ------------------------------------------------------
    def _status_loop(self) -> None:
        interval = int(self.cfg["service"].get("status_interval_s", 10))
        last_queue = 0
        while not self.stop.wait(interval):
            try:
                self.queue_depth = self.tasks.qsize()
                status = {
                    "pid": os.getpid(),
                    "started_at": iso(self.started_at),
                    "uptime_s": int((now_local() - self.started_at).total_seconds()),
                    "camera_ok": self.camera_ok,
                    "screen_ok": self.screen_ok,
                    "audio_ok": self.audio_ok,
                    "audio_dbfs": self.audio_dbfs,
                    "identity_presence": self.identity_presence,
                    "identity_foreground": self.identity_foreground,
                    "filewatch": self.filewatch is not None,
                    "filewatch_last_scan": self.filewatch_last_scan,
                    "queue": self.queue_depth,
                    "tasks_done": self.counters["attention_calls"],
                    "counters": dict(self.counters),
                    "last_event_ts": self.last_event_ts,
                    "gateway_model": self.gateway.model_attention,
                    "model_pool": list(self.model_pool),
                    "attention_workers": self.attention_workers,
                    "screen_blackout": self.screen_blackout,
                    "updated_at": iso(now_local()),
                }
                (self.root / "state" / "status.json").write_text(
                    json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
                (self.root / "state" / "realeyes.pid").write_text(str(os.getpid()), encoding="utf-8")
            except Exception:
                LOG.error("status write failed\n%s", traceback.format_exc())


def parse_verdict(text: str) -> dict:
    if not text:
        return {}
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e <= s:
        return {}
    frag = text[s:e + 1]
    try:
        obj = json.loads(frag)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------- checks

def run_check(cfg: dict, write_events: bool) -> int:
    gw = Gateway(cfg)
    root = Path(cfg["service"]["data_root"])
    ensure_dirs(root)
    store = EventStore(root)
    idle = LastInput()
    idle_s = max(0, idle.idle_ms() // 1000)
    print(f"[idle] {idle_s}s   gateway={gw.base_url}  model={gw.model_attention}")
    ok_all = True
    c = cfg["camera"]
    cap = cv2.VideoCapture(int(c.get("index", 0)), cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(c.get("width", 1280)))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(c.get("height", 720)))
    ok, frame = cap.read() if cap.isOpened() else (False, None)
    cap.release()
    if not ok or frame is None:
        print("[camera] FAIL")
        ok_all = False
    else:
        print(f"[camera] ok shape={frame.shape}")
        jpeg = encode_jpeg(frame)
        prompt = (
            "你是本机的视觉观察者。只能描述图片中可见事实。只输出 JSON："
            "summary(中文一句话), people(整数), activity(字符串), risk(low|medium|high), "
            "notable(布尔), severity(info|warning|critical), notes(字符串)。来源=camera，类型=check。"
        )
        try:
            raw = gw.vision(jpeg, prompt)
            print("[camera-vlm]", json.dumps(parse_verdict(raw), ensure_ascii=False)[:600])
            if write_events:
                ev = {"id": f"evt_{compact(now_local())}_{uuid.uuid4().hex[:6]}", "ts": iso(now_local()),
                      "source": "camera", "kind": "check", "severity": "info",
                      "summary": str(parse_verdict(raw).get("summary", raw[:80])), "signals": {"idle_s": idle_s},
                      "vlm": {"model": gw.model_attention, "verdict": parse_verdict(raw), "raw": raw[:400]},
                      "budget": {"cloud_used_today": 0, "cloud_cap": 100}}
                store.add(ev, [], False)
        except Exception as e:
            print("[camera-vlm] ERROR:", e)
            ok_all = False
    if mss is None:
        print("[screen] mss missing")
        ok_all = False
    else:
        with open_mss() as sct:
            shot = sct.grab(sct.monitors[1])
            sframe = np.asarray(shot)[:, :, :3].copy()
        print(f"[screen] ok shape={sframe.shape}")
        jpeg = encode_jpeg(sframe)
        prompt = (
            "你是本机的视觉观察者。只能描述图片中可见事实。只输出 JSON："
            "summary(中文一句话), people(整数), activity(字符串), risk(low|medium|high), "
            "notable(布尔), severity(info|warning|critical), notes(字符串)。来源=screen，类型=check。"
        )
        try:
            raw = gw.vision(jpeg, prompt)
            print("[screen-vlm]", json.dumps(parse_verdict(raw), ensure_ascii=False)[:600])
            if write_events:
                ev = {"id": f"evt_{compact(now_local())}_{uuid.uuid4().hex[:6]}", "ts": iso(now_local()),
                      "source": "screen", "kind": "check", "severity": "info",
                      "summary": str(parse_verdict(raw).get("summary", raw[:80])), "signals": {"idle_s": idle_s},
                      "vlm": {"model": gw.model_attention, "verdict": parse_verdict(raw), "raw": raw[:400]},
                      "budget": {"cloud_used_today": 0, "cloud_cap": 100}}
                store.add(ev, [], False)
        except Exception as e:
            print("[screen-vlm] ERROR:", e)
            ok_all = False
    store.close()
    return 0 if ok_all else 1


def run_recall(cfg: dict, text: str, hours: float) -> int:
    root = Path(cfg["service"]["data_root"])
    events_jsonl = root / "events" / "events.jsonl"
    hits = memory.recall(events_jsonl, text, hours=hours, limit=40)
    st = memory.stats(events_jsonl, hours=hours)
    print(f"window last {hours:g}h | events={st['events']} | hits={len(hits)}")
    for e in hits:
        print(f"{e['ts'][:19]} [{e.get('source')}/{e.get('kind')}] {e.get('severity')}: {e.get('summary')}")
        kf = e.get("keyframes") or []
        if kf:
            print(f"   keyframe: {kf[0]}")
    q = text.lower()
    ddir = root / "digests"
    if ddir.exists():
        for f in sorted(ddir.glob("*.md"))[-48:]:
            try:
                body = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if q in body.lower():
                print(f"\n--- {f.name} ---\n{body[:600]}")
    return 0


# ---------------------------------------------------------------- selftest


class _MockGateway(BaseHTTPRequestHandler):
    """OpenAI-compatible mock so selftest never needs the real local model."""

    def log_message(self, *args):  # silence http.server logging
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
        except Exception:
            pass
        verdict = {
            "summary": "检测到画面出现移动物体", "people": 1, "activity": "移动",
            "risk": "low", "notable": True, "severity": "info", "notes": "",
        }
        payload = {
            "id": "mock", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": json.dumps(verdict, ensure_ascii=False)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _make_test_video(path: Path, frames: int, mode: str, fps: int = 10) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), float(fps), (320, 240))
    if not writer.isOpened():
        raise RuntimeError("cannot create test video: " + str(path))
    base = np.full((240, 320, 3), 60, dtype=np.uint8)
    third = frames // 3
    for i in range(frames):
        if mode == "black":
            frame = np.zeros((240, 320, 3), dtype=np.uint8)
        else:
            frame = base.copy()
            if third <= i < frames - third:
                x = 20 + (i - third) * 20
                cv2.rectangle(frame, (x, 70), (x + 140, 170), (230, 230, 230), -1)
        writer.write(frame)
    writer.release()


def _read_events(root: Path) -> list[dict]:
    p = root / "events" / "events.jsonl"
    out = []
    if not p.exists():
        return out
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _run_selftest_case(name: str, cfg: dict, want_kind: str, timeout_s: float, root: Path) -> bool:
    svc = RealEyes(cfg)
    svc.start()
    found = None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.2)
        found = next((e for e in _read_events(root) if e.get("kind") == want_kind), None)
        if found:
            break
    svc.shutdown()
    if not found:
        print(f"[FAIL] {name}: no {want_kind} event within {timeout_s:.0f}s; events={len(_read_events(root))}")
        return False
    detail = f"kind={found['kind']} severity={found['severity']}"
    if found.get("vlm"):
        detail += f" vlm={str((found['vlm'].get('verdict') or {}).get('summary', ''))[:24]}"
    print(f"[PASS] {name}: {detail}")
    return True


def run_selftest(base_cfg: dict) -> int:
    """Deterministic L0->L1 test: synthetic video + mock gateway, no camera/GPU needed."""
    base = copy.deepcopy(base_cfg)
    results: list[bool] = []
    with tempfile.TemporaryDirectory(prefix="realeyes-selftest-",
                                     ignore_cleanup_errors=True) as td_raw:
        td = Path(td_raw)
        motion = td / "motion.avi"
        black = td / "black.avi"
        _make_test_video(motion, 70, "motion")
        _make_test_video(black, 40, "black")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockGateway)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def make_cfg(root: Path, source: Path, blackout_dwell: float) -> dict:
            cfg = copy.deepcopy(base)
            cfg["service"] = dict(cfg["service"])
            cfg["service"]["data_root"] = str(root)
            cfg["gateway"] = dict(cfg["gateway"])
            cfg["gateway"]["base_url"] = f"http://127.0.0.1:{port}/v1"
            cfg["gateway"]["api_key_env"] = ""
            cfg["camera"] = {**cfg["camera"], "enabled": True, "source": str(source),
                             "width": 320, "height": 240, "fps": 10,
                             "motion_threshold": 0.01, "motion_min_frames": 2,
                             "cooldown_s": 3, "big_motion": 0.02, "away_idle_s": 2 ** 30,
                             "blackout_luma": 8, "blackout_dwell_s": blackout_dwell}
            cfg["screen"] = {**cfg.get("screen", {}), "enabled": False}
            cfg["heartbeat"] = {**cfg.get("heartbeat", {}), "enabled": False}
            cfg["attention"] = {**cfg["attention"], "max_calls_per_hour": 100, "queue_size": 8}
            cfg["digest"] = {"hourly": False, "daily": False}
            cfg["notify"] = {**cfg.get("notify", {}), "enabled": False}
            return cfg

        try:
            root1 = td / "data1"
            results.append(_run_selftest_case("motion", make_cfg(root1, motion, 99999), "motion", 25, root1))
            evs1 = _read_events(root1)
            motion_ev = next((e for e in evs1 if e.get("kind") == "motion"), None)
            ok = bool(motion_ev and (motion_ev.get("vlm") or {}).get("verdict"))
            print(("[PASS] " if ok else "[FAIL] ") + "motion event carries local-VLM verdict")
            results.append(ok)

            root2 = td / "data2"
            results.append(_run_selftest_case("camera_covered", make_cfg(root2, black, 1.0),
                                              "camera_covered", 20, root2))
            evs2 = _read_events(root2)
            covered = next((e for e in evs2 if e.get("kind") == "camera_covered"), None)
            ok = bool(covered) and covered.get("severity") == "warning" and not covered.get("vlm")
            print(("[PASS] " if ok else "[FAIL] ") + "camera_covered keeps warning severity without VLM downgrade")
            results.append(ok)

            ok = bool(evs1) and all(isinstance(e, dict) and e.get("ts") for e in evs1 + evs2)
            print(("[PASS] " if ok else "[FAIL] ") + "events are valid JSONL with timestamps")
            results.append(ok)

            # file monitoring module
            with tempfile.TemporaryDirectory(prefix="realeyes-fw-") as fw_td:
                fw = filewatch.FileWatch([fw_td])
                fw.scan()
                (Path(fw_td) / "hello.txt").write_text("hi", encoding="utf-8")
                diff = fw.scan()
                ok = any(p.endswith("hello.txt") for p in diff["new"])
            print(("[PASS] " if ok else "[FAIL] ") + "filewatch detects new files")
            results.append(ok)

            # memory recall module (M3) over synthetic JSONL
            mem_file = td / "events" / "events.jsonl"
            mem_file.parent.mkdir(parents=True, exist_ok=True)
            mem_file.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
                {"ts": now_local().isoformat(), "source": "test", "kind": "motion",
                 "severity": "info", "summary": "移动物体"},
                {"ts": now_local().isoformat(), "source": "test", "kind": "file_changed",
                 "severity": "info", "summary": "hello.txt 新增"},
            ]) + "\n", encoding="utf-8")
            ok = len(memory.recall(mem_file, "hello.txt", hours=1)) == 1
            print(("[PASS] " if ok else "[FAIL] ") + "memory recall finds events by keyword")
            results.append(ok)

            # identity baseline (Windows input idle + foreground window)
            snap = identity.IdentityTracker().snapshot()
            ok = snap.get("presence") in ("at_desk", "recent", "away") and "confidence" in snap
            print(("[PASS] " if ok else "[FAIL] ") + "identity baseline yields presence snapshot")
            results.append(ok)
        finally:
            server.shutdown()
            server.server_close()
    passed = sum(results)
    print(f"\nselftest {'PASSED' if passed == len(results) else 'FAILED'} ({passed}/{len(results)} checks)")
    return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RealEyes MVP")
    ap.add_argument("--config", default=None)
    ap.add_argument("--check", action="store_true", help="capture + one local VLM call, no service")
    ap.add_argument("--once", action="store_true", help="like --check but writes events")
    ap.add_argument("--duration", type=float, default=0, help="run service for N seconds (test)")
    ap.add_argument("--simulate-notify", action="store_true", help="send a test desktop notification")
    ap.add_argument("--recall", default=None, metavar="TEXT", help="search recent events/digests for TEXT")
    ap.add_argument("--hours", type=float, default=24, help="recall window in hours (default 24)")
    ap.add_argument("--selftest", action="store_true",
                    help="deterministic L0->L1 test with synthetic video + mock gateway (no camera/GPU)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg)
    if args.selftest:
        return run_selftest(cfg)
    if args.recall:
        return run_recall(cfg, args.recall, args.hours)
    if args.simulate_notify:
        n = Notifier(cfg)
        ev = {"source": "test", "kind": "test", "severity": "warning",
              "summary": "RealEyes 通知测试：如果你看到这条气泡，通知链路正常。"}
        ok = n.maybe_notify(ev, cooldown_s=0)
        print("notified:", ok)
        return 0 if ok else 1
    if args.check or args.once:
        return run_check(cfg, write_events=args.once)
    svc = RealEyes(cfg)
    svc.start()
    def _sig(_signum, _frame):
        LOG.info("signal received")
        svc.stop.set()
    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass
    if args.duration and args.duration > 0:
        svc.stop.wait(args.duration)
        svc.shutdown()
        return 0
    try:
        while not svc.stop.is_set():
            svc.stop.wait(1.0)
    finally:
        svc.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
