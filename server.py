#!/usr/bin/env python3
"""語音書 PDF Reader — OpenAI TTS Proxy + Google OAuth"""
import os, json, httpx, uvicorn, secrets
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

app = FastAPI(title="語音書 PDF Reader")
STATIC_DIR = Path(__file__).parent
app.mount("/pdf", StaticFiles(directory=str(STATIC_DIR)), name="pdf")

# ─── Security Headers Middleware ───
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

SERVER_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SECRET_KEY = os.environ.get("VOICEBOOK_SECRET", secrets.token_urlsafe(32))
serializer = URLSafeTimedSerializer(SECRET_KEY, salt="auth")
SESSION_MAX_AGE = 86400 * 30  # 30 days

# Google OAuth config (set as env vars on Render)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# In-memory user session store (for simplicity; use DB for scale)
# On Render with a single instance, this works fine
user_sessions = {}  # session_id -> {name, email, picture, sub}

# ─── TTS ───
class TTSRequest(BaseModel):
    text: str
    voice: str = "nova"
    speed: float = 1.0
    model: str = "tts-1-hd"

# ─── Auth helpers ───
def get_current_user(request: Request):
    session_id = request.cookies.get("session")
    if not session_id:
        return None
    try:
        data = serializer.loads(session_id, max_age=SESSION_MAX_AGE)
        uid = data.get("sub", "")
        return user_sessions.get(uid, None)
    except (BadSignature, SignatureExpired):
        return None

def make_session_cookie(user_info: dict) -> str:
    uid = user_info.get("sub", secrets.token_urlsafe(16))
    user_sessions[uid] = user_info
    return serializer.dumps({"sub": uid})

def clear_session(session_id: str):
    try:
        data = serializer.loads(session_id)
        uid = data.get("sub", "")
        user_sessions.pop(uid, None)
    except:
        pass

# ─── Routes ───
@app.get("/", response_class=HTMLResponse)
async def index():
    path = STATIC_DIR / "index.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>語音書</h1><p>index.html 未找到</p>")

@app.get("/manifest.json")
async def manifest():
    path = STATIC_DIR / "manifest.json"
    if path.exists():
        return FileResponse(str(path), media_type="application/json")
    return JSONResponse({})

@app.get("/health")
async def health():
    return {"status": "ok", "app": "pdf-reader", "openai": bool(SERVER_API_KEY), "oauth": OAUTH_ENABLED}

@app.get("/api/config")
async def api_config():
    """Expose non-sensitive config to frontend."""
    return {
        "oauth_enabled": OAUTH_ENABLED,
        "google_client_id": GOOGLE_CLIENT_ID,
        "openai_configured": bool(SERVER_API_KEY),
    }

@app.get("/api/me")
async def api_me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False, "user": None})
    return {
        "authenticated": True,
        "user": {
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "picture": user.get("picture", ""),
            "sub": user.get("sub", ""),
        }
    }

@app.get("/auth/login")
async def auth_login(request: Request):
    if not OAUTH_ENABLED:
        return HTMLResponse("<h2>Google OAuth not configured.</h2><p>Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET env vars.</p>")
    
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/callback" if not GOOGLE_REDIRECT_URI else GOOGLE_REDIRECT_URI
    
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "state": secrets.token_urlsafe(32),
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + "&".join(f"{k}={v}" for k, v in params.items())
    
    response = RedirectResponse(url=url)
    response.set_cookie("oauth_state", params["state"], max_age=300, httponly=True, samesite="lax", secure=True)
    return response

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h2>Login failed</h2><p>{error}</p>")
    
    stored_state = request.cookies.get("oauth_state")
    if not state or (stored_state and state != stored_state):
        return HTMLResponse("<h2>State mismatch — possible CSRF attack</h2>")
    
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/callback" if not GOOGLE_REDIRECT_URI else GOOGLE_REDIRECT_URI
    
    # Exchange code for tokens
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if resp.status_code != 200:
                return HTMLResponse(f"<h2>Token exchange failed</h2><p>{resp.text}</p>")
            
            tokens = resp.json()
            id_token = tokens.get("id_token", "")
            access_token = tokens.get("access_token", "")
            
            # Get user info from Google
            user_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code != 200:
                return HTMLResponse(f"<h2>Failed to get user info</h2>")
            
            user_info = user_resp.json()
            session_cookie = make_session_cookie(user_info)
            
            # Redirect back to frontend with session
            response = RedirectResponse(url="/?login=success")
            response.set_cookie(
                "session", session_cookie,
                max_age=SESSION_MAX_AGE,
                httponly=True, samesite="lax",
                secure=True,
            )
            response.delete_cookie("oauth_state")
            return response
    except Exception as e:
        return HTMLResponse(f"<h2>Auth error</h2><p>{str(e)}</p>")

@app.get("/auth/logout")
async def auth_logout(request: Request):
    session_id = request.cookies.get("session", "")
    clear_session(session_id)
    response = RedirectResponse(url="/")
    response.delete_cookie("session")
    return response

@app.post("/api/tts")
async def text_to_speech(req: TTSRequest):
    api_key = SERVER_API_KEY
    if not api_key:
        raise HTTPException(400, "OpenAI API key not configured. Set OPENAI_API_KEY env var.")
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
