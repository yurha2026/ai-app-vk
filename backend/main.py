from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import sqlite3
import requests
from dotenv import load_dotenv
import secrets
import string
from contextlib import contextmanager
import uvicorn
import uuid

load_dotenv()

app = FastAPI(title="AI Assistant Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE = "database.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def create_tables():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, vk_id INTEGER UNIQUE, email TEXT, name TEXT, 
        photo TEXT, balance REAL DEFAULT 0.0, credits INTEGER DEFAULT 0, 
        subscription_status TEXT DEFAULT 'free', referral_code TEXT, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_history (
        id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), role TEXT, 
        content TEXT, message_type TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id TEXT PRIMARY KEY, referrer_id TEXT REFERENCES users(id), referee_id TEXT REFERENCES users(id), 
        reward_amount REAL DEFAULT 0.0, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    conn.commit()
    conn.close()

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return """<html><head><title>AI App</title></head><body style="font-family:Arial;text-align:center;padding:50px;">
    <h1>🤖 Backend работает!</h1><p>База данных SQLite готова.</p></body></html>"""

@app.get("/health")
async def health():
    return {"status": "ok", "database": "sqlite"}

@app.get("/auth/vk/login")
async def vk_login_url():
    client_id = os.getenv("VK_CLIENT_ID", "54562656")
    callback = os.getenv("VK_CALLBACK_URL", "http://localhost:8000/auth/vk/callback")
    return {"login_url": f"https://oauth.vk.com/authorize?client_id={client_id}&redirect_uri={callback}&scope=vk_id&response_type=code"}

@app.get("/auth/vk/callback")
async def vk_callback(code: str):
    client_id = os.getenv("VK_CLIENT_ID", "54562656")
    client_secret = os.getenv("VK_CLIENT_SECRET", "TO2ZBwRkucVuTugyW2z8")
    callback = os.getenv("VK_CALLBACK_URL", "http://localhost:8000/auth/vk/callback")
    
    token_data = requests.post("https://oauth.vk.com/access_token", data={"grant_type":"authorization_code","client_id":client_id,"client_secret":client_secret,"redirect_uri":callback,"code":code}).json()
    
    if "access_token" not in token_data:
        raise HTTPException(status_code=400, detail=f"Ошибка токена: {token_data}")
    
    access_token = token_data["access_token"]
    user_id = int(token_data["user_id"])
    
    user_data = requests.get("https://graph.vk.com/me", params={"access_token": access_token, "fields": "first_name,last_name,photo_max"}, timeout=10).json()
    user_info = user_data.get("object", {})
    first_name = user_info.get('first_name', '')
    last_name = user_info.get('last_name', '')
    name = f"{first_name} {last_name}".strip() or "User"
    photo = user_info.get("photo_max", "")
    
    user_uuid = str(secrets.token_hex(16))
    
    with get_db() as conn:
        cursor = conn.cursor()
        existing = cursor.execute("SELECT * FROM users WHERE vk_id = ?", (user_id,)).fetchone()
        
        if existing:
            cursor.execute("UPDATE users SET updated_at = datetime('now'), photo = ? WHERE id = ?", (photo, existing['id']))
            user_db = dict(existing)
        else:
            referral_code = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
            cursor.execute("INSERT INTO users (id, vk_id, name, photo, referral_code, balance, credits) VALUES (?, ?, ?, ?, ?, 0, 0)", (user_uuid, user_id, name, photo, referral_code))
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_uuid,))
            user_db = dict(cursor.fetchone())
    
    conn.commit()
    session_token = f"{user_db['id']}_{secrets.token_hex(16)}"
    return {"success": True, "user": user_db, "token": session_token}

@app.post("/chat/send")
async def send_message(message: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    prompt = message.get("prompt", "").strip()
    msg_type = message.get("type", "text")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")
    
    ai_response = "Привет! Я готов помогать."
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO chat_history (user_id, role, content, message_type) VALUES (?, 'user', ?, ?)", user_id, prompt, msg_type)
        cursor.execute("INSERT INTO chat_history (user_id, role, content, message_type) VALUES (?, 'assistant', ?, ?)", user_id, ai_response, msg_type)
        conn.commit()
    
    return {"response": ai_response, "type": msg_type}

@app.get("/chat/history/{user_id}")
async def get_history(user_id: str):
    with get_db() as conn:
        cursor = conn.cursor()
        history = cursor.execute("SELECT * FROM chat_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 50", (user_id,)).fetchall()
    return {"history": [dict(msg) for msg in history]}

@app.post("/credits/deduct")
async def deduct_credits(data: dict, request: Request):
    user_id = request.headers.get("X-User-ID")
    amount = float(data.get("amount", 1))
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
        result = cursor.fetchone()
        
        if result and float(result[0]) >= amount:
            cursor.execute("UPDATE users SET credits = credits - ?, updated_at = datetime('now') WHERE id = ?", (amount, user_id))
            conn.commit()
            return {"success": True, "deducted": amount}
    
    return {"success": False, "error": "Недостаточно кредитов"}

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК СЕРВЕРА AI ASSISTANT PRO...")
    print("=" * 60)
    create_tables()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        input("\nНажми Enter для выхода...")