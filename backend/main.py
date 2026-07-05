from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
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
import json
import hashlib
import base64
import urllib.parse
import time
import hmac

load_dotenv()

app = FastAPI(title="AI Assistant Pro", version="2.1.0")

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://ai-app-vk.vercel.app")
BACKEND_URL = os.getenv("API_BASE", "https://neuro-guru-backend.onrender.com")

allowed_origins = [
    FRONTEND_URL,
    "https://vk.com",
    "https://id.vk.com",
    "https://neuro-guru-backend.onrender.com",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

DATABASE = "database.db"
JSONBIN_KEY = os.getenv("JSONBIN_KEY", "")
JSONBIN_ID = os.getenv("JSONBIN_ID", "")

pkce_store = {}
gigachat_token_cache = {"token": "", "expires": 0}
session_store = {}


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def create_tables():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                vk_id INTEGER UNIQUE,
                email TEXT,
                name TEXT,
                photo TEXT,
                balance REAL DEFAULT 0.0,
                credits INTEGER DEFAULT 3,
                subscription_status TEXT DEFAULT 'free',
                referral_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                role TEXT,
                content TEXT,
                message_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id TEXT PRIMARY KEY,
                referrer_id TEXT,
                referee_id TEXT,
                reward_amount REAL DEFAULT 0.0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def cloud_get_all():
    if not JSONBIN_KEY or not JSONBIN_ID:
        return {"users": {}}
    try:
        r = requests.get(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}/latest",
            headers={"X-Master-Key": JSONBIN_KEY},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            record = data.get("record")
            if isinstance(record, dict):
                if "users" not in record:
                    record = {"users": record} if record else {"users": {}}
                return record
            return {"users": {}}
    except Exception as e:
        print(f"[CLOUD] Ошибка чтения: {e}")
    return {"users": {}}


def cloud_save_all(data):
    if not JSONBIN_KEY or not JSONBIN_ID:
        return False
    try:
        r = requests.put(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}",
            headers={"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"},
            json=data,
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[CLOUD] Ошибка записи: {e}")
        return False


def cloud_save_user(user_data):
    try:
        data = cloud_get_all()
        vk_id = str(user_data.get("vk_id", ""))
        if not vk_id:
            return False
        data.setdefault("users", {})[vk_id] = user_data
        return cloud_save_all(data)
    except Exception:
        return False


def cloud_get_user(vk_id):
    try:
        data = cloud_get_all()
        return data.get("users", {}).get(str(vk_id))
    except Exception:
        return None


def cloud_update_credits(vk_id, credits):
    try:
        data = cloud_get_all()
        vk_id_str = str(vk_id)
        if vk_id_str in data.get("users", {}):
            data["users"][vk_id_str]["credits"] = int(credits)
            cloud_save_all(data)
            return True
    except Exception:
        return False


def generate_pkce():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return code_verifier, code_challenge


def create_session(user_id: str) -> str:
    token = f"{user_id}_{secrets.token_hex(32)}"
    session_store[token] = user_id
    return token


async def get_current_user(
    authorization: str = Header(default=None),
    x_user_id: str = Header(default=None),
) -> dict:
    user_id = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
        user_id = session_store.get(token)
    if not user_id and x_user_id:
        user_id = x_user_id
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Пользователь не найден")
        return dict(row)


def get_gigachat_token():
    now = time.time()
    if gigachat_token_cache["token"] and gigachat_token_cache["expires"] > now:
        return gigachat_token_cache["token"]
    auth_key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
    if not auth_key:
        return ""
    try:
        r = requests.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {auth_key}",
            },
            data={"scope": "GIGACHAT_API_PERS"},
            verify=False,
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            token = data.get("access_token", "")
            if token:
                gigachat_token_cache["token"] = token
                gigachat_token_cache["expires"] = now + 1800
                return token
    except Exception:
        pass
    return ""


def ask_gigachat(prompt: str, msg_type: str = "text") -> str:
    token = get_gigachat_token()
    if not token:
        return ""
    if msg_type == "code":
        system_msg = "Ты опытный программист. Пиши только чистый, рабочий код. Без объяснений."
    else:
        system_msg = "Ты полезный ИИ-ассистент. Отвечай подробно, по-русски."
    try:
        r = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "model": "GigaChat",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 2048,
            },
            verify=False,
            timeout=45,
        )
        if r.status_code == 200:
            data = r.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
    except Exception:
        pass
    return ""


