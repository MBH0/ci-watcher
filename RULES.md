# CI Watcher — Rules

## Run
```bash
docker compose up -d --build   # Start
docker compose down            # Stop
docker logs ci-watcher -f      # Live logs
```

## Config
- `.env` — gitignored, contiene secrets (GitHub OAuth, JWT, etc.)
- `.env.example` — template de configuración
- Settings se pueden configurar desde la UI en `/settings` (persistido en SQLite)

## Endpoints
| URL | Descripción |
|-----|-------------|
| `/` | Dashboard de builds |
| `/repos` | Gestionar webhooks de repos |
| `/settings` | Configuración persistente |
| `/api/webhook` | Webhook receiver (GitHub push) |
| `/api/events` | SSE — eventos en tiempo real |
| `/healthz` | Health check |

## Tecnología
- **Backend:** FastAPI + SQLAlchemy + SQLite
- **Frontend:** Jinja2 + Tailwind CSS v4
- **Auth:** GitHub OAuth + JWT + cookies httponly
- **Builds:** Docker-in-Docker (socket mount)

## Proyecto
- Repo: `github.com/MBH0/ci-watcher`
- Local: `~/projects/ci-watcher/`
