import os, json, hmac, hashlib, asyncio, subprocess, time, secrets, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from asyncio import Queue as AsyncIOQueue

# ── Load .env if exists ──
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().strip().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import httpx
import jwt as pyjwt
from fastapi import FastAPI, Request, HTTPException, Response, Form, Cookie
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON, select, func, delete
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ── Config ──
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(exist_ok=True)

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_hex(16))
HOST_URL = os.environ.get("HOST_URL", "http://localhost:8010")
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")
BUILD_DIR = os.environ.get("BUILD_DIR", "/tmp/ci-builds")
os.makedirs(BUILD_DIR, exist_ok=True)

# ── DB ──
engine = create_engine(f"sqlite:///{DATA_DIR}/ci.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class WhConfig(Base):
    __tablename__ = "wh_configs"
    id = Column(Integer, primary_key=True)
    repo = Column(String(200), unique=True)
    access_token = Column(String(200))
    webhook_id = Column(Integer, nullable=True)
    watched_branch = Column(String(100), default="main")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class Build(Base):
    __tablename__ = "builds"
    id = Column(Integer, primary_key=True)
    repo = Column(String(200))
    branch = Column(String(100))
    commit_sha = Column(String(40))
    commit_msg = Column(String(500))
    author = Column(String(100))
    status = Column(String(20), default="pending")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    log = Column(Text, default="")
    triggered_by = Column(String(100), default="webhook")

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def get_settings() -> dict:
    """Load all settings from DB + env overrides."""
    result = {}
    with db() as session:
        rows = session.execute(select(Setting)).scalars().all()
        for r in rows:
            result[r.key] = r.value
    # Env vars override DB settings
    env_overrides = {
        "github_client_id": os.environ.get("GITHUB_CLIENT_ID"),
        "github_client_secret": os.environ.get("GITHUB_CLIENT_SECRET"),
        "host_url": os.environ.get("HOST_URL"),
        "jwt_secret": os.environ.get("JWT_SECRET"),
        "webhook_secret": os.environ.get("WEBHOOK_SECRET"),
        "allowed_users": os.environ.get("ALLOWED_USERS"),
        "build_dir": os.environ.get("BUILD_DIR"),
    }
    for k, v in env_overrides.items():
        if v:
            result[k] = v
    return result


def reload_settings():
    """Reload global config from DB settings."""
    global GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, HOST_URL, JWT_SECRET, WEBHOOK_SECRET, ALLOWED_USERS, BUILD_DIR
    s = get_settings()
    GITHUB_CLIENT_ID = s.get("github_client_id", GITHUB_CLIENT_ID)
    GITHUB_CLIENT_SECRET = s.get("github_client_secret", GITHUB_CLIENT_SECRET)
    HOST_URL = s.get("host_url", HOST_URL)
    JWT_SECRET = s.get("jwt_secret", JWT_SECRET) if s.get("jwt_secret") else JWT_SECRET
    WEBHOOK_SECRET = s.get("webhook_secret", WEBHOOK_SECRET) if s.get("webhook_secret") else WEBHOOK_SECRET
    ALLOWED_USERS = s.get("allowed_users", ALLOWED_USERS)
    BUILD_DIR = s.get("build_dir", BUILD_DIR)
    os.makedirs(BUILD_DIR, exist_ok=True)

Base.metadata.create_all(engine)

def db():
    return SessionLocal()

# ── FastAPI ──
app = FastAPI(title="CI Watcher")

# ── SSE subscribers ──
_sse_subscribers: list[AsyncIOQueue] = []
_sse_lock = asyncio.Lock()

async def sse_broadcast(event: str, data: dict):
    """Push event to all connected SSE clients."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\nretry: 3000\n\n"
    async with _sse_lock:
        dead = []
        for queue in list(_sse_subscribers):
            try:
                await queue.put(msg)
            except Exception:
                dead.append(queue)
        for q in dead:
            _sse_subscribers.remove(q)

@app.on_event("startup")
async def startup():
    # Migrate DB: add watched_branch if missing
    with db() as session:
        try:
            session.execute(text("SELECT watched_branch FROM wh_configs LIMIT 0"))
        except Exception:
            session.execute(text("ALTER TABLE wh_configs ADD COLUMN watched_branch VARCHAR(100) DEFAULT 'main'"))
            session.commit()
    reload_settings()
    print(f"[CI Watcher] Settings loaded. Host: {HOST_URL}")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ── Auth helpers ──
def create_token(user: dict) -> str:
    return pyjwt.encode({
        **user,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }, JWT_SECRET, algorithm="HS256")

def get_user_from_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except:
        return None

def require_user(request: Request) -> dict:
    token = request.cookies.get("session")
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=302, detail="Redirecting to login")
    return user

# ── GitHub OAuth ──
@app.get("/auth/login")
async def login_page(request: Request):
    """Show login page with GitHub button."""
    token = request.cookies.get("session")
    user = get_user_from_token(token)
    if user:
        return RedirectResponse(url="/")
    return templates.TemplateResponse("login.html", {"request": request, "host_url": HOST_URL})

@app.get("/auth/github")
async def login_redirect():
    return RedirectResponse(
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={HOST_URL}/auth/callback"
        f"&scope=repo,user:email"
    )

@app.get("/auth/callback")
async def auth_callback(code: str, response: Response):
    async with httpx.AsyncClient() as client:
        tr = await client.post("https://github.com/login/oauth/access_token",
            data={"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET, "code": code},
            headers={"Accept": "application/json"})
        td = tr.json()
        at = td.get("access_token")
        if not at:
            return HTMLResponse("OAuth failed", status_code=400)
        ur = await client.get("https://api.github.com/user",
            headers={"Authorization": f"Bearer {at}"})
        user = ur.json()

    gh_username = user.get("login", "")
    allowed = [u.strip() for u in ALLOWED_USERS.split(",") if u.strip()]
    if allowed and gh_username not in allowed:
        return HTMLResponse(f"User '{gh_username}' not allowed", status_code=403)

    session_token = create_token({
        "id": user["id"], "login": gh_username,
        "avatar": user.get("avatar_url", ""), "name": user.get("name", gh_username),
        "access_token": at,
    })
    resp = RedirectResponse(url="/")
    resp.set_cookie("session", session_token, httponly=True, max_age=604800, samesite="lax", secure=HOST_URL.startswith("https://"))
    return resp

@app.get("/auth/logout")
async def logout(response: Response):
    resp = RedirectResponse(url="/")
    resp.delete_cookie("session")
    return resp

@app.get("/auth/me")
async def auth_me(request: Request):
    token = request.cookies.get("session")
    user = get_user_from_token(token)
    if not user:
        return {"user": None}
    return {"user": {"login": user["login"], "avatar": user.get("avatar", ""), "name": user.get("name", "")}}

# ── GitHub API helpers ──
async def gh_api(user: dict, method: str, path: str, data: dict = None):
    async with httpx.AsyncClient() as c:
        kwargs = {"headers": {"Authorization": f"Bearer {user['access_token']}"}}
        if data is not None:
            kwargs["json"] = data
        r = await c.request(method, f"https://api.github.com{path}", **kwargs)
        if r.status_code >= 400:
            return None
        return r.json() if r.text else {}

# ── Webhook ──
async def run_docker_build(build_id: int, repo: str, branch: str, commit_sha: str, user_token: str):
    with db() as session:
        build = session.get(Build, build_id)
        if not build: return
        build.status = "running"
        build.started_at = datetime.now(timezone.utc)
        session.commit()

    # Broadcast start
    asyncio.create_task(sse_broadcast("build_started", {
        "id": build_id,
        "repo": repo,
        "status": "running",
    }))

    repo_short = repo.split("/")[-1].replace(".git", "")
    workdir = os.path.join(BUILD_DIR, repo_short)
    full_log = ""

    def wlog(msg: str):
        nonlocal full_log
        ts = datetime.now().strftime("%H:%M:%S")
        full_log += f"[{ts}] {msg}\n"
        with db() as s:
            b = s.get(Build, build_id)
            if b: b.log = full_log; s.commit()

    try:
        repo_url = f"https://x-access-token:{user_token}@github.com/{repo}.git"
        wlog(f"📦 Cloning {repo} @ {branch}")

        if os.path.exists(workdir):
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", workdir, "fetch", "--depth", "1", "origin", branch or "main",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        else:
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", "--branch", branch or "main", repo_url, workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})

        out, _ = await proc.communicate()
        if out: wlog(out.decode().strip())
        if proc.returncode != 0:
            wlog(f"❌ Git failed (rc={proc.returncode})"); status = "failed"
            raise Exception("git_error")

        if commit_sha not in ("", "0000000000000000000000000000000000000000", "manual"):
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", workdir, "checkout", commit_sha,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            if out: wlog(out.decode().strip())
        elif commit_sha in ("manual", "") and branch:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", workdir, "checkout", f"origin/{branch}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            if out: wlog(out.decode().strip())

        # Find build file — priority: docker-compose > Dockerfile
        import glob
        compose_candidates = [
            os.path.join(workdir, "infra", "docker-compose.yml"),
            os.path.join(workdir, "infra", "docker-compose.yaml"),
            os.path.join(workdir, "docker-compose.yml"),
            os.path.join(workdir, "docker-compose.yaml"),
        ]
        compose_file = ""
        for c in compose_candidates:
            if os.path.exists(c):
                compose_file = c
                break

        if compose_file:
            wlog(f"🐳 Building all services via docker compose")
            wlog(f"   Compose file: {compose_file}")
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "-f", compose_file, "build",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=workdir)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                if out:
                    wlog(out.decode().strip())
                if proc.returncode == 0:
                    wlog(f"✅ Build SUCCESS (docker compose)")
                    status = "success"
                else:
                    wlog(f"❌ Build FAILED (rc={proc.returncode})")
                    status = "failed"
            except asyncio.TimeoutError:
                proc.kill()
                wlog("❌ Build timed out after 600s")
                status = "failed"
        else:
            # Fallback: single Dockerfile
            p = ""
            for candidate in [
                os.path.join(workdir, "Dockerfile"),
                os.path.join(workdir, "apps", "frontend", "Dockerfile"),
            ]:
                if os.path.exists(candidate):
                    p = candidate
                    break
            if not p:
                found = sorted(glob.glob(os.path.join(workdir, "**", "Dockerfile"), recursive=True),
                              key=lambda x: len(x))
                if found:
                    p = found[0]

            if not os.path.exists(p):
                wlog("⚠️ No Dockerfile or docker-compose.yml found — skipping docker build")
                wlog("✅ Build recorded (no Docker image)")
                status = "success"
            else:
                docker_tag = f"ci-{repo_short.lower()}:{commit_sha[:7] or 'latest'}"
                ctx = os.path.dirname(p)
                wlog(f"🐳 Building Docker image: {docker_tag}")
                wlog(f"   Dockerfile: {p}")

                proc = await asyncio.create_subprocess_exec(
                    "docker", "build", "-f", p, "-t", docker_tag, ctx,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                    if out:
                        wlog(out.decode().strip())
                    if proc.returncode == 0:
                        wlog(f"✅ Build SUCCESS → {docker_tag}")
                        status = "success"
                    else:
                        wlog(f"❌ Build FAILED (rc={proc.returncode})")
                        status = "failed"
                except asyncio.TimeoutError:
                    proc.kill()
                    wlog("❌ Build timed out after 600s")
                    status = "failed"

    except Exception as e:
        if str(e) != "git_error":
            wlog(f"❌ Error: {e}")
            status = "failed"

    finished_at = datetime.now(timezone.utc)
    with db() as session:
        build = session.get(Build, build_id)
        if build:
            build.status = status
            build.finished_at = finished_at
            build.log = full_log
            session.commit()

    await sse_broadcast("build_updated", {
        "id": build_id,
        "repo": repo,
        "status": status,
        "finished_at": finished_at.isoformat(),
    })

# ── Webhook endpoints ──
@app.get("/api/events")
async def sse_events(request: Request):
    """Server-Sent Events endpoint for real-time build updates."""
    queue: AsyncIOQueue = AsyncIOQueue()
    _sse_subscribers.append(queue)

    async def event_stream():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception:
            pass
        finally:
            async with _sse_lock:
                if queue in _sse_subscribers:
                    _sse_subscribers.remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/api/webhook")
async def webhook_receiver(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event", "")

    if WEBHOOK_SECRET:
        raw = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        expected = "sha256=" + raw
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(401, "Invalid signature")

    if event == "ping":
        return {"msg": "pong"}
    if event != "push":
        return {"msg": f"ignored: {event}"}

    data = json.loads(body)
    repo_full = data.get("repository", {}).get("full_name", "")
    branch = data.get("ref", "").replace("refs/heads/", "")
    commit_sha = data.get("after", "")
    hc = data.get("head_commit") or {}
    commit_msg = hc.get("message", "")
    author = hc.get("committer", {}).get("name", "") or hc.get("author", {}).get("name", "")

    if not repo_full:
        return {"error": "no repo"}

    # Check watched branch filter
    with db() as session:
        config = session.execute(select(WhConfig).where(WhConfig.repo == repo_full)).scalar_one_or_none()

    watched = (config.watched_branch or "main") if config else "main"
    if branch != watched:
        return {"msg": f"ignored: branch '{branch}' != watched '{watched}'"}

    with db() as session:
        build = Build(repo=repo_full, branch=branch, commit_sha=commit_sha[:40],
                       commit_msg=commit_msg[:500], author=author[:100], status="pending")
        session.add(build)
        session.commit()
        build_id = build.id

    # Get token
    user_token = ""
    with db() as session:
        config = session.execute(select(WhConfig).where(WhConfig.repo == repo_full)).scalar_one_or_none()
        if config:
            user_token = config.access_token

    if not user_token:
        with db() as session:
            b = session.get(Build, build_id)
            if b: b.status = "failed"; b.log = "No access token configured"; b.finished_at = datetime.now(timezone.utc); session.commit()
        return {"error": "no token"}

    asyncio.create_task(run_docker_build(build_id, repo_full, branch, commit_sha[:40], user_token))

    # Broadcast via SSE
    await sse_broadcast("build_created", {
        "id": build_id, "repo": repo_full, "branch": branch,
        "commit_sha": commit_sha[:40], "status": "pending",
    })

    return {"build_id": build_id, "status": "queued"}

# ── UI: Auth middleware ──
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = ["/auth/login", "/auth/github", "/auth/callback", "/auth/me", "/static/", "/api/webhook", "/healthz", "/api/events"]
    if any(path.startswith(p) for p in public_paths):
        return await call_next(request)
    token = request.cookies.get("session")
    user = get_user_from_token(token)
    if not user:
        if path.startswith("/api/") and not path.startswith("/api/webhook"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/auth/login")
    return await call_next(request)

app.middleware("http")(auth_middleware)

# ── UI Pages ──
def _user_context(request: Request):
    token = request.cookies.get("session")
    return get_user_from_token(token) or {}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _user_context(request)
    with db() as session:
        rows = session.execute(
            select(Build).order_by(Build.id.desc()).limit(200)
        ).scalars().all()
        repos = session.execute(select(WhConfig)).scalars().all()

    configured_repos = {r.repo for r in repos}

    from collections import OrderedDict
    grouped = OrderedDict()
    for b in rows:
        repo = b.repo
        if repo not in grouped:
            grouped[repo] = []
        grouped[repo].append(b)

    total = len(rows)
    success = sum(1 for b in rows if b.status == 'success')
    failed = sum(1 for b in rows if b.status == 'failed')
    running = sum(1 for b in rows if b.status == 'running')

    return templates.TemplateResponse("index.html", {
        "request": request, "user": user,
        "grouped": grouped, "total_builds": total,
        "success_count": success, "failed_count": failed, "running_count": running,
        "configured_repos": configured_repos,
    })

@app.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    user = _user_context(request)
    repos_data = await gh_api(user, "GET", "/user/repos?sort=updated&per_page=100&affiliation=owner,organization_member")
    repos_list = repos_data or []

    with db() as session:
        configured = session.execute(select(WhConfig)).scalars().all()
    configured_repos = {r.repo for r in configured}
    wh_configs = {r.repo: r for r in configured}

    return templates.TemplateResponse("repos.html", {
        "request": request, "user": user, "repos": repos_list,
        "configured_repos": configured_repos, "wh_configs": wh_configs,
    })

@app.post("/repos/activate")
async def activate_webhook(request: Request, repo: str = Form(...), branch: str = Form("main")):
    user = _user_context(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    webhook_url = f"{HOST_URL}/api/webhook"
    created_id = None

    async with httpx.AsyncClient() as client:
        # Check existing
        hooks = await client.get(f"https://api.github.com/repos/{repo}/hooks",
            headers={"Authorization": f"Bearer {user['access_token']}"})
        for h in (hooks.json() or []):
            if h.get("config", {}).get("url") == webhook_url:
                created_id = h["id"]
                break

        if not created_id:
            resp = await client.post(f"https://api.github.com/repos/{repo}/hooks",
                headers={"Authorization": f"Bearer {user['access_token']}"},
                json={"name": "web", "active": True, "events": ["push"],
                      "config": {"url": webhook_url, "content_type": "json", "secret": WEBHOOK_SECRET}})
            if resp.status_code in (200, 201):
                created_id = resp.json().get("id")

    with db() as session:
        existing = session.execute(select(WhConfig).where(WhConfig.repo == repo)).scalar_one_or_none()
        if existing:
            existing.access_token = user["access_token"]
            existing.watched_branch = branch
            if created_id: existing.webhook_id = created_id
        else:
            session.add(WhConfig(repo=repo, access_token=user["access_token"], webhook_id=created_id, watched_branch=branch))
        session.commit()

    return RedirectResponse(url="/repos", status_code=302)

@app.post("/repos/deactivate")
async def deactivate_webhook(request: Request, repo: str = Form(...)):
    user = _user_context(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    with db() as session:
        config = session.execute(select(WhConfig).where(WhConfig.repo == repo)).scalar_one_or_none()
        wh_id = config.webhook_id if config else None
        session.execute(delete(WhConfig).where(WhConfig.repo == repo))
        session.commit()

    if wh_id:
        async with httpx.AsyncClient() as client:
            await client.delete(f"https://api.github.com/repos/{repo}/hooks/{wh_id}",
                headers={"Authorization": f"Bearer {user['access_token']}"})

    return RedirectResponse(url="/repos", status_code=302)

@app.get("/builds/{build_id}", response_class=HTMLResponse)
async def build_detail(request: Request, build_id: int):
    user = _user_context(request)
    with db() as session:
        build = session.get(Build, build_id)
    if not build:
        return templates.TemplateResponse("error.html", {"request": request, "user": user, "code": 404, "msg": "Build not found"})
    return templates.TemplateResponse("build.html", {"request": request, "user": user, "build": build})

@app.get("/api/builds/{build_id}/log")
async def build_log_api(build_id: int):
    with db() as session:
        build = session.get(Build, build_id)
    if not build:
        raise HTTPException(404)
    return Response(content=build.log or "", media_type="text/plain")

@app.get("/api/builds/{build_id}/raw")
async def build_raw(build_id: int):
    with db() as session:
        build = session.get(Build, build_id)
    if not build:
        raise HTTPException(404)
    return JSONResponse({
        "id": build.id, "repo": build.repo, "branch": build.branch,
        "commit_sha": build.commit_sha, "commit_msg": build.commit_msg,
        "author": build.author, "status": build.status,
        "started_at": build.started_at.isoformat() if build.started_at else None,
        "finished_at": build.finished_at.isoformat() if build.finished_at else None,
        "log": build.log,
    })

@app.get("/api/stats")
async def api_stats():
    with db() as session:
        total = session.execute(select(func.count(Build.id))).scalar() or 0
        success = session.execute(select(func.count(Build.id)).where(Build.status == "success")).scalar() or 0
        failed = session.execute(select(func.count(Build.id)).where(Build.status == "failed")).scalar() or 0
        running = session.execute(select(func.count(Build.id)).where(Build.status == "running")).scalar() or 0
        repos = session.execute(select(func.count(func.distinct(Build.repo)))).scalar() or 0
    return {"total": total, "success": success, "failed": failed, "running": running, "repos": repos}


# ── Project page ──
@app.get("/projects/{repo:path}", response_class=HTMLResponse)
async def project_page(request: Request, repo: str):
    user = _user_context(request)
    with db() as session:
        builds = session.execute(
            select(Build).where(Build.repo == repo).order_by(Build.id.desc()).limit(50)
        ).scalars().all()
        config = session.execute(select(WhConfig).where(WhConfig.repo == repo)).scalar_one_or_none()

    if not builds and not config:
        return templates.TemplateResponse("error.html", {"request": request, "user": user, "code": 404, "msg": "Project not found"})

    is_active = config is not None
    total = len(builds)
    running_builds = [b for b in builds if b.status == 'running']

    return templates.TemplateResponse("project.html", {
        "request": request, "user": user, "repo": repo,
        "builds": builds, "total": total,
        "is_active": is_active, "running_builds": running_builds,
    })


@app.post("/api/builds/{build_id}/cancel")
async def cancel_build(build_id: int):
    """Cancel a running build by killing its process."""
    with db() as session:
        build = session.get(Build, build_id)
        if not build:
            raise HTTPException(404, "Build not found")
        if build.status not in ("running", "pending"):
            return {"status": "error", "msg": f"Build ya está {build.status} (no se puede cancelar)"}
        repo = build.repo
        sha = build.commit_sha[:7]
        finished = datetime.now(timezone.utc)
        build.status = "failed"
        build.finished_at = finished
        build.log += f"\n[{finished.strftime('%H:%M:%S')}] 🛑 Build cancelled by user\n"
        session.commit()

    # Kill the docker build process
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-f", f"ci-.*:{sha}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()
    except:
        pass

    await sse_broadcast("build_updated", {
        "id": build_id, "repo": repo,
        "status": "failed", "finished_at": finished.isoformat(),
    })
    return {"status": "cancelled"}


@app.post("/api/builds/trigger")
async def trigger_build(request: Request, repo: str = Form(...), branch: str = Form("main"),
                         commit_sha: str = Form(""), commit_msg: str = Form("Manual trigger")):
    """Manually trigger a build for a repo."""
    user = _user_context(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    with db() as session:
        config = session.execute(select(WhConfig).where(WhConfig.repo == repo)).scalar_one_or_none()
        if not config or not config.access_token:
            return HTMLResponse("No token configured for this repo", status_code=400)
        access_token = config.access_token  # extract before session closes

        build = Build(repo=repo, branch=branch, commit_sha=commit_sha or "manual",
                       commit_msg=commit_msg[:500], author=user.get("login", ""), status="pending",
                       triggered_by="manual")
        session.add(build)
        session.commit()
        build_id = build.id

    asyncio.create_task(run_docker_build(build_id, repo, branch, commit_sha or "manual", access_token))

    await sse_broadcast("build_created", {
        "id": build_id, "repo": repo, "branch": branch,
        "commit_sha": commit_sha or "manual", "status": "pending",
    })

    return RedirectResponse(url=f"/projects/{repo}", status_code=302)


# ── Settings ──
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = _user_context(request)
    settings = get_settings()
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "settings": settings})

@app.post("/settings")
async def settings_save(request: Request,
    github_client_id: str = Form(""), github_client_secret: str = Form(""),
    host_url: str = Form(""), webhook_secret: str = Form(""),
    jwt_secret: str = Form(""), allowed_users: str = Form(""), build_dir: str = Form("")):
    user = _user_context(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    pairs = {
        "github_client_id": github_client_id,
        "github_client_secret": github_client_secret,
        "host_url": host_url,
        "webhook_secret": webhook_secret,
        "jwt_secret": jwt_secret,
        "allowed_users": allowed_users,
        "build_dir": build_dir,
    }
    with db() as session:
        for k, v in pairs.items():
            if v:
                existing = session.get(Setting, k)
                if existing:
                    existing.value = v
                    existing.updated_at = datetime.now(timezone.utc)
                else:
                    session.add(Setting(key=k, value=v))
        session.commit()

    reload_settings()
    return RedirectResponse(url="/", status_code=302)

@app.get("/healthz")
async def healthz():
    return Response(status_code=204)

# ── Static ──
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8008, reload=False, log_level="info")
