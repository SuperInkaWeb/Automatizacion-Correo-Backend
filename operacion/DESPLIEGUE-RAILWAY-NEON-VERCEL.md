# Despliegue en Railway + Neon + Vercel + Cloudflare R2

Guía concreta para esta combinación. El documento general de criterios está
en [DESPLIEGUE.md](DESPLIEGUE.md); aquí van los pasos para estas plataformas.

## El reparto

| Pieza | Plataforma | Qué corre |
|-------|-----------|-----------|
| Base de datos | **Neon** | PostgreSQL con dos roles (dueño y aplicación) |
| API + workers + cola | **Railway** | 3 servicios (api, worker-ingesta, worker-cron) + Redis |
| Interfaz | **Vercel** | Next.js (lo construye nativo, sin Docker) |
| Adjuntos | **Cloudflare R2** | Almacén compatible con S3 |

Las tres plataformas que elegiste **no incluyen almacenamiento de objetos**,
de ahí el cuarto servicio (R2) para los adjuntos.

Hazlo en este orden: **Neon → R2 → Railway → Vercel → consolas OAuth →
sembrar dueño**. Cada paso necesita datos del anterior.

---

## 1. Neon (base de datos)

El punto delicado aquí es el aislamiento entre clientes: la aplicación debe
conectarse con un rol que **no sea el dueño** de las tablas. Neon te da un rol
dueño (algo como `neondb_owner`); ese corre las migraciones, y se crea un
segundo rol para la aplicación.

1. Crea un proyecto en **neon.tech**. Apunta la cadena de conexión que te da
   (es la del rol dueño).
2. En el **SQL Editor** de Neon, crea el rol de aplicación. Es el mismo
   `scripts/init-db.sql` del proyecto, adaptado a que el dueño ya existe:

   ```sql
   CREATE ROLE mailauto_app WITH LOGIN PASSWORD 'PON-UNA-CONTRASEÑA-FUERTE'
       NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
   GRANT CONNECT ON DATABASE neondb TO mailauto_app;
   GRANT USAGE ON SCHEMA public TO mailauto_app;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mailauto_app;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
       GRANT USAGE, SELECT ON SEQUENCES TO mailauto_app;
   ```

   > El aislamiento aguanta incluso si la app usara el rol dueño, porque el
   > esquema usa `FORCE ROW LEVEL SECURITY` (aplica también al dueño). El rol
   > aparte es la segunda barrera: impide que una inyección SQL pueda
   > *desactivar* las políticas, porque la aplicación no tiene permiso para
   > alterar tablas. Por eso se hace igualmente.

3. **Aplica las migraciones** desde tu máquina, con el rol **dueño** (una
   vez; Neon es accesible desde cualquier sitio):

   ```bash
   DATABASE_URL="postgresql+asyncpg://neondb_owner:...@...neon.tech/neondb" \
     alembic upgrade head
   ```

   Si prefieres no instalar nada local, se puede correr como un comando
   puntual en Railway una vez creado el servicio (paso 3).

Te quedan **dos** cadenas de conexión:
- La del **dueño** → solo para migraciones.
- La de **`mailauto_app`** → es la que va en `DATABASE_URL` de la app. Cambia
  el prefijo a `postgresql+asyncpg://` y añade `?sslmode=require` si Neon no
  lo trae.

---

## 2. Cloudflare R2 (adjuntos)

1. En el panel de Cloudflare → **R2** → crea un *bucket* (ej. `adjuntos`).
2. **Manage R2 API Tokens** → crea un token con permiso de lectura/escritura
   sobre ese bucket. Te da un **Access Key ID**, un **Secret** y una **URL de
   endpoint** del tipo `https://<id-de-cuenta>.r2.cloudflarestorage.com`.

Con eso llenas en el backend:

| Variable | Valor |
|----------|-------|
| `STORAGE_ENDPOINT_URL` | la URL del endpoint de R2 |
| `STORAGE_BUCKET` | `adjuntos` |
| `STORAGE_ACCESS_KEY` | el Access Key ID |
| `STORAGE_SECRET_KEY` | el Secret |
| `STORAGE_REGION` | `auto` |

> **Punto de control.** El almacén exige cifrado en reposo (`AES256`) en cada
> subida. R2 cifra en reposo siempre, y acepta esa cabecera; pero si al
> conectar el primer adjunto ves un error `NotImplemented` sobre *server side
> encryption*, avísame: es un cambio de dos líneas para que la cabecera sea
> opcional en proveedores que ya cifran por su cuenta. Es la misma clase de
> detalle que apareció con MinIO, por eso lo dejo señalado.

---

## 3. Railway (API, workers y Redis)

Railway despliega varios servicios desde el mismo repositorio. Conecta el
repo `automatizacion-correos-backend` y crea **cuatro** servicios:

### a) Redis
Railway → **New → Database → Redis**. Te da una variable `REDIS_URL`.

### b) Servicio `api`
- **Dockerfile:** `Dockerfile`.
- No hace falta configurar el puerto: la imagen se liga a `$PORT`, que
  Railway inyecta.
