import sqlite3
import os
import requests
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "records.db"
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "dev-token")
BARK_KEY = os.environ.get("BARK_KEY", "")

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, app_name TEXT NOT NULL, event TEXT NOT NULL, timestamp TEXT NOT NULL)")
    conn.commit()
    conn.close()

init_db()

app = FastAPI(title="查岗系统")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ReportBody(BaseModel):
    app_name: str
    event: str

@app.post("/report")
async def report(body: ReportBody, req: Request):
    auth = req.headers.get("Authorization", "")
    if auth != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "Unauthorized")
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    
    if body.event == "open":
        cur = conn.cursor()
        cur.execute("SELECT id, app_name FROM records WHERE event='open' AND app_name=? AND id NOT IN (SELECT o.id FROM records o JOIN records c ON o.app_name=c.app_name AND o.id<c.id WHERE o.event='open' AND c.event='close')", (body.app_name,))
        for old in cur.fetchall():
            conn.execute("INSERT INTO records (app_name, event, timestamp) VALUES (?,?,?)", (old[1], "close", now))
    
    conn.execute("INSERT INTO records (app_name, event, timestamp) VALUES (?,?,?)", (body.app_name, body.event, now))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/ping")
async def ping():
    return "pong"

@app.get("/activity/summary")
async def summary():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT app_name, event, timestamp FROM records ORDER BY id DESC LIMIT 5")
    recent = cur.fetchall()
    cur.execute("SELECT app_name, event, timestamp FROM records ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    sessions, opens = {}, {}
    for r in rows:
        app, ev, ts = r
        if ev == "open":
            opens[app] = datetime.fromisoformat(ts)
        elif ev == "close" and app in opens:
            sessions[app] = sessions.get(app, 0) + int((datetime.fromisoformat(ts) - opens[app]).total_seconds())
            del opens[app]
    return {"recent_apps": [r[0] for r in recent], "sessions": sessions}

def check_on_wife(limit=10):
    try:
        r = requests.get(f"http://127.0.0.1:{os.environ.get('PORT', 8080)}/activity/summary", timeout=10)
        data = r.json()
    except Exception as e:
        return f"查岗失败: {e}"
    
    apps = data.get("recent_apps", [])
    ses = data.get("sessions", {})
    lines = []
    lines.append(f"最近打开: {', '.join(apps)}" if apps else "暂无记录")
    if ses:
        for app, secs in sorted(ses.items(), key=lambda x: x[1], reverse=True):
            m, s = divmod(secs, 60)
            lines.append(f"{app}: {m}分{s}秒")
    return "\n".join(lines)

def bark_alert(title="查岗提醒", content=""):
    if not content:
        return "内容不能为空"
    if not BARK_KEY:
        return "BARK_KEY未配置"
    url = f"https://api.day.app/{BARK_KEY}/{title}/{content}"
    try:
        r = requests.get(url, timeout=10)
        return "推送成功" if r.status_code == 200 else f"推送失败: {r.status_code}"
    except Exception as e:
        return f"推送异常: {e}"

TOOLS = [
    {"name": "check_on_wife", "description": "查岗手机活动，获取最近使用的App和使用时长", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "bark_alert", "description": "给手机发Bark推送弹窗", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}}, "required": ["content"]}}
]

FUNCS = {"check_on_wife": check_on_wife, "bark_alert": bark_alert}

@app.post("/mcp")
async def mcp(req: Request):
    body = await req.json()
    method = body.get("method")
    params = body.get("params") or {}
    rid = body.get("id")
    
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "查岗MCP", "version": "1.0"}}}
    
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in FUNCS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "未知工具"}}
        result = FUNCS[name](**args)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(result)}]}}
    
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未知方法: {method}"}}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
