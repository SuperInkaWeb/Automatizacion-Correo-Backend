"""
Adaptadores entre contextos acotados.

Proposito
    Conectar lo que un modulo necesita con lo que otro ofrece, sin que
    ninguno de los dos se entere del otro.

Dependencias
    Varios modulos a la vez. Forma parte del composition root y es la
    unica excepcion admitida al contrato `modulos-independientes`.

Por que existe este fichero
    Ingesta necesita un token de buzon; extraccion necesita leer un
    adjunto del storage; reportes necesita filas de registros. Si cada
    uno importara al otro, los cuatro contextos acabarian siendo uno
    solo con cuatro carpetas. Cada modulo declara un puerto con lo
    minimo que necesita y aqui se enchufa con quien lo cumple. El
    coste es una clase de cinco lineas; la ventaja es que cada modulo
    se puede testear, mover o sustituir por separado.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from mailauto.modules.extraction.domain.entities import RegistroTributario
from mailauto.modules.extraction.domain.ports import (
    FiltrosDeRegistro,
    LectorDeAdjuntos,
    RepositorioDeRegistros,
)
from mailauto.modules.extraction.domain.value_objects import Importe
from mailauto.modules.ingestion.domain.ports import (
    AlmacenDeObjetos,
    CredencialDeBuzon,
    ProveedorDeCredenciales,
)
from mailauto.modules.mailbox.application.gestionar_buzones import ObtenerTokenVigente
from mailauto.modules.reporting.domain.ports import (
    DestinoDeReportes,
    FilaDeReporte,
    FuenteDeFilas,
)
from mailauto.shared.security.context import Rol, TenantContext


class AdaptadorDeCredenciales(ProveedorDeCredenciales):
    """Ingesta pide un token; buzones sabe conseguirlo y refrescarlo."""

    def __init__(self, obtener_token: ObtenerTokenVigente) -> None:
        self._obtener_token = obtener_token

    async def obtener(self, *, tenant_id: UUID, conexion_id: UUID) -> CredencialDeBuzon:
        conexion = await self._obtener_token.por_tenant(tenant_id, conexion_id)
        return CredencialDeBuzon(
            proveedor=conexion.proveedor.value,
            access_token=conexion.access_token,
            correo_de_la_cuenta=conexion.correo_de_la_cuenta,
        )


class AdaptadorDeLecturaDeAdjuntos(LectorDeAdjuntos):
    """Extraccion pide los bytes de un adjunto; ingesta los tiene en storage."""

    def __init__(self, almacen: AlmacenDeObjetos) -> None:
        self._almacen = almacen

    async def leer(self, tenant_id: UUID, clave: str) -> bytes:
        return await self._almacen.descargar(clave)


class AdaptadorDeDestinoDeReportes(DestinoDeReportes):
    """Reportes deja el fichero donde ingesta ya guarda los adjuntos."""

    def __init__(self, almacen: AlmacenDeObjetos, ttl_por_defecto: int) -> None:
        self._almacen = almacen
        self._ttl = ttl_por_defecto

    async def guardar(self, clave: str, contenido: bytes, tipo_mime: str) -> None:
        await self._almacen.guardar(
            clave=clave,
            contenido=contenido,
            tipo_mime=tipo_mime,
            metadatos={"tipo": "reporte"},
        )

    async def url_de_descarga(self, clave: str, *, ttl_segundos: int) -> str:
        return await self._almacen.url_de_descarga(clave, ttl_segundos=min(ttl_segundos, self._ttl))


class AdaptadorDeFilasDeReporte(FuenteDeFilas):
    """
    Reportes pide filas ya formateadas; extraccion tiene registros con
    objetos de valor.

    El formateo ocurre aqui, en la frontera, y no en el generador: asi
    el Excel y el CSV muestran exactamente lo mismo en lugar de que
    cada uno invente su formato de fecha.
    """

    def __init__(self, repositorio: RepositorioDeRegistros) -> None:
        self._repositorio = repositorio

    async def obtener(
        self, tenant_id: UUID, filtros: dict[str, str], limite: int
    ) -> list[FilaDeReporte]:
        contexto = _contexto_de_lectura(tenant_id)
        registros = await self._repositorio.listar_para_reporte(
            contexto, _filtros_desde_dict(filtros), limite
        )
        return [_a_fila(r) for r in registros]


def _filtros_desde_dict(filtros: dict[str, str]) -> FiltrosDeRegistro:
    """
    Reconstruye los filtros desde el dict que viaja en la exportacion.

    El router los serializo a texto para persistirlos; aqui se vuelven a
    tipar. Un valor ausente o vacio no filtra.
    """

    def fecha(clave: str) -> date | None:
        valor = filtros.get(clave)
        return date.fromisoformat(valor) if valor else None

    ids_crudo = filtros.get("ids") or ""
    ids = tuple(UUID(parte) for parte in ids_crudo.split(",") if parte)

    return FiltrosDeRegistro(
        ruc=filtros.get("ruc") or None,
        ruc_inquilino=filtros.get("ruc_inquilino") or None,
        periodo=filtros.get("periodo") or None,
        periodo_desde=filtros.get("periodo_desde") or None,
        periodo_hasta=filtros.get("periodo_hasta") or None,
        fecha_desde=fecha("fecha_desde"),
        fecha_hasta=fecha("fecha_hasta"),
        solo_aprobados=filtros.get("solo_aprobados") == "true",
        ids=ids,
    )


def _contexto_de_lectura(tenant_id: UUID) -> TenantContext:
    """
    Contexto minimo para que la sesion fije `app.current_tenant`.

    El worker no tiene una peticion HTTP detras, pero el repositorio
    exige contexto por diseño: es lo que garantiza que RLS este activo
    tambien aqui.
    """
    return TenantContext.construir(
        tenant_id=tenant_id,
        user_id=tenant_id,
        external_id="worker:reportes",
        rol=Rol.VIEWER,
    )


def _a_fila(registro: RegistroTributario) -> FilaDeReporte:
    """Convierte un registro extraido en una fila de reporte."""

    def monto(valor: Importe | None) -> str:
        return str(valor) if valor else ""

    montos = (
        registro.importe_pagado,
        registro.monto_alquiler,
        registro.tributo_resultante,
        registro.intereses_moratorios,
    )
    return FilaDeReporte(
        ruc_contribuyente=str(registro.ruc_contribuyente or ""),
        nombre_contribuyente=registro.nombre_contribuyente,
        tipo_doc_inquilino=registro.tipo_doc_inquilino,
        ruc_inquilino=str(registro.ruc_inquilino or ""),
        nombre_inquilino=registro.nombre_inquilino,
        tipo_de_bien=registro.tipo_de_bien,
        periodo=registro.periodo.legible if registro.periodo else "",
        monto_alquiler=monto(registro.monto_alquiler),
        tributo_resultante=monto(registro.tributo_resultante),
        importe_pagado=monto(registro.importe_pagado),
        intereses_moratorios=monto(registro.intereses_moratorios),
        moneda=next((m.moneda for m in montos if m is not None), ""),
        fecha_de_pago=(registro.fecha_de_pago.legible if registro.fecha_de_pago else ""),
        numero_de_operacion=str(registro.numero_de_operacion or ""),
        estado=registro.completitud.value,
        revision=registro.estado_de_revision.value,
        archivo_origen=str(registro.adjunto_id),
    )