def ask_huggingface(prompt: str, hf_token: str) -> str:
    if not hf_token:
        return ""
    models = ["Qwen/Qwen2.5-1.5B-Instruct", "HuggingFaceH4/zephyr-7b-beta"]
    for model in models:
        try:
            r = requests.post(
                f"https://api-inference.huggingface.co/models/{model}",
                headers={"Authorization": f"Bearer {hf_token}"},
                json={
                    "inputs": prompt,
                    "parameters": {"max_new_tokens": 512, "temperature": 0.7, "return_full_text": False},
                },
                timeout=40,
            )
            if r.status_code == 200:
                result = r.json()
                if isinstance(result, list) and result:
                    text = result[0].get("generated_text", "")
                    if text:
                        return text.strip()
        except Exception:
            continue
    return ""


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    code = request.query_params.get("code")
    device_id = request.query_params.get("device_id", "")
    state = request.query_params.get("state", "")
    if code:
        return await process_vk_auth(code, device_id, state)
    return """
    <html><head><meta charset="utf-8"><title>AI Assistant Pro</title>
    <style>body{font-family:Arial;text-align:center;padding:60px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}h1{font-size:3em;margin:0}</style>
    </head><body><h1>API работает</h1></body></html>
    """


@app.get("/health")
async def health():
    gc = "connected" if get_gigachat_token() else "not_connected"
    cloud = "connected" if (JSONBIN_KEY and JSONBIN_ID) else "not_configured"
    return {"status": "ok", "version": "2.1.0", "gigachat": gc, "cloud": cloud}


@app.get("/auth/vk/login")
async def vk_login_url():
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    callback = BACKEND_URL
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    pkce_store[state] = code_verifier
    login_url = (
        f"https://id.vk.com/authorize?response_type=code&client_id={client_id}&redirect_uri={urllib.parse.quote(callback)}&state={state}&code_challenge={code_challenge}&code_challenge_method=S256&scope=vkid.personal_info"
    )
    return {"success": True, "login_url": login_url}


async def process_vk_auth(code: str, device_id: str = "", state: str = ""):
    client_id = os.getenv("VK_CLIENT_ID", "54571690")
    client_secret = os.getenv("VK_CLIENT_SECRET", "AAHXNzlDsumtOLOfMnXt")
    callback = BACKEND_URL
    code_verifier = pkce_store.pop(state, secrets.token_urlsafe(64))
    try:
        token_resp = requests.post(
            "https://id.vk.com/oauth2/auth",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback,
                "code": code,
                "code_verifier": code_verifier,
                "device_id": device_id,
                "state": state,
            },
            timeout=15,
        )
        token_data = token_resp.json()
        if "access_token" not in token_data:
            return HTMLResponse(content=f"<html><body><h1>Ошибка</h1><p>{json.dumps(token_data, ensure_ascii=False)}</p></body></html>")
        access_token = token_data["access_token"]
        user_id_vk = int(token_data.get("user_id") or 0)
        user_resp = requests.post(
            "https://id.vk.com/oauth2/user_info",
            data={"access_token": access_token, "client_id": client_id},
            timeout=15,
        )
        user_data = user_resp.json()
        info = user_data.get("user", {}) or {}
        name = f"{info.get('first_name','')} {info.get('last_name','')}".strip() or "Пользователь"
        photo = info.get("avatar", "")
        if not user_id_vk:
            user_id_vk = int(info.get("user_id") or 0)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE vk_id = ?", (user_id_vk,))
            existing = cursor.fetchone()
            if existing:
                ex = dict(existing)
                cloud_user = cloud_get_user(user_id_vk)
                credits = ex.get("credits", 3)
                if cloud_user:
                    cc = int(cloud_user.get("credits", credits))
                    if cc != credits:
                        credits = cc
                cursor.execute(
                    "UPDATE users SET name=?, photo=?, credits=?, updated_at=datetime('now') WHERE id=?",
                    (name, photo, credits, ex["id"]),
                )
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE id=?", (ex["id"],))
                user_db = dict(cursor.fetchone())
                cloud_save_user(user_db)
            else:
                cloud_user = cloud_get_user(user_id_vk)
                if cloud_user:
                    uid = cloud_user.get("id") or str(secrets.token_hex(16))
                    rc = cloud_user.get("referral_code") or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                    cursor.execute(
                        "INSERT OR REPLACE INTO users (id,vk_id,name,photo,referral_code,balance,credits,created_at,updated_at) VALUES (?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM users WHERE id=?),datetime('now')),datetime('now'))",
                        (uid, user_id_vk, name, photo, rc, float(cloud_user.get("balance", 0)), int(cloud_user.get("credits", 3)), uid),
                    )
                    conn.commit()
                else:
                    uid = str(secrets.token_hex(16))
                    rc = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                    cursor.execute(
                        "INSERT INTO users (id,vk_id,name,photo,referral_code,balance,credits) VALUES (?,?,?,?,?,0,3)",
                        (uid, user_id_vk, name, photo, rc),
                    )
                    conn.commit()
                cursor.execute("SELECT * FROM users WHERE id=?", (uid,))
                user_db = dict(cursor.fetchone())
                cloud_save_user(user_db)
        token = create_session(user_db["id"])
        uenc = urllib.parse.quote(json.dumps(user_db, default=str, ensure_ascii=False))
        url = f"{FRONTEND_URL}?auth=success&token={token}&userData={uenc}"
        return HTMLResponse(content=f"<html><head><meta http-equiv='refresh' content='0;url={url}'></head><body><script>window.location.href='{url}';</script></body></html>")
    except Exception as e:
        return HTMLResponse(content=f"<html><body><h1>Ошибка</h1><p>{e}</p></body></html>")


