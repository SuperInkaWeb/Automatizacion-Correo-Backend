"""
Cola de trabajos y canal de progreso sobre Redis.

Proposito
    Sacar la ejecucion del pipeline fuera del proceso web (hallazgo H3) y
    permitir que cualquier replica de la API sirva el progreso en vivo de
    un trabajo que corre en otro worker.

Dependencias
    arq (encolado) y redis.asyncio (señales y pub/sub).

Decisiones de diseño
    1. La cancelacion es una clave en Redis con TTL, no una señal al
       proceso. El worker la consulta entre mensajes y se detiene
       ordenadamente: cierra la transaccion, guarda contadores y publica
       el estado final. Un `kill` dejaria el trabajo "en ejecucion" para
       siempre.

    2. Pub/sub y no una lista. El progreso es informacion efimera: si
       nadie mira la pantalla, no hay que guardarlo. El estado duradero
       ya esta en PostgreSQL, que es de donde se recupera al reconectar.

    3. El `_job_id` de ARQ se deriva del `trabajo_id`. ARQ descarta un
       job con un id que ya esta en la cola, asi que una doble publicacion
       por un reintento de red no produce dos ejecuciones.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from uuid import UUID

from arq import ArqRedis
from redis.asyncio import Redis

from mailauto.modules.ingestion.domain.ports import CanalDeProgreso, ColaDeTrabajos
from mailauto.shared.observability.logging import obtener_logger

logger = obtener_logger(__name__)

NOMBRE_DEL_JOB_DE_ESCANEO = "ejecutar_escaneo"
NOMBRE_DEL_JOB_DE_EXTRACCION = "extraer_adjunto"
NOMBRE_DEL_JOB_DE_EXPORTACION = "generar_exportacion"

# Colas SEPARADAS por worker. Si la ingesta y el cron comparten cola, se
# roban los jobs entre si: cada worker solo registra SUS funciones, asi
# que cuando el cron saca un `ejecutar_escaneo` (o la ingesta un job de
# cron) ARQ no encuentra la funcion y el trabajo falla. Cada tipo de
# worker consume exclusivamente de la suya.
COLA_DE_INGESTA = "mailauto:ingesta"
COLA_DE_CRON = "mailauto:cron"

_PREFIJO_CANCELACION = "scan:cancel:"
_PREFIJO_CANAL = "scan:progress:"
# La señal de cancelacion vive algo mas que el timeout maximo de un job:
# asi no desaparece antes de que el worker pueda leerla.
_TTL_CANCELACION_SEGUNDOS = 7200


class ColaDeTrabajosRedis(ColaDeTrabajos):
    def __init__(self, arq: ArqRedis, redis: Redis) -> None:
        self._arq = arq
        self._redis = redis

    async def encolar_escaneo(self, *, tenant_id: UUID, trabajo_id: UUID) -> str:
        job = await self._arq.enqueue_job(
            NOMBRE_DEL_JOB_DE_ESCANEO,
            str(tenant_id),
            str(trabajo_id),
            _job_id=f"scan:{trabajo_id}",
            _queue_name=COLA_DE_INGESTA,
        )
        if job is None:
            # ARQ devuelve None cuando el `_job_id` ya existe. No es un
            # error: es la idempotencia funcionando.
            logger.info("job_ya_encolado", trabajo_id=str(trabajo_id))
            return f"scan:{trabajo_id}"
        return job.job_id

    async def encolar_extraccion(
        self,
        *,
        tenant_id: UUID,
        trabajo_id: UUID,
        adjunto_id: UUID,
        clave_de_almacenamiento: str,
        tipo_mime: str,
        nombre: str,
    ) -> str:
        job = await self._arq.enqueue_job(
            NOMBRE_DEL_JOB_DE_EXTRACCION,
            str(tenant_id),
            str(trabajo_id),
            str(adjunto_id),
            clave_de_almacenamiento,
            tipo_mime,
            nombre,
            # El id deriva del adjunto: ARQ descarta un job cuyo id ya
            # esta en la cola, asi que una doble publicacion no produce
            # dos extracciones del mismo documento.
            _job_id=f"extract:{adjunto_id}",
            _queue_name=COLA_DE_INGESTA,
        )
        return job.job_id if job is not None else f"extract:{adjunto_id}"

    async def encolar_exportacion(self, *, tenant_id: UUID, exportacion_id: UUID) -> str:
        job = await self._arq.enqueue_job(
            NOMBRE_DEL_JOB_DE_EXPORTACION,
            str(tenant_id),
            str(exportacion_id),
            _job_id=f"export:{exportacion_id}",
            _queue_name=COLA_DE_INGESTA,
        )
        return job.job_id if job is not None else f"export:{exportacion_id}"

    async def solicitar_cancelacion(self, trabajo_id: UUID) -> None:
        await self._redis.set(
            f"{_PREFIJO_CANCELACION}{trabajo_id}", "1", ex=_TTL_CANCELACION_SEGUNDOS
        )

    async def cancelacion_solicitada(self, trabajo_id: UUID) -> bool:
        return bool(await self._redis.exists(f"{_PREFIJO_CANCELACION}{trabajo_id}"))

    async def estado(self) -> tuple[int, float]:
        """
        Profundidad de la cola y antiguedad del trabajo mas viejo.

        ARQ guarda los trabajos pendientes en un conjunto ordenado por su
        instante de ejecucion, asi que ambas cifras salen de dos consultas
        baratas y no hay que recorrer nada.

        La antiguedad es la cifra que de verdad importa para una alerta: la
        profundidad sola no distingue una cola de mil trabajos que se vacia
        en un minuto de una de cincuenta que lleva una hora atascada.
        """
        pendientes = await self._arq.zcard(COLA_DE_INGESTA)
        if not pendientes:
            return 0, 0.0

        primeros = await self._arq.zrange(COLA_DE_INGESTA, 0, 0, withscores=True)
        if not primeros:
            return int(pendientes), 0.0

        # Las puntuaciones de ARQ son milisegundos desde la epoca.
        _, marca = primeros[0]
        antiguedad = max(0.0, time.time() - float(marca) / 1000)
        return int(pendientes), antiguedad

    async def esta_disponible(self) -> bool:
        try:
            await self._redis.ping()
            return True
        except Exception:  # noqa: BLE001 - el progreso es cosmetico; el estado real esta en la BD
            return False


class CanalDeProgresoRedis(CanalDeProgreso):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publicar(self, trabajo_id: UUID, evento: dict[str, object]) -> None:
        try:
            await self._redis.publish(
                f"{_PREFIJO_CANAL}{trabajo_id}", json.dumps(evento, default=str)
            )
        except Exception:  # noqa: BLE001 - sonda de salud: informa binario, no diagnostica
            # El progreso es cosmetico: que falle su publicacion no puede
            # tumbar un escaneo que por lo demas va bien. El estado real
            # queda en PostgreSQL.
            logger.warning("fallo_al_publicar_progreso", trabajo_id=str(trabajo_id))

    async def suscribirse(self, trabajo_id: UUID) -> AsyncIterator[dict[str, object]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"{_PREFIJO_CANAL}{trabajo_id}")
        try:
            async for mensaje in pubsub.listen():
                if mensaje.get("type") != "message":
                    continue
                try:
                    datos = json.loads(mensaje["data"])
                except (ValueError, TypeError):
                    continue
                if isinstance(datos, dict):
                    yield datos
        finally:
            # Sin este cierre, cada cliente SSE que se desconecta deja una
            # suscripcion viva y Redis acaba con miles de canales abiertos.
            await pubsub.unsubscribe(f"{_PREFIJO_CANAL}{trabajo_id}")
            await pubsub.aclose()
