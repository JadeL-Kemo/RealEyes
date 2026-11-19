"""End-to-end smoke test for the on-demand local-llm gateway (http://127.0.0.1:8322).

Covers: health, model list, non-stream chat, streaming chat, tool calls,
vision request via mmproj, and admin status/unload.
Run:  python F:\\scripts\\llm\\smoke_test_gateway.py [model_id]
"""
from __future__ import annotations

import base64
import io
import json
import sys
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

BASE = "http://127.0.0.1:8322"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5-9b"


def hr(title: str) -> None:
    print(f"\n===== {title} =====")


def make_test_image(text: str = "LOCAL VLM OK 42") -> str:
    img = Image.new("RGB", (640, 240), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 64)
    except OSError:
        font = ImageFont.load_default()
    draw.rectangle([10, 10, 630, 230], outline=(0, 0, 0), width=4)
    draw.text((40, 80), text, fill=(200, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def main() -> int:
    t0 = time.time()
    with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=180.0, write=60.0, pool=10.0)) as c:
        hr("health")
        print(c.get(f"{BASE}/healthz").text)

        hr("models")
        print(json.dumps(c.get(f"{BASE}/v1/models").json(), ensure_ascii=False)[:600])

        hr(f"chat non-stream [{MODEL}]")
        t = time.time()
        r = c.post(f"{BASE}/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "用一句话回答：1+1等于几？"}],
            "max_tokens": 128,
            "temperature": 0.2,
        })
        j = r.json()
        msg = j["choices"][0]["message"]
        print(f"status={r.status_code} elapsed={time.time()-t:.1f}s "
              f"ct={j.get('usage', {}).get('completion_tokens')} content={msg.get('content')!r} "
              f"reasoning={msg.get('reasoning_content')!r}")

        hr("chat stream")
        t = time.time()
        chunks = 0
        text = ""
        with c.stream("POST", f"{BASE}/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "数到五，只输出数字。"}],
            "max_tokens": 64,
            "temperature": 0.2,
            "stream": True,
        }) as resp:
            print(f"status={resp.status_code}")
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    raw = line[6:]
                    if raw == "[DONE]":
                        break
                    chunks += 1
                    try:
                        delta = json.loads(raw)["choices"][0].get("delta", {})
                        text += delta.get("content") or ""
                    except Exception:
                        pass
        print(f"elapsed={time.time()-t:.1f}s chunks={chunks} text={text!r}")

        hr("tool call")
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string", "description": "City name"}},
                    "required": ["city"],
                },
            },
        }]
        r = c.post(f"{BASE}/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "请用 get_weather 工具查询上海今天的天气。"}],
            "tools": tools,
            "max_tokens": 256,
            "temperature": 0.2,
        })
        j = r.json()
        msg = j["choices"][0]["message"]
        print(f"status={r.status_code} finish={j['choices'][0].get('finish_reason')} "
              f"tool_calls={json.dumps(msg.get('tool_calls'), ensure_ascii=False)}")

        hr("vision")
        b64 = make_test_image()
        data_url = f"data:image/png;base64,{b64}"
        t = time.time()
        r = c.post(f"{BASE}/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "图片里写了什么？只回答图片中的文字内容。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "max_tokens": 128,
            "temperature": 0.0,
        })
        print(f"status={r.status_code} elapsed={time.time()-t:.1f}s")
        if r.status_code == 200:
            print(f"vision answer={r.json()['choices'][0]['message'].get('content')!r}")
        else:
            print(f"vision error body={r.text[:800]}")

        hr("admin status")
        st = c.get(f"{BASE}/admin/status").json()
        print(json.dumps(st, ensure_ascii=False)[:900])

    print(f"\nTOTAL elapsed={time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
