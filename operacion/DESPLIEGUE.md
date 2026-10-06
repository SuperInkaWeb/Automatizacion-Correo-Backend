# Despliegue

Lo que hay que tener resuelto antes de servir tráfico real, en el orden en
que hay que resolverlo.

---

## Bloqueantes

Dos cosas no las puede arreglar el código y hay que hacerlas a mano. La
primera es un bloqueante duro: sin ella el sistema arranca y *parece*
funcionar, que es justamente el problema. La segunda es una decisión con
compromisos, que admite un camino intermedio para empezar ya.

### 1. El rol de la base de datos no puede ser propietario

**Por qué es bloqueante.** PostgreSQL exime de las políticas de Row Level
Security al propietario de la tabla y a los superusuarios. Si la aplicación
se conecta con el rol que creó el esquema —lo normal en un montaje rápido—
RLS aparece habilitado en cada panel y **no filtra una sola fila**. No hay
error ni aviso: los datos fiscales de un cliente quedan visibles para otro.

El esquema usa `FORCE ROW LEVEL SECURITY`, que cubre al propietario, de modo
que esto es la segunda barrera y no la única. Pero las dos deben estar.

**Qué hacer.** Provisionar dos roles, como hace
[`scripts/init-db.sql`](../scripts/init-db.sql) en desarrollo:

| Rol | Para qué | Privilegios |
|-----|----------|-------------|
| propietario | Crear el esquema y aplicar migraciones | Dueño de las tablas |
| aplicación | Todo el tráfico de la API y de los workers | `SELECT, INSERT, UPDATE, DELETE`. **Sin** `CREATE` sobre el esquema, **sin** `SUPERUSER`, **sin** `BYPASSRLS` |

`DATABASE_URL` de la API y de los workers apunta al rol de aplicación. El de
las migraciones, al propietario. Nunca al revés.

**Cómo verificarlo.** Es exactamente lo que comprueban los tests de
integración, así que se pueden apuntar al entorno recién provisionado:

```bash
TEST_DATABASE_URL="postgresql+asyncpg://app:...@host/db" \
TEST_DATABASE_URL_OWNER="postgresql+asyncpg://owner:...@host/db" \
pytest -q -m integration tests/integration/test_aislamiento_rls.py
```

Si el rol es propietario o tiene `BYPASSRLS`, fallan con un mensaje que lo
dice. Es la única comprobación que distingue «RLS configurado» de «RLS
funcionando».

### 2. Gmail: modo prueba ahora, verificación CASA después

`gmail.readonly` es un *restricted scope*. Google ofrece dos caminos, y el
código funciona igual en los dos —la diferencia es solo del lado de Google—,
así que esto no bloquea el despliegue mientras se asuman los límites del
primero.

**Camino elegido para empezar: publishing status = "Testing".** Se deja la
pantalla de consentimiento en estado *Testing* y se añaden los correos que
van a conectarse como *test users*. Permite operar en producción sin esperar
la verificación. Sus límites, que hay que conocer antes de ponerlo delante
de nadie:

- **Máximo 100 usuarios de prueba**, y cada uno hay que añadirlo a mano en la
  consola.
- El usuario ve una **pantalla de "app no verificada"** al conectar, y tiene
  que pulsar "Avanzado → continuar". No es un error, pero asusta a quien no
  lo espera.
- **El refresh token caduca a los 7 días.** Es la trampa importante: es un
  comportamiento documentado de Google para las apps en *Testing*. En la
  práctica significa que **cada buzón conectado se desconecta solo cada
  semana** y el usuario tiene que volver a vincularlo.

  El código ya lo maneja sin romperse: cuando el refresh falla, la conexión
  se marca como revocada y la interfaz pide reconectar, en vez de reintentar
  en bucle. No hay pérdida de datos ni error silencioso. Pero la experiencia
  es esa, y hay que avisar a los usuarios de prueba.

**Camino para producción de verdad: verificación.** Publicar la pantalla de
consentimiento (publishing status → *In production*) e iniciar la evaluación
CASA. Quita los tres límites de arriba, incluido el vencimiento semanal. El
plazo habitual es de semanas, así que conviene **iniciarlo en paralelo**
desde el primer día aunque se empiece en modo prueba: no cuesta nada tenerlo
en marcha mientras se opera con usuarios de prueba.

**Outlook no tiene nada de esto.** Microsoft Graph (`Mail.Read`) no exige un
proceso equivalente ni caduca el refresh token a los 7 días, así que para una
prueba estable con usuarios reales desde ya, es el camino más cómodo.

