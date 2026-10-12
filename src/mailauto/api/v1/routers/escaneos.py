"""
Rutas de escaneo, incluido el canal SSE de progreso.

Proposito
    Controlar el ciclo de vida de un escaneo y transmitir su avance en
    vivo sin que el frontend tenga que hacer polling (hallazgo H8).

Dependencias
    FastAPI, casos de uso de ingesta, canal de progreso.

Decisiones de diseño
    1. `POST /scans` responde 202 con `Location`. El trabajo no esta
       hecho: esta aceptado. Devolver 200 con un resultado vacio mentiria
       sobre el estado real.

    2. El stream SSE envia primero el estado actual y luego los eventos.
       Si el cliente se conecta tarde o reconecta, no se queda esperando
       un evento que quiza ya paso; arranca con la foto real.

    3. Hay un latido periodico. Los proxies y balanceadores cierran
       conexiones ociosas a los 30-60 segundos; un comentario SSE cada
       15 mantiene viva la conexion durante las fases largas sin eventos.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse

from mailauto.api.deps import (
    ContenedorDep,
    ContextoDep,
    PaginacionDep,
    limita_por_tenant,
    obtener_clave_de_idempotencia,
)
from mailauto.api.schemas.comunes import (
    ErrorDeProcesamientoSalida,
    EscaneoSalida,
    IniciarEscaneoEntrada,
    MetaDePagina,
    Respuesta,
)
from mailauto.modules.audit.domain.entities import AccionAuditada
from mailauto.modules.ingestion.domain.entities import ParametrosDeEscaneo

router = APIRouter(prefix="/scans", tags=["escaneos"])

_SEGUNDOS_ENTRE_LATIDOS = 15.0


# La cuota se declara en la ruta y no dentro del caso de uso porque es
# una politica del borde HTTP: un escaneo lanzado por el worker de cron no
# debe consumirla.
@router.post(
    "",
    response_model=Respuesta[EscaneoSalida],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        limita_por_tenant(
            "escaneos",
            maximo_de=lambda ajustes: ajustes.rate_limit_scan_per_hour,
            ventana_segundos=3600,
        )
    ],
)
async def iniciar(
    entrada: IniciarEscaneoEntrada,
    contexto: ContextoDep,
    contenedor: ContenedorDep,
    respuesta: Response,
    clave_de_idempotencia: Annotated[str | None, Depends(obtener_clave_de_idempotencia)] = None,
) -> Respuesta[EscaneoSalida]:
    """Encola un escaneo. Idempotente si se envia `Idempotency-Key`."""
    trabajo = await contenedor.iniciar_escaneo.ejecutar(
        contexto,
        conexion_id=entrada.conexion_id,
        parametros=ParametrosDeEscaneo(
            desde=entrada.desde,
            hasta=entrada.hasta,
            limite_de_mensajes=entrada.limite_de_mensajes,
            carpeta=entrada.carpeta,
        ),
        clave_de_idempotencia=clave_de_idempotencia,
    )
    respuesta.headers["Location"] = f"/api/v1/scans/{trabajo.id}"

    await contenedor.auditoria.registrar(
        contexto,
        accion=AccionAuditada.ESCANEO_INICIADO,
        tipo_de_recurso="scan_job",
        recurso_id=trabajo.id,
        metadatos={"limite": entrada.limite_de_mensajes},
    )
    return Respuesta(data=EscaneoSalida.desde_dominio(trabajo))


@router.get("", response_model=Respuesta[list[EscaneoSalida]])
async def listar(
    contexto: ContextoDep, contenedor: ContenedorDep, pagina: PaginacionDep
) -> Respuesta[list[EscaneoSalida]]:
    resultado = await contenedor.consultar_escaneo.listar(contexto, pagina)
    return Respuesta(
        data=[EscaneoSalida.desde_dominio(t) for t in resultado.elementos],
        meta=MetaDePagina(cursor=resultado.siguiente_cursor, hay_mas=resultado.hay_mas),
    )


@router.delete("/data", response_model=Respuesta[dict[str, int]])
async def reiniciar_datos(
    contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[dict[str, int]]:
    """
    Borra TODOS los datos de escaneo del tenant para poder re-escanear.

    Elimina registros, adjuntos, correos, trabajos, errores y reportes,
    pero CONSERVA los buzones conectados: no es desvincular la cuenta.
    Es irreversible, por eso la interfaz pide una confirmacion explicita
    antes de llamar. Devuelve cuantas filas borro de cada tipo.
    """
    resumen = await contenedor.reiniciar_datos_de_escaneo.ejecutar(contexto)
    await contenedor.auditoria.registrar(
        contexto,
        accion=AccionAuditada.DATOS_PURGADOS,
        tipo_de_recurso="tenant",
        recurso_id=contexto.tenant_id,
        metadatos=resumen,
    )
    return Respuesta(data=resumen)


@router.get("/{trabajo_id}", response_model=Respuesta[EscaneoSalida])
async def obtener(
    trabajo_id: UUID, contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[EscaneoSalida]:
    trabajo = await contenedor.consultar_escaneo.obtener(contexto, trabajo_id)
    return Respuesta(data=EscaneoSalida.desde_dominio(trabajo))


@router.post("/{trabajo_id}/cancel", response_model=Respuesta[EscaneoSalida])
async def cancelar(
    trabajo_id: UUID, contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[EscaneoSalida]:
    trabajo = await contenedor.cancelar_escaneo.ejecutar(contexto, trabajo_id)
    await contenedor.auditoria.registrar(
        contexto,
        accion=AccionAuditada.ESCANEO_CANCELADO,
        tipo_de_recurso="scan_job",
        recurso_id=trabajo_id,
    )
    return Respuesta(data=EscaneoSalida.desde_dominio(trabajo))


@router.get("/{trabajo_id}/stream")
async def transmitir_progreso(
    trabajo_id: UUID,
    request: Request,
    contexto: ContextoDep,
    contenedor: ContenedorDep,
) -> StreamingResponse:
    """
    Progreso en vivo por Server-Sent Events.

    La autorizacion se comprueba ANTES de abrir el stream: una vez
    establecido, la conexion queda abierta y ya no hay donde devolver un
    403 que el cliente interprete correctamente.
    """
    trabajo = await contenedor.consultar_escaneo.obtener(contexto, trabajo_id)

    async def generar() -> AsyncIterator[str]:
        yield _evento("estado", _foto(trabajo))

        if not trabajo.esta_activo:
            # Trabajo ya terminado: se entrega la foto y se cierra. Dejar
            # el stream abierto consumiria una conexion para siempre sin
            # que vaya a llegar ningun evento.
            yield _evento("fin", {"trabajo_id": str(trabajo_id)})
            return

        cola: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)

        async def consumir() -> None:
            async for evento in contenedor.progreso.suscribirse(trabajo_id):
                try:
                    cola.put_nowait(evento)
                except asyncio.QueueFull:
                    # Cliente lento: se descarta el evento mas antiguo. El
                    # progreso es acumulativo, asi que perder un paso
                    # intermedio no deja la pantalla en un estado erroneo.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        cola.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        cola.put_nowait(evento)

        tarea = asyncio.create_task(consumir())
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evento = await asyncio.wait_for(cola.get(), timeout=_SEGUNDOS_ENTRE_LATIDOS)
                except TimeoutError:
                    yield ": latido\n\n"  # comentario SSE: mantiene viva la conexion
                    continue

                yield _evento("progreso", evento)
                if evento.get("estado") in (
                    "succeeded",
                    "partial",
                    "failed",
                    "cancelled",
                ):
                    yield _evento("fin", {"trabajo_id": str(trabajo_id)})
                    break
        finally:
            tarea.cancel()

    return StreamingResponse(
        generar(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Desactiva el buffering de nginx, que de lo contrario retiene
            # los eventos hasta llenar su bufer y anula el tiempo real.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{trabajo_id}/errors", response_model=Respuesta[list[ErrorDeProcesamientoSalida]])
async def errores_del_escaneo(
    trabajo_id: UUID,
    contexto: ContextoDep,
    contenedor: ContenedorDep,
    pagina: PaginacionDep,
) -> Respuesta[list[ErrorDeProcesamientoSalida]]:
    resultado = await contenedor.consultar_escaneo.listar_errores(contexto, pagina, trabajo_id)
    return Respuesta(
        data=[ErrorDeProcesamientoSalida.desde_dominio(e) for e in resultado.elementos],
        meta=MetaDePagina(cursor=resultado.siguiente_cursor, hay_mas=resultado.hay_mas),
    )


# ── Auxiliares SSE ───────────────────────────────────────────────────


def _evento(nombre: str, datos: dict[str, Any]) -> str:
    return f"event: {nombre}\ndata: {json.dumps(datos, default=str)}\n\n"


def _foto(trabajo: Any) -> dict[str, Any]:  # noqa: ANN401 - entidad de dominio: tiparla acoplaria la API al modulo
    return {
        "trabajo_id": str(trabajo.id),
        "estado": trabajo.estado.value,
        "fase": trabajo.fase.value,
        "progreso_porcentaje": trabajo.progreso_porcentaje,
        "contadores": trabajo.contadores.como_dict(),
    }
