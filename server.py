#!/usr/bin/env python3
"""PDF 語音朗讀器 - OpenAI TTS Proxy + 靜態檔案伺服器"""
import os, json, httpx, uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="語音書 PDF Reader")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/pdf", StaticFiles(directory=STATIC_DIR), name="pdf")

SERVER_API_KEY = os.environ.get("OPENAI_API_KEY", "")

class TTSRequest(BaseModel):
    text: str
    voice: str = "nova"
    speed: float = 1.0
    model: str = "tts-1-hd"

@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("<h1>語音書</h1><p>index.html 未找到</p>")

@app.get("/manifest.json")
async def manifest():
    path = os.path.join(STATIC_DIR, "manifest.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json")
    return JSONResponse({})

@app.get("/health")
async def health():
    return {"status": "ok", "app": "pdf-reader", "openai_configured": bool(SERVER_API_KEY)}

@app.get("/api/openai-key-status")
async def openai_key_status():
    """Check if server-side OpenAI key is configured."""
    return {"configured": bool(SERVER_API_KEY)}

@app.post("/api/tts")
async def text_to_speech(req: TTSRequest):
    """Proxy to OpenAI TTS API. Accepts client key header or server env key."""
    # Priority: request header > server env
    api_key = req.headers.get("X-OpenAI-Key", "") if hasattr(req, 'headers') else ""
    if not api_key:
        api_key = SERVER_API_KEY
    if not api_key:
        raise HTTPException(400, "OpenAI API key not configured. Set OPENAI_API_KEY env var or provide in app settings.")
    
    # Validate text length
    if len(req.text) > 4096:
        raise HTTPException(400, f"Text too long ({len(req.text)} chars). Max 4096.")
    if not req.text.strip():
        raise HTTPException(400, "Empty text")
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": req.model,
                    "input": req.text,
                    "voice": req.voice,
                    "response_format": "mp3",
                    "speed": min(max(req.speed, 0.25), 4.0),
                },
            )
            if resp.status_code != 200:
                detail = resp.json().get("error", {}).get("message", resp.text)
                raise HTTPException(resp.status_code, f"OpenAI error: {detail}")
            
            return Response(
                content=resp.content,
                media_type="audio/mpeg",
                headers={
                    "X-Segment-Length": str(len(req.text)),
                    "Cache-Control": "no-cache",
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "OpenAI TTS timeout")
    except httpx.RequestError as e:
        raise HTTPException(502, f"OpenAI connection error: {str(e)}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    uvicorn.run(app, host="0.0.0.0", port=port)
