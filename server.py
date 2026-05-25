#!/usr/bin/env python3
"""PDF 語音朗讀器 - 簡單靜態檔案伺服器"""
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import os, json, uvicorn

app = FastAPI(title="語音書 PDF Reader")

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/pdf", StaticFiles(directory=STATIC_DIR), name="pdf")

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
    return {"status": "ok", "app": "pdf-reader"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    uvicorn.run(app, host="0.0.0.0", port=port)
