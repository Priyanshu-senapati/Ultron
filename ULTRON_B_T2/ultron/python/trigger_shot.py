"""Force a screenshot + LLaVA roundtrip and observe the result."""
import asyncio, json, os, time, tomllib
from pathlib import Path
import websockets

cfg = tomllib.load(open(Path(os.environ["APPDATA"])/"ULTRON"/"config.toml","rb"))
url = f"ws://{cfg['bridge']['bind']}/ws"
tok = cfg["bridge"]["token"]

async def main():
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op":"hello","token":tok,"role":"trigger"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["screenshot_captured","visual_label","insight_snapshot"]}))
        await ws.send(json.dumps({"op":"publish","kind":"request_screenshot","payload":{"reason":"on_demand"}}))
        print("fired request_screenshot, listening 30s for fallout...")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("op") != "event": continue
            k = m.get("kind")
            p = m.get("payload",{})
            if k == "screenshot_captured":
                print(f"  SHOT: {p.get('path','')[-50:]} reason={p.get('reason')}")
            elif k == "visual_label":
                print(f"  LABEL: {p.get('label')!r}")
            elif k == "insight_snapshot":
                vl = p.get("visual_label")
                if vl:
                    print(f"  SNAP visual_label={vl!r} focus_app={p.get('focus_app')} cat={p.get('focus_category')}")

asyncio.run(main())