@app.post("/chat/send")
async def send_message(message: dict, current_user: dict = Depends(get_current_user)):
    prompt = message.get("prompt", "").strip()
    msg_type = message.get("type", "text")
    if not prompt:
        raise HTTPException(status_code=400, detail="Пустой запрос")
    hf_token = os.getenv("HF_TOKEN", "")
    ai_response = ""
    rtype = msg_type
    if msg_type in ["text", "code"]:
        ai_response = ask_gigachat(prompt, msg_type)
        if not ai_response and hf_token:
            ai_response = ask_huggingface(prompt, hf_token)
        if not ai_response:
            ai_response = "Модели загружаются. Попробуйте позже."
        rtype = "text"
    elif msg_type == "image":
        gc = get_gigachat_token()
        if gc:
            try:
                r = requests.post(
                    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                    headers={"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {gc}"},
                    json={"model": "GigaChat", "messages": [{"role": "user", "content": f"Создай изображение: {prompt}"}], "temperature": 0.7},
                    verify=False,
                    timeout=90,
                )
                if r.status_code == 200:
                    d = r.json()
                    ch = d.get("choices", [])
                    if ch:
                        content = ch[0].get("message", {}).get("content", "")
                        import re

                        fids = re.findall(r'src="([^"]+)"', content)
                        if fids:
                            fid = fids[0]
                            ir = requests.get(
                                f"https://gigachat.devices.sberbank.ru/api/v1/files/{fid}/content",
                                headers={"Authorization": f"Bearer {gc}"},
                                verify=False,
                                timeout=60,
                            )
                            if ir.status_code == 200 and len(ir.content) > 100:
                                b64 = base64.b64encode(ir.content).decode("utf-8")
                                ct = ir.headers.get("content-type", "image/png")
                                ai_response = f"data:{ct};base64,{b64}"
                                rtype = "image"
                            else:
                                ai_response, rtype = content, "text"
                        else:
                            ai_response, rtype = content, "text"
                    else:
                        ai_response, rtype = "Не удалось", "text"
                else:
                    ai_response, rtype = "Недоступно", "text"
            except Exception:
                ai_response, rtype = "Ошибка", "text"
        else:
            ai_response, rtype = "Загружается", "text"
    else:
        ai_response, rtype = "Неизвестный тип", "text"
    uid = current_user["id"]
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO chat_history (id,user_id,role,content,message_type) VALUES (?,?,?,?,?)", (str(uuid.uuid4()), uid, "user", prompt, msg_type))
        c.execute("INSERT INTO chat_history (id,user_id,role,content,message_type) VALUES (?,?,?,?,?)", (str(uuid.uuid4()), uid, "assistant", ai_response, rtype))
        conn.commit()
    return {"success": True, "response": ai_response, "type": rtype}