---

## Configuración

La aplicación **valida sus ajustes al arrancar y se niega a levantar si algo
es inseguro**. Es deliberado: un contenedor que no arranca es visible, una
brecha silenciosa no. En `production` y `staging` se exige:

| Variable | Exigencia | Por qué |
|----------|-----------|---------|
| `CORS_ORIGINS` | No vacío, sin `*`, solo `https` | `*` con credenciales permite a cualquier origen leer respuestas |
| `DOCS_ENABLED` | `false` | `/docs` es el mapa completo de la superficie de ataque |
| `DB_ECHO` | `false` | Vuelca SQL con datos fiscales al log |
| `KMS_PROVIDER` | `aws` o `vault`, nunca `local` | Con `local` la clave maestra vive en una variable de entorno |
| `OAUTH_REDIRECT_URIS` | No vacío, sin `localhost` | Un redirect laxo es el vector clásico de robo del código de autorización |
| `STORE_RAW_OCR_TEXT` | `false` | Persistiría dato fiscal en claro |
| `STORAGE_ENDPOINT_URL` | No `localhost` | Señal de que quedó la configuración de desarrollo |
| `METRICS_TOKEN` | Definido | `/metrics` revela rutas internas, tasas de error y volumen de uso |

### Secretos

Todos por variable de entorno, desde el gestor de secretos de la
plataforma. **Ninguno en la imagen, en el repositorio ni en un `ConfigMap`.**

| Secreto | Generación |
|---------|-----------|
| `MASTER_KEY_B64` | 32 bytes aleatorios en base64. En producción la envuelve el KMS; esta variable solo existe para `KMS_PROVIDER=local` |
| `METRICS_TOKEN` | `openssl rand -base64 32` |
| `GOOGLE_CLIENT_SECRET`, `MICROSOFT_CLIENT_SECRET` | Consola del proveedor |
| `ANTHROPIC_API_KEY` | Consola de Anthropic. Solo si `VISION_AI_HABILITADA=true` |
| Contraseñas de base de datos | Rotables sin redespliegue si el pooler las relee |

**Sobre la rotación de `MASTER_KEY_B64`.** El cifrado es envolvente: la
clave maestra envuelve una DEK por tenant. Rotar la maestra reescribe solo
las DEK envueltas, no los datos. Sin esa indirección habría que descifrar y
volver a cifrar cada token de cada buzón.

---

## Orden de despliegue

Las migraciones van **antes** que el código nuevo, y deben ser compatibles
con el código viejo durante la ventana en que conviven. Es lo que permite
revertir sin tocar la base de datos.

```
1. migraciones (job, rol propietario, hasta completarse)
2. workers      (procesan trabajos ya encolados con el esquema nuevo)
3. API          (despliegue gradual; /health/ready decide cuándo recibe tráfico)
```

```bash
# 1
alembic upgrade head

# 2 y 3: la imagen es la misma, cambia el comando
docker run ... mailauto:TAG arq mailauto.workers.settings.WorkerDeIngesta
docker run ... mailauto:TAG uvicorn mailauto.bootstrap.app:crear_app --factory
```

### Migraciones destructivas

Una migración que elimina o renombra una columna **no** es compatible con el
código anterior, así que no se puede revertir sin pérdida. Se parten en dos
despliegues:

1. Añadir lo nuevo, escribir en ambos sitios, leer de lo viejo.
2. Leer de lo nuevo. Solo entonces, en un tercer despliegue, eliminar lo
   viejo.

Es más lento y es la diferencia entre poder revertir y no poder.

### Reversión

```bash
kubectl rollout undo deployment/mailauto-api
```

El esquema se queda como está: por eso la migración debe ser compatible
hacia atrás. `alembic downgrade` es el último recurso y nunca con tráfico
encima.

---

## Sondas

| Sonda | Endpoint | Por qué ese |
|-------|----------|-------------|
| Liveness | `/health/live` | No toca dependencias. Si comprobara la base de datos, una caída de PostgreSQL haría que el orquestador reiniciara en bucle contenedores sanos que volverían a servir en cuanto la base se recupere |
| Readiness | `/health/ready` | Comprueba base de datos, cola y almacén. Retira la réplica del balanceador sin matarla |

Confundirlas es el error clásico: una liveness que depende de la base de
datos convierte una degradación en una caída total.