- **Healthcheck path:** `/health/live`.
- Variables de entorno: ver la tabla de más abajo.

### c) Servicio `worker-ingesta`
- **Dockerfile:** `Dockerfile.worker` (su comando por defecto ya es el worker
  de ingesta).
- Mismas variables que `api`.
- Es el que abre ficheros ajenos: si Railway lo permite, márcalo como el que
  escala en horizontal.

### d) Servicio `worker-cron`
- **Dockerfile:** `Dockerfile.worker`.
- **Override del comando de inicio:** `arq mailauto.workers.settings.WorkerDeCron`
- **Una sola réplica, siempre.** Dos procesos de cron duplicarían cada purga
  y cada refresco programado.

### Variables de entorno (api y los dos workers)

```
ENVIRONMENT=production
DATABASE_URL=postgresql+asyncpg://mailauto_app:...@...neon.tech/neondb?sslmode=require
REDIS_URL=   (la que da el Redis de Railway)
OIDC_ISSUER=https://TU-TENANT.us.auth0.com/
OIDC_AUDIENCE=https://api.automatizacion-correos
MASTER_KEY_B64=   (genera: python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")
KMS_PROVIDER=local
KMS_LOCAL_EN_PRODUCCION_ACEPTADO=true
GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...        (si usas Gmail)
MICROSOFT_CLIENT_ID=... MICROSOFT_CLIENT_SECRET=... MICROSOFT_TENANT_ID=common  (si usas Outlook)
OAUTH_REDIRECT_URIS=["https://TU-APP.vercel.app/oauth/callback"]
STORAGE_ENDPOINT_URL=https://....r2.cloudflarestorage.com
STORAGE_BUCKET=adjuntos STORAGE_REGION=auto
STORAGE_ACCESS_KEY=... STORAGE_SECRET_KEY=...
METRICS_TOKEN=   (genera: openssl rand -base64 32)
CORS_ORIGINS=["https://TU-APP.vercel.app"]
DOCS_ENABLED=false
```

> `KMS_LOCAL_EN_PRODUCCION_ACEPTADO=true` es la aceptación explícita de que la
> clave maestra vive en el secreto de Railway y no en un KMS gestionado. Sin
> esa línea, la app se niega a arrancar en producción, a propósito.

Cuando el servicio `api` esté desplegado, Railway le da una URL pública
(`https://....up.railway.app`). Esa es la que necesita Vercel.

---

## 4. Vercel (interfaz)

1. Importa el repo `automatizacion-correos-frontend`. Vercel detecta Next.js
   solo; no uses el Dockerfile.
2. Variables de entorno:

   ```
   API_URL=https://TU-API.up.railway.app      (la URL pública del backend en Railway)
   APP_URL=https://TU-APP.vercel.app
   OIDC_ISSUER=https://TU-TENANT.us.auth0.com
   OIDC_CLIENT_ID=...        (de la app de Auth0)
   OIDC_CLIENT_SECRET=...
   OIDC_AUDIENCE=https://api.automatizacion-correos
   SESSION_SECRET=   (genera: node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))")
   NEXT_PUBLIC_APP_NAME=Automatización de Correos
   ```

> El navegador nunca habla directo con Railway: la interfaz llama a su propio
> BFF (en Vercel) y este reenvía al backend. Por eso `API_URL` es del lado
> servidor y el `CORS_ORIGINS` del backend casi no se ejercita — se pone por
> coherencia y porque la validación de producción lo exige.

---

## 5. Consolas OAuth (URLs de producción)

Ahora que conoces la URL de Vercel, regístrala:

- **Auth0** → tu app → *Allowed Callback URLs*: `https://TU-APP.vercel.app/api/auth/callback`;
  *Allowed Logout URLs*: `https://TU-APP.vercel.app`.
- **Google** y **Microsoft** → en las credenciales OAuth, *URI de redirección*:
  `https://TU-APP.vercel.app/oauth/callback` (la misma que pusiste en
  `OAUTH_REDIRECT_URIS` del backend).

---

## 6. Sembrar el primer dueño

Inicia sesión una vez en `https://TU-APP.vercel.app` (eso crea tu cuenta).
Luego, contra la base de Neon:

```bash
DATABASE_URL="postgresql+asyncpg://mailauto_app:...@...neon.tech/neondb?sslmode=require" \
  python scripts/sembrar_tenant.py --email tu@correo.com --nombre "Mi Empresa"
```

Desde ahí ya puedes conectar un buzón y lanzar un escaneo.

---

## Comprobación final

- `https://TU-API.up.railway.app/health/ready` responde con los tres
  componentes en `true`.
- Inicias sesión en Vercel y llegas al panel (no a una pantalla vacía: eso
  significaría que falta sembrar el dueño).
- Conectas un buzón y vuelve sin 404 a `/buzones`.
- Lanzas un escaneo y avanza.

Si algo falla, el [runbook](RUNBOOK.md) tiene una entrada por síntoma.
