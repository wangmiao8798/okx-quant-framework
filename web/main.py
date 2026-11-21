"""OKX 量化交易系统 - Web管理后台入口"""
import os, sys, json
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

app = FastAPI(title="OKX Quant Web Panel", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8798", "http://localhost:8798"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# 静态文件
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

# 注册API路由
from routes import router as api_router
app.include_router(api_router)

# Token校验中间件
from auth import check_token, reset_request_token, set_request_token

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # 放行登录和静态文件
    if path.startswith("/static/") or path == "/api/auth/login" or path == "/api/auth/register" or path == "/api/app":
        response = await call_next(request)
        return response
    # 需要认证的API
    if path.startswith("/api/"):
        token = ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not check_token(token):
            return JSONResponse({"code": 401, "msg": "未登录或Token已失效"}, status_code=401)
        marker = set_request_token(token)
        try:
            return await call_next(request)
        finally:
            reset_request_token(marker)
    return await call_next(request)

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = Path(__file__).resolve().parent / "static" / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Web Panel</h1><p>index.html not found</p>")

@app.get("/health")
async def health():
    return {"status": "ok"}

def main():
    import uvicorn
    cfg_path = Path(__file__).resolve().parent / "web_config.json"
    cfg = {"server": {"host": "127.0.0.1", "port": 8798}}
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text()))
        except:
            pass
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = cfg.get("server", {}).get("port", 8798)
    print(f"[WebPanel] 启动管理后台 http://{host}:{port}")
    print(f"[WebPanel] 首次访问请设置后台密码")
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()