Los workers no tienen HTTP. Su salud se mide por la antigüedad de la cola
(`ColaEstancada` en el [runbook](RUNBOOK.md#colaestancada)).

---

## Escalado

| Componente | Cómo | Límite real |
|------------|------|-------------|
| API | Horizontal, sin estado | El pool de PostgreSQL: `DB_POOL_SIZE × réplicas` debe caber en `max_connections`. Con muchas réplicas, poner PgBouncer en modo transaction (las sentencias preparadas ya están desactivadas para que sea compatible) |
| Worker de ingesta | Horizontal | Red del proveedor de correo y sus cuotas |
| Worker de cron | **Una sola réplica** | Dos procesos duplicarían cada purga y cada refresco programado |
| Redis | Vertical | `maxmemory-policy noeviction`: con `allkeys-lru` Redis descartaría trabajos encolados silenciosamente |

El worker de ingesta corre endurecido —sistema de ficheros de solo lectura,
`cap_drop: ALL`, `no-new-privileges`, `tmpfs` con `noexec`— porque es el
contenedor que abre ficheros de terceros. El perfil de `docker-compose.yml`
lo reproduce en desarrollo a propósito: así un fallo por restricciones
aparece en local y no al desplegar.

---

## Retención y purga

| Dato | Variable | Qué lo borra |
|------|----------|--------------|
| Adjuntos en el almacén | `RETENTION_DAYS_ATTACHMENTS` | Worker de cron |
| Estado OAuth en Redis | `OAUTH_STATE_TTL_SECONDS` | TTL de Redis |
| Contadores de cuota y de límite | — | TTL de Redis (la ventana forma parte de la clave) |
| Bitácora de auditoría | **No se purga** | Nada: es la evidencia |

La bitácora no admite `UPDATE` ni `DELETE` desde la aplicación. Las
políticas RLS solo cubren `SELECT` e `INSERT`, y además se retiran esos
privilegios al rol de aplicación, de modo que un intento es un error
explícito en el log de PostgreSQL y no una sentencia que termina con cero
filas afectadas.

---

## Observabilidad

| Pieza | Estado | Cómo |
|-------|--------|------|
| Logs | ✅ | JSON con `trace_id`, `request_id` y `tenant_id`, y redacción de tokens, RUC, correos y asuntos antes de serializar |
| Métricas | ✅ | `/metrics` en formato Prometheus, protegido con `METRICS_TOKEN`. Consultas de panel en el [runbook](RUNBOOK.md#panel) |
| Trazas | ✅ | OpenTelemetry sobre OTLP/HTTP a `OTEL_EXPORTER_ENDPOINT`. Sin endpoint no se instala nada, para no llenar la consola de errores de exportación en desarrollo |
| Alertas | ✅ | [`alertas.yml`](alertas.yml), validado con `promtool`. Cada alerta tiene su entrada en el runbook |
| Errores | ⏳ | `SENTRY_DSN` está reservado pero **no implementado**. Mientras tanto, los 5xx se siguen por logs y trazas |

El raspado de `/metrics` necesita la cabecera:

```yaml
scrape_configs:
  - job_name: mailauto-api
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/mailauto-metrics-token
    static_configs:
      - targets: ["mailauto-api:8000"]
```

---

## Lista de comprobación

- [ ] Rol de aplicación sin `SUPERUSER`, sin `BYPASSRLS` y **sin ser
      propietario**; verificado con los tests de integración contra el
      entorno real
- [ ] Migraciones aplicadas con el rol propietario
- [ ] Gmail: decidido el camino — modo *Testing* (añadir los test users; avisar del vencimiento semanal) o verificación CASA iniciada. Outlook no necesita ninguno
- [ ] `KMS_PROVIDER` distinto de `local` y clave maestra en el KMS
- [ ] `METRICS_TOKEN` definido y cargado en Prometheus
- [ ] `CORS_ORIGINS` con el dominio real, en `https`
- [ ] `DOCS_ENABLED=false`
- [ ] `OAUTH_REDIRECT_URIS` con las URL reales del frontend
- [ ] Bucket del almacén provisionado, con cifrado en reposo y ciclo de vida
- [ ] Redis con `maxmemory-policy noeviction`
- [ ] Worker de cron con una sola réplica
- [ ] Sondas apuntando a `/health/live` y `/health/ready`, cada una a la suya
- [ ] Alertas cargadas y con destinatario de guardia
- [ ] Copias de seguridad de PostgreSQL con restauración **probada**, no
      solo configurada
