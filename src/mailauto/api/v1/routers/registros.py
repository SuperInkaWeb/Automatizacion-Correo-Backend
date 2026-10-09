"""
Rutas de registros extraidos, revision humana y reportes.

Proposito
    Exponer lo que produce el pipeline: consultarlo, corregir lo
    dudoso y exportarlo.

Dependencias
    FastAPI y los casos de uso ya construidos.

Decision de diseño
    La exportacion responde 202 y no el fichero. Un reporte de miles
    de filas tarda mas de lo que un balanceador espera, y devolverlo
    en la misma peticion funcionaria en desarrollo y fallaria con los
    datos reales de un cliente. El cliente consulta despues y recibe
    una URL prefirmada de vida corta.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from mailauto.api.deps import ContenedorDep, ContextoDep, PaginacionDep
from mailauto.api.schemas.comunes import (
    CorreccionEntrada,
    ExportacionSalida,
    MetaDePagina,
    RegistroSalida,
    Respuesta,
    SolicitarExportacionEntrada,
)
from mailauto.modules.audit.domain.entities import AccionAuditada
from mailauto.modules.extraction.domain.ports import FiltrosDeRegistro
from mailauto.modules.reporting.domain.ports import FormatoDeReporte

router = APIRouter(tags=["registros"])


async def _con_origen(
    contenedor: ContenedorDep, contexto: ContextoDep, registros: list[Any]
) -> list[RegistroSalida]:
    """
    Enriquece registros con su procedencia (correo y adjunto).

    El origen vive en el modulo de ingesta; se trae en UNA consulta por
    lote y se une aqui, en la frontera, para que la UI pueda decir de
    que correo salio cada registro sin que extraccion conozca esas tablas.
    """
    origenes = await contenedor.consultar_origen_de_adjuntos.de_adjuntos(
        contexto, [r.adjunto_id for r in registros]
    )
    return [RegistroSalida.desde_dominio(r, origenes.get(r.adjunto_id)) for r in registros]


# ── Registros ────────────────────────────────────────────────────────


@router.get("/records", response_model=Respuesta[list[RegistroSalida]])
async def listar_registros(
    contexto: ContextoDep,
    contenedor: ContenedorDep,
    pagina: PaginacionDep,
    trabajo_id: UUID | None = None,
    ruc: str | None = Query(default=None, max_length=11, pattern=r"^\d{11}$"),
    periodo: str | None = Query(default=None, max_length=6, pattern=r"^\d{6}$"),
) -> Respuesta[list[RegistroSalida]]:
    """Listado principal, paginado por cursor."""
    resultado = await contenedor.consultar_registros.listar(
        contexto,
        FiltrosDeRegistro(trabajo_id=trabajo_id, ruc=ruc, periodo=periodo),
        pagina,
    )
    return Respuesta(
        data=await _con_origen(contenedor, contexto, resultado.elementos),
        meta=MetaDePagina(cursor=resultado.siguiente_cursor, hay_mas=resultado.hay_mas),
    )


@router.get("/records/{registro_id}", response_model=Respuesta[RegistroSalida])
async def obtener_registro(
    registro_id: UUID, contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[RegistroSalida]:
    registro = await contenedor.consultar_registros.obtener(contexto, registro_id)
    data = await _con_origen(contenedor, contexto, [registro])
    return Respuesta(data=data[0])


# ── Revision humana ──────────────────────────────────────────────────


@router.get("/review", response_model=Respuesta[list[RegistroSalida]])
async def cola_de_revision(
    contexto: ContextoDep, contenedor: ContenedorDep, pagina: PaginacionDep
) -> Respuesta[list[RegistroSalida]]:
    """Registros que el pipeline no pudo dar por buenos."""
    resultado = await contenedor.consultar_registros.cola_de_revision(contexto, pagina)
    return Respuesta(
        data=await _con_origen(contenedor, contexto, resultado.elementos),
        meta=MetaDePagina(cursor=resultado.siguiente_cursor, hay_mas=resultado.hay_mas),
    )


@router.get("/review/count", response_model=Respuesta[dict[str, int]])
async def pendientes_de_revision(
    contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[dict[str, int]]:
    """Contador para el aviso de la interfaz."""
    total = await contenedor.consultar_registros.pendientes(contexto)
    return Respuesta(data={"pendientes": total})


@router.patch("/records/{registro_id}", response_model=Respuesta[RegistroSalida])
async def corregir_registro(
    registro_id: UUID,
    entrada: CorreccionEntrada,
    contexto: ContextoDep,
    contenedor: ContenedorDep,
) -> Respuesta[RegistroSalida]:
    """
    Aplica las correcciones de una persona y aprueba el registro.

    Los valores llegan como texto y pasan por los mismos objetos de
    valor que la extraccion automatica: una persona tambien teclea mal
    un RUC, y aceptarlo sin verificar dejaria entrar justo el error
    que el pipeline existe para evitar.
    """
    registro = await contenedor.revisar_registro.corregir_y_aprobar(
        contexto, registro_id, entrada.correcciones
    )
    await contenedor.auditoria.registrar(
        contexto,
        accion=AccionAuditada.REGISTRO_CORREGIDO,
        tipo_de_recurso="extracted_record",
        recurso_id=registro_id,
        metadatos={"campos": sorted(entrada.correcciones)},
    )
    return Respuesta(data=RegistroSalida.desde_dominio(registro))


@router.post("/records/{registro_id}/approve", response_model=Respuesta[RegistroSalida])
async def aprobar_registro(
    registro_id: UUID, contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[RegistroSalida]:
    """El revisor confirma que lo extraido es correcto."""
    registro = await contenedor.revisar_registro.aprobar_sin_cambios(contexto, registro_id)
    await contenedor.auditoria.registrar(
        contexto,
        accion=AccionAuditada.REGISTRO_APROBADO,
        tipo_de_recurso="extracted_record",
        recurso_id=registro_id,
    )
    return Respuesta(data=RegistroSalida.desde_dominio(registro))


@router.post("/records/{registro_id}/reject", response_model=Respuesta[RegistroSalida])
async def rechazar_registro(
    registro_id: UUID, contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[RegistroSalida]:
    """Marca el registro como no utilizable: documento ilegible o ajeno."""
    registro = await contenedor.revisar_registro.rechazar(contexto, registro_id)
    await contenedor.auditoria.registrar(
        contexto,
        accion=AccionAuditada.REGISTRO_RECHAZADO,
        tipo_de_recurso="extracted_record",
        recurso_id=registro_id,
    )
    return Respuesta(data=RegistroSalida.desde_dominio(registro))


# ── Reportes ─────────────────────────────────────────────────────────


@router.post(
    "/reports/exports",
    response_model=Respuesta[ExportacionSalida],
    status_code=status.HTTP_202_ACCEPTED,
)
async def solicitar_exportacion(
    entrada: SolicitarExportacionEntrada,
    contexto: ContextoDep,
    contenedor: ContenedorDep,
    respuesta: Response,
) -> Respuesta[ExportacionSalida]:
    """Encola la generacion del reporte y devuelve su identificador."""
    filtros = {
        clave: valor
        for clave, valor in (("ruc", entrada.ruc), ("periodo", entrada.periodo))
        if valor
    }
    exportacion = await contenedor.solicitar_exportacion.ejecutar(
        contexto, formato=FormatoDeReporte(entrada.formato), filtros=filtros
    )
    respuesta.headers["Location"] = f"/api/v1/reports/exports/{exportacion.id}"

    await contenedor.auditoria.registrar(
        contexto,
        accion=AccionAuditada.REPORTE_EXPORTADO,
        tipo_de_recurso="report_export",
        recurso_id=exportacion.id,
        metadatos={"formato": entrada.formato, **filtros},
    )
    return Respuesta(data=ExportacionSalida.desde_dominio(exportacion, None))


@router.get("/reports/exports/{exportacion_id}", response_model=Respuesta[ExportacionSalida])
async def consultar_exportacion(
    exportacion_id: UUID, contexto: ContextoDep, contenedor: ContenedorDep
) -> Respuesta[ExportacionSalida]:
    """
    Estado de la exportacion y, cuando esta lista, su URL de descarga.

    La URL vive cinco minutos: lleva los datos tributarios del cliente
    y suele acabar pegada en un chat.
    """
    exportacion, url = await contenedor.consultar_exportacion.ejecutar(contexto, exportacion_id)
    return Respuesta(data=ExportacionSalida.desde_dominio(exportacion, url))
