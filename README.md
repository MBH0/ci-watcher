
# 🚦 CI Watcher

> **Monitor de builds automatizado** — Recibe webhooks de GitHub, construye imágenes Docker y muestra logs en tiempo real con SSE.

---

## ✨ Características

| | |
|---|---|
| ⚡ **Webhook automático** | Activa un repo desde la UI y cada push construye automáticamente |
| 📦 **Docker build** | Clona el repo y ejecuta `docker build` mostrando la salida en vivo |
| 🔔 **SSE en tiempo real** | Notificaciones toast cuando un build empieza, termina o falla |
| 📊 **Dashboard por proyecto** | Builds agrupados por repo con estado, duración y mensajes de commit |
| 🛑 **Cancelar builds** | Botón para cancelar builds en ejecución |
| 🚀 **Build manual** | Dispara builds sin necesidad de push |
| 🔐 **GitHub OAuth** | Login con tu cuenta de GitHub, JWT seguro con cookies httponly |
| ⚙️ **Configuración persistente** | Settings guardados en SQLite, editables desde la UI |

---

## 🧱 Stack tecnológico

| Capa | Tecnología |
|---|---|
| **Backend** | Python 3.12 + FastAPI + SQLAlchemy + SQLite |
| **Frontend** | Jinja2 + Tailwind CSS v4 (utility-first) |
| **Auth** | GitHub OAuth + JWT + cookies httponly |
| **Builds** | Docker-in-Docker (socket mount `/var/run/docker.sock`) |
| **Tiempo real** | Server-Sent Events (SSE) con keepalive |
| **Infra** | Docker Compose con volumen persistente |

---

## 🚀 Cómo usarlo

### 1. Clonar y configurar

```bash
git clone https://github.com/MBH0/ci-watcher.git
cd ci-watcher
cp .env.example .env
```

Editar `.env`:

```env
GITHUB_CLIENT_ID=tu_client_id
GITHUB_CLIENT_SECRET=tu_client_secret
HOST_URL=https://tu-dominio.ngrok.app
ALLOWED_USERS=tu_usuario_github
```

### 2. Crear OAuth App en GitHub

1. Ve a **Settings → Developer settings → OAuth Apps → New OAuth App**
2. Llena:
   - **Application name:** `CI Watcher`
   - **Homepage URL:** `https://tu-dominio.ngrok.app`
   - **Authorization callback URL:** `https://tu-dominio.ngrok.app/auth/callback`
3. Copia el **Client ID** y **Client Secret** al `.env`

### 3. Iniciar con Docker

```bash
docker compose up -d --build
```

### 4. Exponer con ngrok (desarrollo local)

```bash
ngrok http 8008
```

Actualizar `HOST_URL` en `.env` con la URL de ngrok y reiniciar.

---

## 🖥️ Pantallas

| Ruta | Descripción |
|---|---|
| `/` | Dashboard con builds agrupados por proyecto |
| `/projects/{repo}` | Detalle del proyecto con todos sus builds |
| `/builds/{id}` | Log completo del build en tiempo real |
| `/repos` | Activar/desactivar webhooks por repositorio |
| `/settings` | Configuración persistente (OAuth, host, secrets) |

### Webhook API

```
POST /api/webhook ← GitHub Push Event (automático)
```

### Eventos SSE

```
GET /api/events   ← build_created, build_started, build_updated
```

---

## 📦 Endpoints principales

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Dashboard |
| `GET` | `/projects/{repo}` | Builds por proyecto |
| `GET` | `/builds/{id}` | Detalle del build con log |
| `GET` | `/repos` | Gestión de repositorios |
| `POST` | `/repos/activate` | Activar webhook para un repo |
| `POST` | `/repos/deactivate` | Desactivar webhook |
| `GET` | `/settings` | Configuración |
| `POST` | `/api/builds/trigger` | Build manual |
| `POST` | `/api/builds/{id}/cancel` | Cancelar build |
| `GET` | `/api/events` | SSE — eventos en vivo |
| `POST` | `/api/webhook` | Webhook receiver (GitHub) |
| `GET` | `/healthz` | Health check |
| `GET` | `/auth/login` | Login con GitHub |

---

## 🏗️ Estructura del proyecto

```
ci-watcher/
├── app/
│   ├── main.py              # FastAPI — rutas, auth, builds, SSE
│   ├── templates/            # Jinja2 templates
│   │   ├── base.html         # Layout global con sidebar + toasts SSE
│   │   ├── index.html        # Dashboard agrupado por proyectos
│   │   ├── project.html      # Detalle de proyecto con builds
│   │   ├── build.html        # Log del build en tiempo real
│   │   ├── repos.html        # Gestión de webhooks
│   │   ├── settings.html     # Configuración persistente
│   │   ├── login.html        # Pantalla de login
│   │   └── error.html        # Página de error
│   └── static/css/output.css # Tailwind CSS compilado
├── src/input.css             # Fuente Tailwind con tema personalizado
├── Dockerfile                # Imagen Docker con Python 3.12
├── docker-compose.yml        # Servicio con volumen persistente
├── .env.example              # Template de configuración
├── requirements.txt          # Dependencias Python
└── package.json              # Tailwind CLI
```

---

## 🔧 Desarrollo local

### Build CSS

```bash
npx tailwindcss -i ./src/input.css -o ./app/static/css/output.css --minify
```

### Logs

```bash
docker logs ci-watcher -f
```

### Detener

```bash
docker compose down
```

---

## 🤝 Creado por

<div align="center">
  <br>
  <a href="https://bett.es">
    <img src="https://bett.es/favicon.ico" width="48" alt="Bett AI">
    <br>
    <strong>Bett AI</strong>
  </a>
  <br>
  <sub>Automatización inteligente para equipos de desarrollo</sub>
  <br>
  <a href="https://bett.es">https://bett.es</a>
</div>
