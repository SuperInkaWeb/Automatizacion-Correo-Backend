"""
Repositorio de registros extraidos sobre PostgreSQL.

Proposito
    Implementar el puerto `RepositorioDeRegistros` traduciendo entre
    entidades con objetos de valor y filas con columnas tipadas.

Dependencias
    SQLAlchemy async y los modelos del modulo.

Decision de diseño
    El mapeo de vuelta usa `interpretar()` y no los constructores
    estrictos. Una fila guardada hace meses puede llevar un valor que
    hoy ya no validaria (porque se afino una regla, o porque la
    escribio una version anterior); si el mapeo lanzara, el listado
    entero se caeria por una sola fila antigua. Con `interpretar()`,
    ese campo vuelve como None y el resto del registro sigue siendo
    utilizable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, tuple_

from mailauto.modules.extraction.domain.entities import (
    Completitud,
    EstadoDeRevision,
    Estrategia,
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
from mailauto.modules.extraction.infrastructure.persistence.models import (
    RegistroTributarioORM,
)
from mailauto.shared.db.session import FabricaDeSesiones
from mailauto.shared.pagination import Cursor, Pagina, SolicitudDePagina
from mailauto.shared.security.context import TenantContext

# Tope de seguridad del export. Aun con filtros, nadie debe poder
# arrastrar la tabla entera a memoria de un solo golpe.
LIMITE_MAXIMO_DE_EXPORT = 50_000


class RepositorioDeRegistrosPostgres(RepositorioDeRegistros):
    def __init__(self, sesiones: FabricaDeSesiones) -> None:
        self._sesiones = sesiones

    # ── Escritura ────────────────────────────────────────────────────

    async def guardar(self, tenant_id: UUID, registro: RegistroTributario) -> RegistroTributario:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            sesion.add(_a_orm(registro))
            await sesion.flush()
            return registro

    async def actualizar(self, ctx: TenantContext, registro: RegistroTributario) -> None:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            fila = await sesion.get(RegistroTributarioORM, registro.id)
            if fila is None:
                return
            _volcar_en_orm(registro, fila)

    # ── Lectura ──────────────────────────────────────────────────────

    async def obtener(self, ctx: TenantContext, registro_id: UUID) -> RegistroTributario | None:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            fila = await sesion.get(RegistroTributarioORM, registro_id)
            return _a_dominio(fila) if fila else None

    async def listar(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, pagina: SolicitudDePagina
    ) -> Pagina[RegistroTributario]:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            consulta = _aplicar_filtros(select(RegistroTributarioORM), filtros).order_by(
                RegistroTributarioORM.created_at.desc(),
                RegistroTributarioORM.id.desc(),
            )

            cursor = pagina.cursor_decodificado()
            if cursor is not None:
                consulta = consulta.where(
                    tuple_(RegistroTributarioORM.created_at, RegistroTributarioORM.id)
                    < (cursor.creado_en, cursor.identificador)
                )

            filas = list(await sesion.scalars(consulta.limit(pagina.limite + 1)))

            hay_mas = len(filas) > pagina.limite
            visibles = filas[: pagina.limite]
            siguiente = None
            if hay_mas and visibles:
                ultima = visibles[-1]
                siguiente = Cursor(creado_en=ultima.created_at, identificador=ultima.id).codificar()

            return Pagina(
                elementos=[_a_dominio(f) for f in visibles],
                siguiente_cursor=siguiente,
                hay_mas=hay_mas,
            )

    async def listar_para_reporte(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, limite: int
    ) -> list[RegistroTributario]:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            consulta = _aplicar_filtros(select(RegistroTributarioORM), filtros)
            # Orden cronologico ascendente: es como se lee un reporte
            # contable, al reves que el listado de la pantalla.
            consulta = consulta.order_by(
                RegistroTributarioORM.periodo.asc().nullslast(),
                RegistroTributarioORM.fecha_de_pago.asc().nullslast(),
                RegistroTributarioORM.created_at.asc(),
            ).limit(min(limite, LIMITE_MAXIMO_DE_EXPORT))

            filas = await sesion.scalars(consulta)
            return [_a_dominio(f) for f in filas]

    async def existe_para_adjunto(self, tenant_id: UUID, adjunto_id: UUID) -> bool:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            encontrado = await sesion.scalar(
                select(RegistroTributarioORM.id)
                .where(RegistroTributarioORM.adjunto_id == adjunto_id)
                .limit(1)
            )
            return encontrado is not None

    async def contar_pendientes_de_revision(self, ctx: TenantContext) -> int:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            total = await sesion.scalar(
                select(func.count())
                .select_from(RegistroTributarioORM)
                .where(RegistroTributarioORM.estado_de_revision == EstadoDeRevision.PENDIENTE.value)
            )
            return int(total or 0)


# ── Filtros ──────────────────────────────────────────────────────────


def _aplicar_filtros(consulta, filtros: FiltrosDeRegistro):  # type: ignore[no-untyped-def]
    if filtros.trabajo_id is not None:
        consulta = consulta.where(RegistroTributarioORM.trabajo_id == filtros.trabajo_id)
    if filtros.ruc:
        consulta = consulta.where(RegistroTributarioORM.ruc_contribuyente == filtros.ruc)
    if filtros.periodo:
        consulta = consulta.where(RegistroTributarioORM.periodo == filtros.periodo)
    if filtros.solo_pendientes_de_revision:
        consulta = consulta.where(
            RegistroTributarioORM.estado_de_revision == EstadoDeRevision.PENDIENTE.value
        )
    return consulta


# ── Mapeo ────────────────────────────────────────────────────────────


def _a_orm(registro: RegistroTributario) -> RegistroTributarioORM:
    fila = RegistroTributarioORM(
        id=registro.id,
        tenant_id=registro.tenant_id,
        adjunto_id=registro.adjunto_id,
        trabajo_id=registro.trabajo_id,
        perfil=registro.perfil,
    )
    _volcar_en_orm(registro, fila)
    return fila


def _volcar_en_orm(registro: RegistroTributario, fila: RegistroTributarioORM) -> None:
    fila.ruc_contribuyente = str(registro.ruc_contribuyente) if registro.ruc_contribuyente else None
    fila.nombre_contribuyente = registro.nombre_contribuyente[:120]
    fila.ruc_inquilino = str(registro.ruc_inquilino) if registro.ruc_inquilino else None
    fila.nombre_inquilino = registro.nombre_inquilino[:120]
    fila.tipo_doc_inquilino = registro.tipo_doc_inquilino[:40]
    fila.tipo_de_bien = registro.tipo_de_bien[:40]
    fila.periodo = str(registro.periodo) if registro.periodo else None
    fila.fecha_de_pago = registro.fecha_de_pago.valor if registro.fecha_de_pago else None
    fila.numero_de_operacion = (
        str(registro.numero_de_operacion) if registro.numero_de_operacion else None
    )
    fila.monto_alquiler = registro.monto_alquiler.cantidad if registro.monto_alquiler else None
    fila.tributo_resultante = (
        registro.tributo_resultante.cantidad if registro.tributo_resultante else None
    )
    fila.importe_pagado = registro.importe_pagado.cantidad if registro.importe_pagado else None
    fila.intereses_moratorios = (
        registro.intereses_moratorios.cantidad if registro.intereses_moratorios else None
    )
    # Una sola moneda por fila: la del primer monto que la tenga.
    fila.moneda = _moneda_de(registro)
    fila.campos_crudos = dict(registro.campos_crudos)
    fila.confianza_por_campo = dict(registro.confianza_por_campo)
    fila.completitud = registro.completitud.value
    fila.estado_de_revision = registro.estado_de_revision.value
    fila.revisado_por = registro.revisado_por
    fila.revisado_en = registro.revisado_en
    fila.estrategia_usada = registro.estrategia_usada.value if registro.estrategia_usada else None
    fila.duracion_ms = registro.duracion_ms


def _a_dominio(fila: RegistroTributarioORM) -> RegistroTributario:
    return RegistroTributario(
        id=fila.id,
        tenant_id=fila.tenant_id,
        adjunto_id=fila.adjunto_id,
        trabajo_id=fila.trabajo_id,
        perfil=fila.perfil,
        # `interpretar` y no el constructor: una fila antigua con un
        # valor que hoy ya no validaria vuelve con ese campo a None en
        # lugar de tumbar el listado entero.
        ruc_contribuyente=Ruc.interpretar(fila.ruc_contribuyente),
        nombre_contribuyente=fila.nombre_contribuyente,
        ruc_inquilino=Ruc.interpretar(fila.ruc_inquilino),
        nombre_inquilino=fila.nombre_inquilino,
        tipo_doc_inquilino=fila.tipo_doc_inquilino,
        tipo_de_bien=fila.tipo_de_bien,
        periodo=PeriodoTributario.interpretar(fila.periodo),
        fecha_de_pago=_a_fecha(fila.fecha_de_pago),
        numero_de_operacion=NumeroDeOperacion.interpretar(fila.numero_de_operacion),
        monto_alquiler=_a_importe(fila.monto_alquiler, fila.moneda),
        tributo_resultante=_a_importe(fila.tributo_resultante, fila.moneda),
        importe_pagado=_a_importe(fila.importe_pagado, fila.moneda),
        intereses_moratorios=_a_importe(fila.intereses_moratorios, fila.moneda),
        campos_crudos=dict(fila.campos_crudos or {}),
        confianza_por_campo={k: float(v) for k, v in (fila.confianza_por_campo or {}).items()},
        completitud=Completitud(fila.completitud),
        estado_de_revision=EstadoDeRevision(fila.estado_de_revision),
        revisado_por=fila.revisado_por,
        revisado_en=fila.revisado_en,
        estrategia_usada=Estrategia(fila.estrategia_usada) if fila.estrategia_usada else None,
        duracion_ms=fila.duracion_ms,
        creado_en=fila.created_at,
    )


def _a_fecha(valor: date | None) -> FechaDePago | None:
    return FechaDePago(valor) if valor else None


def _a_importe(cantidad: Decimal | None, moneda: str) -> Importe | None:
    if cantidad is None:
        return None
    try:
        return Importe(cantidad, moneda)
    except ValueError:
        # Importe fuera de rango guardado por una version anterior: se
        # descarta el campo en lugar de impedir leer el registro.
        return None


def _moneda_de(registro: RegistroTributario) -> str:
    """La moneda del primer monto que la tenga; PEN si no hay ninguno."""
    for monto in (
        registro.importe_pagado,
        registro.monto_alquiler,
        registro.tributo_resultante,
        registro.intereses_moratorios,
    ):
        if monto is not None:
            return monto.moneda
    return "PEN"
