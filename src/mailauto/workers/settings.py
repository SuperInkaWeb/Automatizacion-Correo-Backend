"""
Configuracion de los workers ARQ.

Proposito
    Ejecutar el pipeline de ingesta fuera del proceso web, con reintentos,
    timeouts y apagado controlado (hallazgo H3).

Flujo
    ARQ consume de Redis -> `ejecutar_escaneo` -> contenedor -> pipeline

Dependencias
    arq y el composition root.

Decisiones de diseño
    1. `max_tries` y `retry_jobs` activados. Un fallo transitorio de red
       no puede dar un escaneo por perdido; pero el tope evita el bucle
       infinito sobre un error permanente.

    2. `job_timeout` acotado. Un worker bloqueado en una llamada que
       nunca responde retiene su ranura de concurrencia indefinidamente.

    3. El contenedor se construye una vez en `on_startup` y se comparte
       entre jobs. Crear el pool de base de datos por job lo convertiria
       en el cuello de botella.

    4. El worker de cron corre en un proceso aparte, con su propia cola.
       Si compartiera cola con la ingesta, una avalancha de escaneos
       retrasaria el refresco de tokens, que es justo lo que los escaneos
       necesitan para funcionar.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from mailauto.bootstrap.container import Contenedor, construir_contenedor
from mailauto.bootstrap.settings import get_settings
from mailauto.modules.ingestion.infrastructure.cola_redis import (
    COLA_DE_CRON,
    COLA_DE_INGESTA,
)
from mailauto.shared.observability.logging import configurar_logging, obtener_logger

logger = obtener_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────


async def ejecutar_escaneo(ctx: dict[str, Any], tenant_id: str, trabajo_id: str) -> None:
    """
    Job de ingesta.

    Los argumentos viajan como cadenas porque la carga del job se
    serializa: pasar UUID directamente ata la cola a un formato de
    serializacion concreto.
    """
    from uuid import UUID

    contenedor: Contenedor = ctx["contenedor"]
    await contenedor.ejecutar_escaneo.ejecutar(
        tenant_id=UUID(tenant_id), trabajo_id=UUID(trabajo_id)
    )


async def extraer_adjunto(
    ctx: dict[str, Any],
    tenant_id: str,
    trabajo_id: str,
    adjunto_id: str,
    clave_de_almacenamiento: str,
    tipo_mime: str,
    nombre: str,
) -> None:
    """
    Job de extraccion: un adjunto, un job.

    Los argumentos van planos y como cadenas porque la carga del job
    se serializa; pasar objetos ataria la cola a un formato concreto.
    Llevar los metadatos en la carga, en vez de releerlos de la base
    de datos, ahorra una consulta por adjunto y hace el job autonomo.
    """
    from uuid import UUID

    from mailauto.modules.extraction.domain.ports import DocumentoAExtraer

    contenedor: Contenedor = ctx["contenedor"]
    tenant = UUID(tenant_id)

    contenido = await contenedor.lector_de_adjuntos.leer(tenant, clave_de_almacenamiento)
    await contenedor.extraer_documento.ejecutar(
        DocumentoAExtraer(
            adjunto_id=UUID(adjunto_id),
            tenant_id=tenant,
            trabajo_id=UUID(trabajo_id),
            contenido=contenido,
            tipo_mime=tipo_mime,
            nombre=nombre,
        )
    )


async def generar_exportacion(ctx: dict[str, Any], tenant_id: str, exportacion_id: str) -> None:
    """Job de reporte: consulta, genera el fichero y lo deja en storage."""
    from uuid import UUID

    contenedor: Contenedor = ctx["contenedor"]
    await contenedor.generar_exportacion.ejecutar(
        tenant_id=UUID(tenant_id), exportacion_id=UUID(exportacion_id)
    )


async def refrescar_tokens_proximos_a_vencer(ctx: dict[str, Any]) -> None:
    """
    Tarea periodica de mantenimiento de credenciales.

    Refrescar antes de que el usuario lance un escaneo evita que el
    primer paso del pipeline sea siempre una llamada al proveedor de
    identidad, y detecta pronto las conexiones revocadas para avisar en
    la UI en lugar de fallar a mitad de un trabajo.
    """
    logger.info("cron_refresco_de_tokens")
    # Implementacion en la Fase 7 (endurecimiento). El job existe desde
    # ahora para que la planificacion y el despliegue del worker de cron
    # queden cerrados en esta fase.


async def purgar_por_retencion(ctx: dict[str, Any]) -> None:
    """Elimina adjuntos, correos y registros que superaron su retencion."""
    logger.info("cron_purga_por_retencion")
    # Implementacion en la Fase 7, junto con la politica por tenant.


# ─────────────────────────────────────────────────────────────────────
# Ciclo de vida compartido
# ─────────────────────────────────────────────────────────────────────


async def _al_arrancar(ctx: dict[str, Any]) -> None:
    ajustes = get_settings()
    configurar_logging(nivel=ajustes.log_level, formato_json=ajustes.environment.es_productivo)
    ctx["contenedor"] = await construir_contenedor(ajustes)
    logger.info("worker_listo", entorno=ajustes.environment.value)


async def _al_apagar(ctx: dict[str, Any]) -> None:
    contenedor: Contenedor | None = ctx.get("contenedor")
    if contenedor is not None:
        await contenedor.cerrar()
    logger.info("worker_detenido")


def _redis_settings() -> RedisSettings:
    """
    Conexion a Redis leida directamente del entorno.

    No se usa `get_settings()` aqui a proposito: ARQ lee los atributos de
    la clase al importar el modulo, y validar la configuracion completa
    en ese momento haria que el modulo no se pudiera ni importar en un
    test. La validacion estricta ocurre en `_al_arrancar`, que es cuando
    el worker de verdad va a necesitarla.
    """
    return RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


def _entero_de_entorno(nombre: str, por_defecto: int) -> int:
    try:
        return int(os.environ.get(nombre, por_defecto))
    except ValueError:
        return por_defecto


# ─────────────────────────────────────────────────────────────────────
# Definiciones de worker
# ─────────────────────────────────────────────────────────────────────


class WorkerDeIngesta:
    """
    arq mailauto.workers.settings.WorkerDeIngesta

    Consume los jobs de escaneo. Es el proceso que mas escala
    horizontalmente: basta aumentar replicas cuando crece la cola.
    """

    functions: ClassVar[list[Any]] = [ejecutar_escaneo, extraer_adjunto, generar_exportacion]
    # Cola propia: sin esto, ARQ usa la cola por defecto y el worker de
    # cron (que no conoce estas funciones) se roba los jobs de escaneo y
    # los hace fallar con "function not found".
    queue_name = COLA_DE_INGESTA
    on_startup = _al_arrancar
    on_shutdown = _al_apagar
    redis_settings = _redis_settings()
    max_jobs = _entero_de_entorno("WORKER_MAX_JOBS", 8)
    job_timeout = _entero_de_entorno("WORKER_JOB_TIMEOUT_SECONDS", 1800)
    max_tries = _entero_de_entorno("WORKER_MAX_TRIES", 3)
    retry_jobs = True
    # Margen para que el job en curso termine antes de que el orquestador
    # mate el contenedor: es lo que hace que un redeploy no pierda trabajo.
    handle_signals = True
    keep_result = 3600


class WorkerDeCron:
    """
    arq mailauto.workers.settings.WorkerDeCron

    Tareas periodicas. Una sola replica: ejecutar el mismo cron en varios
    procesos duplicaria purgas y refrescos.
    """

    functions: ClassVar[list[Any]] = []
    cron_jobs: ClassVar[list[Any]] = [
        cron(refrescar_tokens_proximos_a_vencer, minute={0, 30}, run_at_startup=False),
        cron(purgar_por_retencion, hour=3, minute=0, run_at_startup=False),
    ]
    queue_name = COLA_DE_CRON
    on_startup = _al_arrancar
    on_shutdown = _al_apagar
    redis_settings = _redis_settings()
    max_jobs = 2
    job_timeout = 3600
