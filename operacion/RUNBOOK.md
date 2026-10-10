# Runbook

Procedimientos de guardia. Una entrada por alerta de
[`alertas.yml`](alertas.yml), con el mismo nombre.

El orden de cada entrada es siempre el mismo: **qué significa**, **cómo
confirmarlo**, **qué hacer** y **qué no hacer**. El último apartado existe
porque la mayoría de los incidentes se alargan por una acción
bienintencionada que borra la evidencia o empeora el estado.

---

## Antes de nada

```bash
curl -s https://api.ejemplo.com/health/ready | jq .data
```

Dice en una línea si la base de datos, la cola y el almacenamiento son
alcanzables. Es el primer comando de cualquier incidente.

Para las métricas hace falta el token (`METRICS_TOKEN`):

```bash
curl -s -H "Authorization: Bearer $METRICS_TOKEN" https://api.ejemplo.com/metrics | grep mailauto_cola
```

Los logs son JSON con `trace_id`, `request_id` y `tenant_id`. Un incidente
se reconstruye filtrando por `trace_id`, que viaja también en la cabecera
`X-Request-ID` de la respuesta: si un usuario reporta un error, ese
identificador es lo único que hay que pedirle.

**Ningún log lleva tokens, cabeceras de autorización, RUC, correos ni
asuntos**: hay un procesador de redacción que los enmascara antes de
serializar. Si se necesita el dato real para diagnosticar, está en la base
de datos, con control de acceso.

---

## ApiCaida

**Qué significa.** Prometheus no obtiene métricas de una instancia. Si caen
todas, no hay servicio.

**Cómo confirmarlo.**

```bash
kubectl get pods -l app=mailauto-api
kubectl logs -l app=mailauto-api --tail=100
```

**Qué hacer.**

1. Si el pod está en `CrashLoopBackOff`, leer el primer error del arranque.
   La causa más frecuente es configuración: la aplicación valida sus
   ajustes al arrancar y **se niega a levantar si algo es inseguro**. El
   mensaje nombra la variable exacta.
2. Si el pod está vivo pero no responde, comprobar `/health/live`. Si
   responde y Prometheus no lo ve, el problema es de red o del raspado, no
   del servicio.
3. Si fue un despliegue, revertir a la revisión anterior antes de
   investigar.

**Qué no hacer.** No relajar la configuración para que arranque. Un
contenedor que no levanta es visible; uno que levanta inseguro, no.

---

## SondaDePreparacionDegradada

**Qué significa.** `/health/ready` devuelve 503: alguna dependencia no es
alcanzable. El balanceador ya retiró la réplica, no la mató: volverá sola
cuando la dependencia se recupere.

**Cómo confirmarlo.** El cuerpo de `/health/ready` nombra el componente:

```json
{ "estado": "degradado", "componentes": { "base_de_datos": true, "cola": false, "almacenamiento": true } }
```

**Qué hacer.**

- `base_de_datos: false` → comprobar PostgreSQL y el pool. Un pool agotado
  da el mismo síntoma que una base caída.
- `cola: false` → comprobar Redis. Con Redis caído, el limitador y las
  cuotas **dejan pasar** por decisión de diseño; los escaneos no se encolan.
- `almacenamiento: false` → comprobar credenciales y política del bucket.
  La ingesta falla al guardar adjuntos, pero nada se corrompe.

**Qué no hacer.** No reiniciar la API: no es ella la que falla, y
reiniciarla borra el pool caliente y los contadores en memoria.

---

## TasaDeErroresDelServidor

**Qué significa.** Más del 1 % de las peticiones termina en 5xx. **Un 5xx
es siempre un defecto**: lo que el cliente hace mal se responde con 4xx.

**Cómo confirmarlo.**

```promql
sum by (ruta) (rate(mailauto_peticiones_http_total{estado=~"5.."}[5m]))
```

Y en los logs, `nivel=error`: el manejador de errores registra cada
excepción no prevista con su `trace_id`.

**Qué hacer.**

1. Si está concentrado en una ruta, es un defecto de ese endpoint: revertir
   el despliegue si coincide en el tiempo.
2. Si está repartido, mirar dependencias: un error de base de datos se
   propaga a todo.
3. Abrir la traza en Jaeger por `trace_id` para ver en qué span falla.

**Qué no hacer.** No responder sólo subiendo réplicas. Un defecto
replicado sigue siendo un defecto.

---

## LecturasLentas

**Qué significa.** El p95 de los listados pasa de 300 ms, que es el
objetivo declarado.

**Cómo confirmarlo.**

```promql
histogram_quantile(0.95, sum by (le, ruta) (rate(mailauto_peticion_http_segundos_bucket[10m])))
```

**Qué hacer.**

1. Comprobar en PostgreSQL si la consulta dejó de usar su índice:

   ```sql
   SELECT query, calls, mean_exec_time
   FROM pg_stat_statements
   ORDER BY mean_exec_time DESC
   LIMIT 10;
   ```