@app.get("/chat/history")
async def get_history(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM chat_history WHERE user_id=? ORDER BY created_at ASC LIMIT 100", (uid,))
        rows = [dict(r) for r in c.fetchall()]
    return {"success": True, "history": rows}


@app.post("/credits/deduct")
async def deduct_credits(data: dict, current_user: dict = Depends(get_current_user)):
    amount = int(data.get("amount", 1))
    if amount <= 0:
        amount = 1
    uid, vk = current_user["id"], current_user.get("vk_id")
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT credits FROM users WHERE id=?", (uid,))
        r = c.fetchone()
        if not r or int(r["credits"]) < amount:
            return {"success": False, "error": "Недостаточно кредитов"}
        nc = int(r["credits"]) - amount
        c.execute("UPDATE users SET credits=?, updated_at=datetime('now') WHERE id=?", (nc, uid))
        conn.commit()
    if vk:
        cloud_update_credits(vk, nc)
    return {"success": True, "deducted": amount, "credits": nc}


@app.post("/credits/add")
async def add_credits(data: dict, current_user: dict = Depends(get_current_user)):
    amount = int(data.get("amount", 0))
    if amount <= 0:
        return {"success": False}
    uid, vk = current_user["id"], current_user.get("vk_id")
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET credits=credits+?, updated_at=datetime('now') WHERE id=?", (amount, uid))
        conn.commit()
        c.execute("SELECT credits FROM users WHERE id=?", (uid,))
        r = c.fetchone()
        nc = int(r["credits"]) if r else 0
    if vk:
        cloud_update_credits(vk, nc)
    return {"success": True, "added": amount, "credits": nc}


@app.get("/credits/check")
async def check_credits(user_id: str = None, current_user: dict = Depends(get_current_user)):
    uid = user_id or current_user["id"]
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT credits,balance,vk_id FROM users WHERE id=?", (uid,))
        r = c.fetchone()
        if not r:
            return {"success": False}
        credits, balance, vk = int(r["credits"]), float(r["balance"]), r["vk_id"]
    cu = cloud_get_user(vk) if vk else None
    if cu:
        cc = int(cu.get("credits", credits))
        if cc != credits:
            credits = cc
            with get_db() as conn:
                c = conn.cursor()
                c.execute("UPDATE users SET credits=?, updated_at=datetime('now') WHERE id=?", (credits, uid))
                conn.commit()
    return {"success": True, "credits": credits, "balance": balance}


@app.post("/payment/create")
async def create_payment(payment_data: dict, current_user: dict = Depends(get_current_user)):
    package = payment_data.get("package")
    amount = float(payment_data.get("amount", 0))
    shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    secret_key = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    if not shop_id or not secret_key:
        return {"success": False, "error": "Платёжная система недоступна"}
    packages = {
        "starter": {"credits": 150, "desc": "Пакет Starter"},
        "professional": {"credits": 450, "desc": "Пакет Professional"},
        "business": {"credits": 1100, "desc": "Пакет Business"},
        "unlimited": {"credits": 3500, "desc": "Пакет Unlimited"},
    }
    pkg = packages.get(package)
    if not pkg:
        return {"success": False, "error": "Неизвестный пакет"}
    try:
        auth_str = f"{shop_id}:{secret_key}"
        auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
        payload = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": f"{FRONTEND_URL}?payment=success&pkg={package}"},
            "capture": True,
            "description": pkg["desc"],
            "metadata": {
                "user_id": current_user["id"],
                "vk_id": current_user.get("vk_id"),
                "package": package,
                "credits": pkg["credits"],
            },
        }
        r = requests.post(
            "https://api.yookassa.ru/v3/payments",
            json=payload,
            headers={
                "Authorization": f"Basic {auth_b64}",
                "Content-Type": "application/json",
                "Idempotence-Key": str(uuid.uuid4()),
            },
            timeout=20,
        )
        if r.status_code in (200, 201):
            data = r.json()
            conf = data.get("confirmation", {}) or {}
            url = conf.get("confirmation_url") or conf.get("url")
            return {"success": True, "payment_id": data.get("id"), "confirmation_url": url}
        else:
            print(f"[PAYMENT] Ошибка: {r.status_code} {r.text}")
            return {"success": False, "error": "Не удалось создать платёж"}
    except Exception as e:
        print(f"[PAYMENT] Исключение: {e}")
        return {"success": False, "error": "Ошибка создания платежа"}


@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    try:
        body = await request.body()
        body_str = body.decode("utf-8")
        try:
            data = json.loads(body_str)
        except:
            return {"status": "ok"}
        if data.get("event") == "payment.succeeded":
            obj = data.get("object", {}) or {}
            meta = obj.get("metadata", {}) or {}
            uid = meta.get("user_id")
            vk = meta.get("vk_id")
            cr = int(meta.get("credits", 0))
            if cr > 0 and uid:
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute("UPDATE users SET credits=credits+?, updated_at=datetime('now') WHERE id=?", (cr, uid))
                    conn.commit()
                    if vk:
                        c.execute("SELECT credits FROM users WHERE id=?", (uid,))
                        rr = c.fetchone()
                        if rr:
                            cloud_update_credits(vk, int(rr["credits"]))
        return {"status": "ok"}
    except Exception:
        return {"status": "ok"}


@app.get("/user/me")
async def me(current_user: dict = Depends(get_current_user)):
    return {"success": True, "user": current_user}


@app.post("/auth/logout")
async def logout(authorization: str = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        t = authorization.replace("Bearer ", "").strip()
        session_store.pop(t, None)
    return {"success": True}


if __name__ == "__main__":
    print("=" * 60)
    print("AI ASSISTANT PRO v2.1.0")
    print("=" * 60)
    create_tables()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")