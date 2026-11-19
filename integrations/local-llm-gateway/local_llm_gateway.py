"""Local LLM gateway: on-demand llama.cpp workers behind an OpenAI-compatible API.

Why: the 22 GB GPU must stay mostly free (ComfyUI, other tools), so no model is
loaded at startup. A worker (llama-server.exe) is spawned the first time its
model is requested and is stopped again after `idle_ttl_seconds` without use,
releasing all VRAM. Only one worker runs at a time (single GPU).

Endpoints
  GET  /healthz                 gateway + loaded-worker status
  GET  /v1/models               OpenAI model list (all configured, loaded or not)
  GET  /admin/status            detailed state
  POST /admin/unload            {"model": "<id>"?} unload now
  POST /admin/reload            stop worker and re-read the config file
  POST /admin/quit              stop worker and exit the gateway process
  POST /v1/chat/completions     OpenAI chat completions (stream or not) -> proxy
  POST /v1/completions          legacy completions -> proxy

Config: F:\\scripts\\llm\\local_llm_models.json
Run:    python local_llm_gateway.py [--config PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

LOGGER = logging.getLogger("local_llm_gateway")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
@dataclass
class ModelSpec:
    model_id: str
    display_name: str
    path: str
    mmproj: Optional[str] = None
    ctx_size: int = 32768
    parallel: int = 1
    min_free_vram_mb: int = 0
    load_timeout_seconds: int = 300
    extra_args: list[str] = field(default_factory=list)
    # None = inherit gateway-level default; {} = explicitly send no kwargs
    chat_template_kwargs: Optional[dict[str, Any]] = None


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8322
    idle_ttl_seconds: int = 900
    internal_base_port: int = 8401
    nvidia_smi: str = "nvidia-smi.exe"
    llama_server_exe: str = ""
    exe_args: list[str] = field(default_factory=list)
    log_dir: str = "F:\\logs"
    vram_safety_mb: int = 1024
    chat_template_kwargs: dict[str, Any] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)

    @staticmethod
    def load(path: str) -> "GatewayConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cfg = GatewayConfig(
            host=raw.get("host", "127.0.0.1"),
            port=int(raw.get("port", 8322)),
            idle_ttl_seconds=int(raw.get("idle_ttl_seconds", 900)),
            internal_base_port=int(raw.get("internal_base_port", 8401)),
            nvidia_smi=raw.get("nvidia_smi", "nvidia-smi.exe"),
            llama_server_exe=raw.get("llama_server_exe", ""),
            exe_args=list(raw.get("exe_args", [])),
            log_dir=raw.get("log_dir", "F:\\logs"),
            vram_safety_mb=int(raw.get("vram_safety_mb", 1024)),
            chat_template_kwargs=dict(raw.get("chat_template_kwargs", {})),
        )
        for mid, m in raw.get("models", {}).items():
            cfg.models[mid] = ModelSpec(
                model_id=mid,
                display_name=m.get("display_name", mid),
                path=m["path"],
                mmproj=m.get("mmproj"),
                ctx_size=int(m.get("ctx_size", 32768)),
                parallel=int(m.get("parallel", 1)),
                min_free_vram_mb=int(m.get("min_free_vram_mb", 0)),
                load_timeout_seconds=int(m.get("load_timeout_seconds", 300)),
                extra_args=list(m.get("extra_args", [])),
                chat_template_kwargs=(None if "chat_template_kwargs" not in m
                                      else dict(m.get("chat_template_kwargs") or {})),
            )
        return cfg


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #
@dataclass
class Worker:
    spec: ModelSpec
    port: int
    proc: subprocess.Popen
    started_at: float
    last_used: float
    log_path: str

    @property
    def model_id(self) -> str:
        return self.spec.model_id


def now() -> float:
    return time.time()


def free_vram_mb(nvidia_smi: str) -> Optional[int]:
    try:
        out = subprocess.run(
            [nvidia_smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        )
        if out.returncode != 0:
            return None
        return int(out.stdout.strip().splitlines()[0].strip())
    except Exception:  # noqa: BLE001 - best effort guard only
        return None


def vram_guard(nvidia_smi: str, need_mb: int, safety_mb: int) -> tuple[bool, str]:
    """Return (ok, reason). If nvidia-smi is unavailable we optimistically allow."""
    free = free_vram_mb(nvidia_smi)
    if free is None:
        return True, "vram-unknown"
    required = need_mb + safety_mb
    if free < required:
        return False, f"gpu busy: free={free} MiB, need≈{required} MiB (another GPU app, e.g. ComfyUI, is using VRAM)"
    return True, f"free={free} MiB"


class Manager:
    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg
        self.worker: Optional[Worker] = None
        self.lock = asyncio.Lock()
        self.active_requests = 0
        self.idle_task: Optional[asyncio.Task] = None
        self.client: Optional[httpx.AsyncClient] = None
        self.stopping = False

    # -- lifecycle helpers ------------------------------------------------
    def _port_for(self, model_id: str) -> int:
        return self.cfg.internal_base_port + list(self.cfg.models).index(model_id)

    def _server_log(self, model_id: str) -> str:
        Path(self.cfg.log_dir).mkdir(parents=True, exist_ok=True)
        path = os.path.join(self.cfg.log_dir, f"llama_server_{model_id}.log")
        try:
            if os.path.getsize(path) > 64 * 1024 * 1024:
                os.replace(path, path + ".1")
        except OSError:
            pass
        return path

    def _build_args(self, spec: ModelSpec, port: int) -> list[str]:
        args = [
            self.cfg.llama_server_exe,
            *self.cfg.exe_args,
            "--model", spec.path,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--ctx-size", str(spec.ctx_size),
            "--n-gpu-layers", "99",
            "--parallel", str(max(1, spec.parallel)),
        ]
        if spec.mmproj:
            args += ["--mmproj", spec.mmproj]
        args += spec.extra_args
        return args

    async def _wait_healthy(self, port: int, timeout: float) -> bool:
        deadline = now() + timeout
        url = f"http://127.0.0.1:{port}/health"
        while now() < deadline:
            if self.worker and self.worker.proc.poll() is not None:
                return False
            try:
                if self.client is None:
                    return False
                r = await self.client.get(url, timeout=2.0)
                if r.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 - health polling
                pass
            await asyncio.sleep(1.0)
        return False

    async def _stop_worker(self, reason: str) -> None:
        w = self.worker
        if w is None:
            return
        LOGGER.info("stopping worker %s (%s)", w.model_id, reason)
        self.worker = None
        proc = w.proc
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 10)
        except Exception:  # noqa: BLE001 - teardown best effort
            LOGGER.exception("failed to stop worker %s", w.model_id)

    async def ensure_model(self, model_id: str) -> Worker:
        spec = self.cfg.models.get(model_id)
        if spec is None:
            # accept "provider/model" style ids from clients
            for mid, s in self.cfg.models.items():
                if model_id.endswith("/" + mid):
                    spec, model_id = s, mid
                    break
        if spec is None:
            raise KeyError(model_id)

        async with self.lock:
            if self.worker and self.worker.model_id == spec.model_id:
                self.worker.last_used = now()
                return self.worker
            if self.worker:
                deadline = now() + 120
                while self.active_requests > 0 and now() < deadline:
                    await asyncio.sleep(0.5)
                await self._stop_worker(f"switch to {spec.model_id}")

            ok, why = vram_guard(self.cfg.nvidia_smi, spec.min_free_vram_mb, self.cfg.vram_safety_mb)
            if not ok:
                LOGGER.warning("refusing to load %s: %s", spec.model_id, why)
                raise RuntimeError(why)

            port = self._port_for(spec.model_id)
            log_path = self._server_log(spec.model_id)
            args = self._build_args(spec, port)
            LOGGER.info("starting worker %s on :%d (%s)", spec.model_id, port, " ".join(args))
            log_fh = open(log_path, "ab")  # noqa: SIM115 - owned by worker lifetime
            log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start {spec.model_id} =====\n".encode())
            log_fh.flush()
            env = dict(os.environ)
            env.setdefault("CUDA_VISIBLE_DEVICES", "0")
            proc = subprocess.Popen(
                args, stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=os.path.dirname(self.cfg.llama_server_exe) or None,
                creationflags=CREATE_NO_WINDOW, env=env,
            )
            self.worker = Worker(spec=spec, port=port, proc=proc, started_at=now(), last_used=now(), log_path=log_path)
            try:
                healthy = await self._wait_healthy(port, spec.load_timeout_seconds)
            finally:
                log_fh.close()
            if not healthy:
                tail = _tail(log_path, 40)
                await self._stop_worker("health check failed")
                raise RuntimeError(
                    f"model {spec.model_id} failed to start within {spec.load_timeout_seconds}s; log tail:\n{tail}"
                )
            LOGGER.info("worker %s ready on :%d after %.1fs", spec.model_id, port, now() - self.worker.started_at)
            return self.worker

    async def touch(self) -> None:
        if self.worker:
            self.worker.last_used = now()

    async def unload(self, model_id: Optional[str] = None) -> bool:
        async with self.lock:
            if self.worker is None:
                return False
            if model_id and model_id != self.worker.model_id and not model_id.endswith("/" + self.worker.model_id):
                return False
            await self._stop_worker("admin unload")
            return True

    async def idle_loop(self) -> None:
        while not self.stopping:
            await asyncio.sleep(30)
            try:
                if self.worker and self.active_requests == 0:
                    idle = now() - self.worker.last_used
                    if idle > self.cfg.idle_ttl_seconds:
                        async with self.lock:
                            await self._stop_worker(f"idle {int(idle)}s")
            except Exception:  # noqa: BLE001 - monitor must survive
                LOGGER.exception("idle monitor error")

    def status(self) -> dict[str, Any]:
        w = self.worker
        return {
            "ok": True,
            "pid": os.getpid(),
            "port": self.cfg.port,
            "idle_ttl_seconds": self.cfg.idle_ttl_seconds,
            "active_requests": self.active_requests,
            "loaded": None if w is None else {
                "model": w.model_id,
                "port": w.port,
                "spawned_at": w.started_at,
                "last_used": w.last_used,
                "idle_seconds": round(now() - w.last_used, 1),
                "pid": w.proc.pid,
                "log": w.log_path,
            },
            "models": {
                mid: {"display_name": s.display_name, "path": s.path, "vision": bool(s.mmproj),
                      "ctx_size": s.ctx_size, "min_free_vram_mb": s.min_free_vram_mb}
                for mid, s in self.cfg.models.items()
            },
        }


def _tail(path: str, lines: int) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read()[-20000:]
        return data.decode("utf-8", "replace")[-4000:]
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
def create_app(cfg: GatewayConfig, config_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="local-llm-gateway", version="1.0")
    mgr = Manager(cfg)

    @app.on_event("startup")
    async def _startup() -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(cfg.log_dir, "local_llm_gateway.log"), maxBytes=8 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        mgr.client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=None, write=None, pool=None))
        mgr.idle_task = asyncio.create_task(mgr.idle_loop())
        missing = [mid for mid, s in cfg.models.items() if not os.path.isfile(s.path)]
        if not os.path.isfile(cfg.llama_server_exe):
            LOGGER.error("llama_server_exe not found: %s", cfg.llama_server_exe)
        if missing:
            LOGGER.warning("model files not downloaded yet: %s", ", ".join(missing))
        LOGGER.info("gateway ready on %s:%d, %d models configured, idle TTL %ss",
                    cfg.host, cfg.port, len(cfg.models), cfg.idle_ttl_seconds)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        mgr.stopping = True
        if mgr.idle_task:
            mgr.idle_task.cancel()
        async with mgr.lock:
            await mgr._stop_worker("gateway shutdown")
        if mgr.client:
            await mgr.client.aclose()

    def err(status: int, message: str, etype: str = "invalid_request_error") -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"message": message, "type": etype}})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "loaded": None if mgr.worker is None else mgr.worker.model_id}

    @app.get("/admin/status")
    async def admin_status() -> dict[str, Any]:
        return mgr.status()

    @app.post("/admin/unload")
    async def admin_unload(request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - body optional
            pass
        stopped = await mgr.unload(body.get("model"))
        return {"ok": True, "unloaded": stopped, "status": mgr.status()}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        created = int(now())
        return {"object": "list", "data": [
            {"id": mid, "object": "model", "created": created, "owned_by": "local",
             "meta": {"display_name": s.display_name, "vision": bool(s.mmproj)}}
            for mid, s in mgr.cfg.models.items()
        ]}

    @app.post("/admin/reload")
    async def admin_reload() -> dict[str, Any]:
        if not config_path:
            return {"ok": False, "error": "gateway started without a config path"}
        async with mgr.lock:
            await mgr._stop_worker("config reload")
            mgr.cfg = GatewayConfig.load(config_path)
        LOGGER.info("config reloaded from %s: %s", config_path, ", ".join(mgr.cfg.models))
        return {"ok": True, "models": list(mgr.cfg.models)}

    @app.post("/admin/quit")
    async def admin_quit() -> dict[str, Any]:
        async with mgr.lock:
            await mgr._stop_worker("admin quit")
        LOGGER.info("admin quit requested")
        import threading
        threading.Timer(0.4, lambda: os._exit(0)).start()
        return {"ok": True, "quitting": True}

    async def proxy(request: Request, path: str) -> Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed body
            return err(400, "request body must be JSON")
        model_id = body.get("model")
        if not isinstance(model_id, str) or not model_id:
            return err(400, "missing required field: model")
        try:
            worker = await mgr.ensure_model(model_id)
        except KeyError:
            return err(404, f"unknown model: {model_id}; configured: {', '.join(mgr.cfg.models)}", "model_not_found")
        except RuntimeError as exc:
            return err(503, str(exc), "server_error")

        stream = bool(body.get("stream"))
        if path == "/v1/chat/completions":
            effective = worker.spec.chat_template_kwargs
            if effective is None:
                effective = cfg.chat_template_kwargs
            if effective:
                ctk = body.get("chat_template_kwargs")
                if not isinstance(ctk, dict):
                    ctk = {}
                for key, value in effective.items():
                    ctk.setdefault(key, value)
                body["chat_template_kwargs"] = ctk
        url = f"http://127.0.0.1:{worker.port}{path}"
        mgr.active_requests += 1
        try:
            if stream:
                async def _iter():
                    try:
                        async with mgr.client.stream("POST", url, json=body) as upstream:
                            if upstream.status_code >= 400:
                                text = (await upstream.aread()).decode("utf-8", "replace")
                                yield json.dumps({"error": {"message": text[:2000], "type": "server_error"}}).encode()
                                return
                            async for chunk in upstream.aiter_raw():
                                yield chunk
                    finally:
                        mgr.active_requests -= 1
                        await mgr.touch()

                return StreamingResponse(_iter(), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            resp = await mgr.client.post(url, json=body)
            mgr.active_requests -= 1
            await mgr.touch()
            if resp.status_code >= 400:
                return Response(content=resp.content, status_code=resp.status_code,
                                media_type=resp.headers.get("content-type", "application/json"))
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"))
        except Exception as exc:  # noqa: BLE001 - upstream failure
            if not stream:
                mgr.active_requests -= 1
            LOGGER.exception("proxy error for %s", model_id)
            return err(502, f"upstream error: {exc}", "server_error")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await proxy(request, "/v1/chat/completions")

    @app.post("/v1/completions")
    async def completions(request: Request) -> Response:
        return await proxy(request, "/v1/completions")

    return app


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="On-demand local llama.cpp OpenAI-compatible gateway")
    parser.add_argument("--config", default=str(here / "local_llm_models.json"))
    args = parser.parse_args()
    cfg = GatewayConfig.load(args.config)
    uvicorn.run(create_app(cfg, args.config), host=cfg.host, port=cfg.port, log_level="info", timeout_keep_alive=120)


if __name__ == "__main__":
    main()