2. La causa habitual es una tabla que creció hasta que el plan cambió.
   La paginación es por cursor y usa comparación de tuplas: el índice
   compuesto debe cubrir exactamente el orden del cursor.
3. Si hay un `Seq Scan` sobre `extracted_records`, falta un índice o la
   migración que lo creaba no se aplicó.

**Qué no hacer.** No añadir un índice en producción sin migración. El
siguiente despliegue lo contradiría y nadie sabría por qué cambió el plan.

---

## ColaEstancada

**Qué significa.** El trabajo más antiguo lleva más de 30 minutos sin
empezar. Los usuarios ven escaneos que no avanzan.

**Cómo confirmarlo.**

```bash
kubectl get pods -l app=mailauto-worker
```

```bash
redis-cli zcard arq:queue
```

**Qué hacer.**

1. Si no hay workers vivos, es lo primero: arrancarlos. Los trabajos no se
   pierden, están en Redis.
2. Si hay workers vivos y la cola no baja, están bloqueados. Mirar sus logs:
   un documento patológico puede agotar el tiempo del job. El job tiene
   `WORKER_JOB_TIMEOUT_SECONDS` y reintentos, así que acabará marcado como
   fallido y liberará el worker.
3. Un escaneo que no debe seguir se cancela por la API
   (`POST /api/v1/scans/{id}/cancel`), que es cooperativo y deja el estado
   consistente.

**Qué no hacer.** No vaciar la cola de Redis. Se pierden trabajos que la
base de datos sigue dando por encolados, y el sistema queda en un estado
que nadie sabe describir: escaneos eternamente «en cola» sin nada
procesándolos.

---

## ColaCreciendo

**Qué significa.** Entra más trabajo del que se procesa. Todavía no afecta
a nadie, pero acabará en `ColaEstancada`.

**Cómo confirmarlo.**

```promql
deriv(max(mailauto_cola_profundidad)[30m:1m])
```

**Qué hacer.**

1. Escalar el worker de ingesta. Es horizontal: varias réplicas se reparten
   los trabajos por Redis sin coordinación adicional.
2. Si la causa es un cliente concreto lanzando escaneos en serie, su cuota
   (`RATE_LIMIT_SCAN_PER_HOUR`) debería estar cortándolo; comprobar que su
   plan es el correcto.

**Qué no hacer.** No subir `WORKER_MAX_JOBS` sin mirar la memoria: cada job
concurrente abre documentos y el worker tiene límite de 2 GB. Un OOM
convierte una cola lenta en una cola parada.

---

## ExtraccionFallandoEnUnaEstrategia

**Qué significa.** Una de las cuatro estrategias falla en más de la mitad
de los intentos. Las demás cubren el hueco, así que **no hay errores
visibles**: hay registros incompletos. Es degradación silenciosa, y es la
razón de que esta alerta exista.

**Cómo confirmarlo.**

```promql
sum by (estrategia) (rate(mailauto_extracciones_total{resultado="fallo"}[30m]))
  / sum by (estrategia) (rate(mailauto_extracciones_total[30m]))
```

**Qué hacer.**

- `pdf_texto` / `pdf_tabla` → suele ser un cambio de formato en el emisor
  del documento. Revisar el perfil de extracción.
- `ocr` → comprobar que Tesseract y sus datos de idioma están en la imagen
  del worker. Si falta `tesseract-ocr-spa`, falla el 100 %.
- `vision_ia` → ver `VisionIaFallando`.

**Qué no hacer.** No desactivar la estrategia que falla. Bajaría la
completitud de todos los registros sin que nadie lo note, que es
exactamente el problema que la alerta señala.

---

## VisionIaFallando

**Qué significa.** Más del 20 % de las llamadas al modelo falla. El
pipeline sigue: la IA es la última estrategia y la más cara.

**Qué hacer.**

1. Comprobar la credencial del proveedor activo (`VISION_PROVEEDOR`):
   `GROQ_API_KEY` para Groq (por defecto) o `ANTHROPIC_API_KEY` para
   Anthropic, y la cuota de esa cuenta.
2. Revisar el tamaño de las imágenes enviadas: un documento muy grande
   puede exceder el límite de la petición.
3. `VISION_MAXIMO_LLAMADAS_POR_TRABAJO` acota el gasto por trabajo. Si la
   alerta coincide con un pico de coste, bajarlo es una mitigación válida.

**Qué no hacer.** No subir el límite de llamadas para «compensar» los
fallos. Multiplica el coste sin arreglar la causa.

---

## EtapaDelPipelineLenta

**Qué significa.** Una etapa supera los dos minutos en p95.

**Qué hacer.** Casi siempre es OCR sobre documentos escaneados a resolución
muy alta. El preprocesado con OpenCV ya reescala; si la etapa sigue lenta,
revisar si llegan PDF de muchas páginas y considerar un tope por documento.

