import os, httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
VOICE = os.getenv("OPENAI_REALTIME_VOICE", "verse")

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "voice": VOICE}

@app.post("/session")
async def create_session():
    if not OPENAI_API_KEY:
        return JSONResponse({"error": "OPENAI_API_KEY not set"}, status_code=500)
    url = "https://api.openai.com/v1/realtime/sessions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "voice": VOICE,
        "modalities": ["audio", "text"],
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return JSONResponse({
        "client_secret": data.get("client_secret", {}),
        "model": MODEL,
        "voice": VOICE
    })
