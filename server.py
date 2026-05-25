#!/usr/bin/env python3
"""語音書 PDF Reader — Google 強制登入 + Edge TTS (完全免費)"""
import os, io, uvicorn, secrets, asyncio
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

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

SECRET_KEY = os.environ.get("VOICEBOOK_SECRET", secrets.token_urlsafe(32))
serializer = URLSafeTimedSerializer(SECRET_KEY, salt="auth")
SESSION_MAX_AGE = 86400 * 30

# Google OAuth
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

user_sessions = {}

# ─── Edge TTS Voices ───
# 完全免費，無需 API Key
VOICE_ZH = "zh-TW-HsiaoChenNeural"   # 台灣口音女聲 (曉辰)
VOICE_EN = "en-US-JennyNeural"        # 美國女聲 (Jenny)

class TTSRequest(BaseModel):
    text: str
    lang: str = "zh"  # "zh" or "en"
    speed: float = 1.0

# ─── Auth ───
def get_current_user(request: Request):
    session_id = request.cookies.get("session")
    if not session_id:
        return None
    try:
        data = serializer.loads(session_id, max_age=SESSION_MAX_AGE)
        return user_sessions.get(data.get("sub", ""), None)
    except (BadSignature, SignatureExpired):
        return None

async def require_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "請先使用 Google 登入")
    return user

def make_session_cookie(user_info: dict) -> str:
    uid = user_info.get("sub", secrets.token_urlsafe(16))
    user_sessions[uid] = user_info
    return serializer.dumps({"sub": uid})

def clear_session(session_id: str):
    try:
        user_sessions.pop(serializer.loads(session_id).get("sub", ""), None)
    except: pass

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
    return {"status": "ok", "oauth": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)}

@app.get("/auth/login")
async def auth_login(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return HTMLResponse("<h2>❌ Google OAuth 尚未設定</h2>")
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = GOOGLE_REDIRECT_URI or f"{base_url}/auth/callback"
    state = secrets.token_urlsafe(32)
    url = (f"https://accounts.google.com/o/oauth2/v2/auth?"
           f"client_id={GOOGLE_CLIENT_ID}&redirect_uri={redirect_uri}"
           f"&response_type=code&scope=openid%20email%20profile&state={state}")
    response = RedirectResponse(url=url)
    response.set_cookie("oauth_state", state, max_age=300, httponly=True, samesite="lax", secure=True)
    return response

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h2>Login failed</h2><p>{error}</p>")
    if state != request.cookies.get("oauth_state", ""):
        return HTMLResponse("<h2>State mismatch</h2>")
    import httpx
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = GOOGLE_REDIRECT_URI or f"{base_url}/auth/callback"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            })
            if r.status_code != 200:
                return HTMLResponse(f"<h2>Token exchange failed</h2>")
            tokens = r.json()
            ur = await client.get("https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"})
            if ur.status_code != 200:
                return HTMLResponse(f"<h2>Failed to get user info</h2>")
            session = make_session_cookie(ur.json())
            resp = RedirectResponse(url="/")
            resp.set_cookie("session", session, max_age=SESSION_MAX_AGE,
                           httponly=True, samesite="lax", secure=True)
            resp.delete_cookie("oauth_state")
            return resp
    except Exception as e:
        return HTMLResponse(f"<h2>Auth error</h2><p>{str(e)}</p>")

@app.get("/auth/logout")
async def auth_logout(request: Request):
    clear_session(request.cookies.get("session", ""))
    resp = RedirectResponse(url="/")
    resp.delete_cookie("session")
    return resp

@app.get("/api/me")
async def api_me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False})
    return {"authenticated": True, "user": {
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "picture": user.get("picture", ""),
    }}

# ─── Edge TTS (完全免費) ───
@app.post("/api/tts")
async def text_to_speech(req: TTSRequest, user: dict = Depends(require_user)):
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
