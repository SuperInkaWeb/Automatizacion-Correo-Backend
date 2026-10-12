"""
Reinicio de los datos de escaneo de un tenant.

Proposito
    Dejar el espacio como recien creado para poder re-escanear los
    correos desde cero: borra registros, adjuntos, correos, trabajos,
    errores y reportes, pero CONSERVA los buzones conectados. Es lo que
    permite que un escaneo nuevo vuelva a procesar todo (la deduplicacion
    por contenido, si no, hace que un re-escaneo no reprocese nada).

Decisiones de diseño
    1. Orquesta la purga de tres contextos (extraccion, ingesta,
       reportes) sin importar sus puertos: depende de Protocols
       estructurales que el composition root satisface con los
       repositorios concretos. Asi no se acoplan los contextos entre si.

    2. Los registros se borran ANTES que los adjuntos: hay una clave
       foranea entre ambos y el orden inverso fallaria.

    3. El borrado de objetos del almacen es best-effort y va al final,
       con la base ya consistente. Un fallo ahi solo deja objetos
       huerfanos (gasto de espacio), nunca datos a medias que rompan la
       pantalla; y no puede impedir el reinicio, que es lo que se pidio.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from mailauto.shared.observability.logging import obtener_logger
from mailauto.shared.security.context import Permiso, TenantContext

logger = obtener_logger(__name__)


class PurgaDeRegistros(Protocol):
    async def purgar(self, ctx: TenantContext) -> int: ...


class PurgaDeIngesta(Protocol):
    async def claves_de_almacenamiento(self, ctx: TenantContext) -> list[str]: ...
    async def purgar_datos_de_escaneo(self, ctx: TenantContext) -> dict[str, int]: ...


class PurgaDeExportaciones(Protocol):
    async def purgar(self, tenant_id: UUID) -> int: ...


class BorradoDeObjetos(Protocol):
    async def eliminar(self, clave: str) -> None: ...


class ReiniciarDatosDeEscaneo:
    """Borra los datos de escaneo del tenant, conservando los buzones."""

    def __init__(
        self,
        *,
        registros: PurgaDeRegistros,
        ingesta: PurgaDeIngesta,
        exportaciones: PurgaDeExportaciones,
        almacen: BorradoDeObjetos,
    ) -> None:
        self._registros = registros
        self._ingesta = ingesta
        self._exportaciones = exportaciones
        self._almacen = almacen

    async def ejecutar(self, ctx: TenantContext) -> dict[str, int]:
        ctx.exigir(Permiso.ADMIN_WRITE)

        # Se leen las claves ANTES de borrar las filas: despues ya no
        # existirian y los objetos quedarian huerfanos sin remedio.
        claves = await self._ingesta.claves_de_almacenamiento(ctx)

        # Base de datos primero y en orden hijo -> padre entre contextos.
        registros = await self._registros.purgar(ctx)
        conteo_ingesta = await self._ingesta.purgar_datos_de_escaneo(ctx)
        exportaciones = await self._exportaciones.purgar(ctx.tenant_id)

        # Objetos del almacen al final, best-effort.
        objetos = 0
        for clave in claves:
            try:
                await self._almacen.eliminar(clave)
                objetos += 1
            except Exception:  # noqa: BLE001 - limpieza best-effort, ya detallada arriba
                logger.warning("fallo_al_borrar_objeto_de_almacen")

        resumen = {"registros": registros, "exportaciones": exportaciones, "objetos": objetos}
        resumen.update(conteo_ingesta)
        logger.info("datos_de_escaneo_reiniciados", **resumen)
        return resumen