**Qué no hacer.** No subir el timeout del job sin más: alarga el bloqueo
del worker y acerca `ColaEstancada`.

---

## MuchosRechazosPorCuota

**Qué significa.** Clientes alcanzando su cuota de forma sostenida. **No es
un fallo técnico.**

**Cómo confirmarlo.**

```bash
redis-cli --scan --pattern 'cuota:*'
```

La clave es `cuota:<recurso>:<tenant_id>:<ventana>`, así que se ve
exactamente quién y en qué.

**Qué hacer.** Decidir si el cliente necesita más plan o si su integración
reintenta sin respetar el `Retry-After` del 429. Lo segundo es lo habitual.

**Qué no hacer.** No subir `RATE_LIMIT_SCAN_PER_HOUR` global para resolver
el caso de un cliente: afecta a todos y retira la protección.

---

## CortafuegosRechazandoMucho

**Qué significa.** El límite por minuto corta tráfico de forma continua.
Este control es **por IP y anterior a la autenticación**: es un cortafuegos
contra avalanchas, no una cuota.

**Cómo confirmarlo.** Buscar `rate_limit_excedido` en los logs; la entrada
incluye la identidad (`ip:<direccion>:<tenant>`).

**Qué hacer.**

1. Si es una sola IP, es un cliente mal programado o un abuso: bloquear en
   el borde de red, que es más barato que en la aplicación.
2. Si son muchas, puede ser tráfico legítimo que creció. Subir
   `RATE_LIMIT_DEFAULT_PER_MINUTE` es aceptable aquí, al contrario que con
   las cuotas.

**Qué no hacer.** No mover las cuotas de negocio a este control.
[Ya ocurrió](../pruebas-de-carga/README.md#lo-que-encontró): contar una
cuota horaria por IP antes de autenticar permite que cualquiera **sin
credenciales** la agote para todos los que compartan salida a internet.

---

## ProveedorDeCorreoRechazando

**Qué significa.** Gmail o Graph rechazan más del 30 % de las peticiones.

**Qué hacer.**

1. Si son **401**, los tokens dejaron de ser válidos: el usuario revocó el
   consentimiento o cambió la contraseña. La conexión queda marcada y el
   usuario debe volver a vincular. No hay nada que arreglar en el servidor.
2. Si son **403**, falta un permiso o el proyecto perdió la verificación
   del scope restringido. Ver `DESPLIEGUE.md`.
3. Si son **429**, es cuota del proveedor. El cliente ya aplica reintentos
   con espera y jitter; si persiste, repartir los escaneos en el tiempo.

**Qué no hacer.** No reintentar un 401 en bucle. Cada intento acerca el
bloqueo de la cuenta en el proveedor.

---

## ProveedorDeCorreoCaido

**Qué significa.** El proveedor devuelve 5xx. Es una caída ajena.

**Qué hacer.** Comprobar su página de estado **antes** de investigar nada
propio. Los escaneos afectados se marcan como fallidos y son reintentables;
no hay pérdida de datos.

**Qué no hacer.** No tocar nada propio mientras dure. Un cambio durante una
caída ajena es un cambio sin forma de verificar.

---

## Panel

Consultas de los paneles, en orden de utilidad durante un incidente:

| Panel | Consulta |
|-------|----------|
| Peticiones por segundo | `sum by (ruta) (rate(mailauto_peticiones_http_total[5m]))` |
| Tasa de error | `sum(rate(mailauto_peticiones_http_total{estado=~"5.."}[5m])) / sum(rate(mailauto_peticiones_http_total[5m]))` |
| Latencia p50 / p95 / p99 | `histogram_quantile(0.95, sum by (le, ruta) (rate(mailauto_peticion_http_segundos_bucket[5m])))` |
| Profundidad de la cola | `max(mailauto_cola_profundidad)` |
| Antigüedad de la cola | `max(mailauto_cola_antiguedad_segundos)` |
| Éxito por estrategia | `sum by (estrategia) (rate(mailauto_extracciones_total{resultado="exito"}[30m])) / sum by (estrategia) (rate(mailauto_extracciones_total[30m]))` |
| Duración por etapa (p95) | `histogram_quantile(0.95, sum by (le, etapa) (rate(mailauto_etapa_segundos_bucket[30m])))` |
| Llamadas a la IA | `sum by (resultado) (rate(mailauto_llamadas_a_ia_total[30m]))` |
| Rechazos por límite | `sum by (control) (rate(mailauto_rechazos_por_limite_total[5m]))` |
| Salud de proveedores | `sum by (proveedor, clase) (rate(mailauto_respuestas_de_proveedor_total[15m]))` |

**Ninguna serie lleva `tenant_id`**, y es deliberado: una etiqueta por
cliente hace crecer la cardinalidad sin techo y expone cuántos clientes hay
y cuánto usa cada uno en un endpoint que suele estar menos protegido que la
API. El consumo por cliente se consulta en la base de datos.
