"""
Casos de uso de consulta y revision humana de registros.

Proposito
    Cerrar el ciclo de la extraccion: lo que el pipeline no pudo leer
    con certeza lo corrige una persona, y queda constancia de quien lo
    hizo.

Dependencias
    Puertos del dominio de extraccion.

Decision de diseño
    La correccion pasa por los MISMOS objetos de valor que la
    extraccion automatica. Una persona tambien se equivoca al teclear,
    y aceptar un RUC con el digito verificador mal solo porque lo
    escribio un humano dejaria entrar el error que todo el pipeline
    existe para evitar.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mailauto.modules.extraction.domain.entities import (
    EstadoDeRevision,
    RegistroTributario,
)
from mailauto.modules.extraction.domain.ports import (
    FiltrosDeRegistro,
    RepositorioDeRegistros,
)
from mailauto.modules.extraction.domain.value_objects import (
    FechaDePago,
    Importe,
    NumeroDeOperacion,
    PeriodoTributario,
    Ruc,
)
from mailauto.shared.errors import ErrorDeValidacion, RecursoNoEncontrado
from mailauto.shared.pagination import Pagina, SolicitudDePagina
from mailauto.shared.security.context import Permiso, TenantContext

# Campos corregibles y el objeto de valor que valida cada uno. Lo que
# no esta aqui no se puede corregir: el nombre del perfil, la
# procedencia o la confianza no son datos que una persona deba tocar.
_CORREGIBLES: dict[str, Any] = {
    "ruc_contribuyente": Ruc.interpretar,
    "ruc_inquilino": Ruc.interpretar,
    "periodo": PeriodoTributario.interpretar,
    "fecha_de_pago": FechaDePago.interpretar,
    "numero_de_operacion": NumeroDeOperacion.interpretar,
    "monto_alquiler": Importe.interpretar,
    "tributo_resultante": Importe.interpretar,
    "importe_pagado": Importe.interpretar,
    "intereses_moratorios": Importe.interpretar,
}
# Campos de texto libre: no hay nada que validar mas alla de la longitud.
_CORREGIBLES_DE_TEXTO: frozenset[str] = frozenset(
    {"nombre_contribuyente", "nombre_inquilino", "tipo_doc_inquilino", "tipo_de_bien"}
)
_LONGITUD_MAXIMA_TEXTO = 120


class ConsultarRegistros:
    """Lecturas del listado principal y de la cola de revision."""

    def __init__(self, repositorio: RepositorioDeRegistros) -> None:
        self._repositorio = repositorio

    async def listar(
        self,
        ctx: TenantContext,
        filtros: FiltrosDeRegistro,
        pagina: SolicitudDePagina,
    ) -> Pagina[RegistroTributario]:
        ctx.exigir(Permiso.RECORD_READ)
        return await self._repositorio.listar(ctx, filtros, pagina)

    async def obtener(self, ctx: TenantContext, registro_id: UUID) -> RegistroTributario:
        ctx.exigir(Permiso.RECORD_READ)
        registro = await self._repositorio.obtener(ctx, registro_id)
        if registro is None:
            raise RecursoNoEncontrado("El registro no existe.")
        ctx.exigir_mismo_tenant(registro.tenant_id)
        return registro

    async def cola_de_revision(
        self, ctx: TenantContext, pagina: SolicitudDePagina
    ) -> Pagina[RegistroTributario]:
        ctx.exigir(Permiso.RECORD_REVIEW)
        return await self._repositorio.listar(
            ctx, FiltrosDeRegistro(solo_pendientes_de_revision=True), pagina
        )

    async def pendientes(self, ctx: TenantContext) -> int:
        """Contador para el aviso de la interfaz."""
        ctx.exigir(Permiso.RECORD_READ)
        return await self._repositorio.contar_pendientes_de_revision(ctx)


class RevisarRegistro:
    """Corregir, aprobar o rechazar un registro."""

    def __init__(self, repositorio: RepositorioDeRegistros) -> None:
        self._repositorio = repositorio

    async def corregir_y_aprobar(
        self,
        ctx: TenantContext,
        registro_id: UUID,
        correcciones: dict[str, str],
    ) -> RegistroTributario:
        """
        Aplica los valores corregidos y da el registro por bueno.

        Las correcciones vienen como texto tal cual lo escribio la
        persona; aqui se interpretan con los objetos de valor, igual
        que si las hubiera leido un motor.
        """
        ctx.exigir(Permiso.RECORD_REVIEW)

        registro = await self._repositorio.obtener(ctx, registro_id)
        if registro is None:
            raise RecursoNoEncontrado("El registro no existe.")
        ctx.exigir_mismo_tenant(registro.tenant_id)

        interpretadas = self._interpretar(correcciones)
        registro.aplicar_correccion(ctx.user_id, interpretadas)
        registro.aprobar(ctx.user_id)

        await self._repositorio.actualizar(ctx, registro)
        return registro

    async def rechazar(self, ctx: TenantContext, registro_id: UUID) -> RegistroTributario:
        """Marca el registro como no utilizable: documento ilegible o ajeno."""
        ctx.exigir(Permiso.RECORD_REVIEW)

        registro = await self._repositorio.obtener(ctx, registro_id)
        if registro is None:
            raise RecursoNoEncontrado("El registro no existe.")
        ctx.exigir_mismo_tenant(registro.tenant_id)

        registro.rechazar(ctx.user_id)
        await self._repositorio.actualizar(ctx, registro)
        return registro

    async def aprobar_sin_cambios(
        self, ctx: TenantContext, registro_id: UUID
    ) -> RegistroTributario:
        """El revisor confirma que lo extraido es correcto."""
        ctx.exigir(Permiso.RECORD_REVIEW)

        registro = await self._repositorio.obtener(ctx, registro_id)
        if registro is None:
            raise RecursoNoEncontrado("El registro no existe.")
        ctx.exigir_mismo_tenant(registro.tenant_id)

        if registro.estado_de_revision is EstadoDeRevision.NO_REQUERIDA:
            # No estaba en la cola. No es un error: la interfaz pudo
            # mostrarlo desde el listado general.
            return registro

        registro.aprobar(ctx.user_id)
        await self._repositorio.actualizar(ctx, registro)
        return registro

    # ── Interno ──────────────────────────────────────────────────────

    @staticmethod
    def _interpretar(correcciones: dict[str, str]) -> dict[str, Any]:
        """
        Convierte texto en objetos de valor, rechazando lo que no sea
        interpretable y lo que no sea corregible.
        """
        resultado: dict[str, Any] = {}

        for nombre, crudo in correcciones.items():
            if nombre in _CORREGIBLES_DE_TEXTO:
                resultado[nombre] = crudo.strip()[:_LONGITUD_MAXIMA_TEXTO]
                continue

            interpretar = _CORREGIBLES.get(nombre)
            if interpretar is None:
                # Campo desconocido o no corregible. Se rechaza en vez
                # de ignorarse en silencio: quien corrige debe saber
                # que su cambio no se aplico.
                raise ErrorDeValidacion(f"El campo '{nombre}' no se puede corregir.", campo=nombre)

            valor = interpretar(crudo)
            if valor is None:
                raise ErrorDeValidacion(
                    f"El valor introducido para '{nombre}' no es valido.", campo=nombre
                )
            resultado[nombre] = valor

        if not resultado:
            raise ErrorDeValidacion("No se indico ninguna correccion.")

        return resultado


class EliminarRegistro:
    """Borra un registro extraido."""

    def __init__(self, repositorio: RepositorioDeRegistros) -> None:
        self._repositorio = repositorio

    async def ejecutar(self, ctx: TenantContext, registro_id: UUID) -> None:
        """
        Elimina el registro del tenant.

        Exige el mismo permiso que corregir o rechazar: es una accion de
        quien gestiona los datos, no de solo lectura. Si no existe se
        levanta un 404 en lugar de callar, para que la interfaz no diga
        que borro algo que no estaba.
        """
        ctx.exigir(Permiso.RECORD_REVIEW)
        if not await self._repositorio.eliminar(ctx, registro_id):
            raise RecursoNoEncontrado("El registro no existe.")
