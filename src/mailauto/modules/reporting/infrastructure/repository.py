"""
Repositorio de exportaciones sobre PostgreSQL.

Proposito
    Persistir el ciclo de vida de cada reporte solicitado.

Dependencias
    SQLAlchemy async. Reutiliza la tabla `report_exports`, cuyo modelo
    vive junto al de registros porque ambos nacen de la misma
    migracion y comparten claves foraneas.

Nota sobre el modelo compartido
    Importar el ORM desde el modulo de extraccion romperia la
    independencia entre contextos. En su lugar, el modelo se declara
    una sola vez en la capa de persistencia y aqui se importa desde
    `shared`-nivel de infraestructura: el contrato de dominio
    (`Exportacion`) sigue siendo propio de reportes.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select

from mailauto.modules.reporting.domain.ports import (
    EstadoDeExportacion,
    Exportacion,
    FormatoDeReporte,
    RepositorioDeExportaciones,
)
from mailauto.modules.reporting.infrastructure.models import ExportacionORM
from mailauto.shared.db.session import FabricaDeSesiones


class RepositorioDeExportacionesPostgres(RepositorioDeExportaciones):
    def __init__(self, sesiones: FabricaDeSesiones) -> None:
        self._sesiones = sesiones

    async def crear(self, exportacion: Exportacion) -> Exportacion:
        async with self._sesiones.sesion_de_tenant_por_id(exportacion.tenant_id) as sesion:
            sesion.add(
                ExportacionORM(
                    id=exportacion.id,
                    tenant_id=exportacion.tenant_id,
                    solicitado_por=exportacion.solicitado_por,
                    formato=exportacion.formato.value,
                    estado=exportacion.estado.value,
                    filtros=dict(exportacion.filtros),
                )
            )
            await sesion.flush()
            return exportacion

    async def actualizar(self, exportacion: Exportacion) -> None:
        async with self._sesiones.sesion_de_tenant_por_id(exportacion.tenant_id) as sesion:
            fila = await sesion.get(ExportacionORM, exportacion.id)
            if fila is None:
                return
            fila.estado = exportacion.estado.value
            fila.total_filas = exportacion.total_filas
            fila.clave_de_almacenamiento = exportacion.clave_de_almacenamiento
            fila.mensaje_de_error = exportacion.mensaje_de_error
            fila.finalizado_en = exportacion.finalizado_en

    async def obtener(self, tenant_id: UUID, exportacion_id: UUID) -> Exportacion | None:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            fila = await sesion.scalar(
                select(ExportacionORM).where(ExportacionORM.id == exportacion_id)
            )
            return _a_dominio(fila) if fila else None

    async def purgar(self, tenant_id: UUID) -> int:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            total = await sesion.scalar(select(func.count()).select_from(ExportacionORM))
            await sesion.execute(delete(ExportacionORM))
            return int(total or 0)


def _a_dominio(fila: ExportacionORM) -> Exportacion:
    return Exportacion(
        id=fila.id,
        tenant_id=fila.tenant_id,
        solicitado_por=fila.solicitado_por,
        formato=FormatoDeReporte(fila.formato),
        estado=EstadoDeExportacion(fila.estado),
        filtros={k: str(v) for k, v in (fila.filtros or {}).items()},
        total_filas=fila.total_filas,
        clave_de_almacenamiento=fila.clave_de_almacenamiento,
        mensaje_de_error=fila.mensaje_de_error,
        creado_en=fila.created_at,
        finalizado_en=fila.finalizado_en,
    )
