#!/usr/bin/env python3
"""語音書 PDF Reader — Edge TTS 完全免費，免登入"""
import os, io, uvicorn
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="語音書 PDF Reader")
STATIC_DIR = Path(__file__).parent
app.mount("/pdf", StaticFiles(directory=str(STATIC_DIR)), name="pdf")

# ─── Security Headers ───
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

# ─── Edge TTS Voices ───
VOICE_ZH = "zh-TW-HsiaoChenNeural"   # 台灣口音女聲 (曉辰)
VOICE_EN = "en-US-JennyNeural"        # 美國女聲 (Jenny)

class TTSRequest(BaseModel):
    text: str
    lang: str = "zh"  # "zh" or "en"
    speed: float = 1.0

# ─── Public Routes ───
@app.get("/", response_class=HTMLResponse)
async def index():
    path = STATIC_DIR / "index.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>語音書</h1>")

@app.get("/manifest.json")
async def manifest():
    path = STATIC_DIR / "manifest.json"
    if path.exists():
        return FileResponse(str(path), media_type="application/json")
    return JSONResponse({})

@app.get("/health")
async def health():
    return {"status": "ok", "tts": "edge-tts", "auth": False}

# ─── Edge TTS (完全免費，無需登入) ───
@app.post("/api/tts")
async def text_to_speech(req: TTSRequest):
    if len(req.text) > 4096:
        raise HTTPException(400, "文字過長（上限 4096 字）")
    if not req.text.strip():
        raise HTTPException(400, "空白文字")

    voice = VOICE_ZH if req.lang == "zh" else VOICE_EN

    try:
        import edge_tts
        rate = f"+{int((req.speed - 1) * 50)}%" if req.speed >= 1 else f"-{int((1 - req.speed) * 50)}%"
        communicate = edge_tts.Communicate(req.text, voice, rate=rate)
        audio_data = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data.write(chunk["data"])
        audio_bytes = audio_data.getvalue()
        if not audio_bytes:
            raise HTTPException(500, "TTS 未產生音訊")
        return Response(content=audio_bytes, media_type="audio/mpeg")
    except ImportError:
        raise HTTPException(503, "Edge TTS 未安裝 (pip install edge-tts)")
    except Exception as e:
        raise HTTPException(500, f"TTS 錯誤: {str(e)}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    uvicorn.run(app, host="0.0.0.0", port=port)
