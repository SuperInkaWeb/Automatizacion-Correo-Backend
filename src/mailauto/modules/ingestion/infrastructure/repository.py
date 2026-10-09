"""
Repositorio de ingesta sobre PostgreSQL.

Proposito
    Implementar el puerto `RepositorioDeIngesta`, siempre dentro de una
    sesion con el tenant fijado para que RLS actue.

Dependencias
    SQLAlchemy async y los modelos del modulo.

Decision de diseño
    `guardar_mensaje` y `guardar_adjunto` usan INSERT ... ON CONFLICT DO
    NOTHING y, si no insertan, recuperan la fila existente. Dos workers
    procesando el mismo correo por una reentrega de la cola no pueden
    provocar un IntegrityError que aborte el escaneo entero: el conflicto
    se resuelve en la propia sentencia, sin transaccion fallida.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mailauto.modules.ingestion.domain.entities import (
    Adjunto,
    ContadoresDeEscaneo,
    ErrorDeProcesamiento,
    EstadoDeTrabajo,
    EtapaDeError,
    FaseDeEscaneo,
    MensajeDeCorreo,
    ParametrosDeEscaneo,
    TrabajoDeEscaneo,
)
from mailauto.modules.ingestion.domain.ports import OrigenDeAdjunto, RepositorioDeIngesta
from mailauto.modules.ingestion.infrastructure.models import (
    AdjuntoORM,
    ErrorDeProcesamientoORM,
    MensajeDeCorreoORM,
    TrabajoDeEscaneoORM,
)
from mailauto.shared.db.session import FabricaDeSesiones
from mailauto.shared.pagination import Cursor, Pagina, SolicitudDePagina
from mailauto.shared.security.context import TenantContext

# Genericos del ayudante de paginacion: la fila del ORM y la entidad
# de dominio a la que se mapea. El ayudante no accede a atributos de
# la fila: recibe la extraccion del cursor como funcion, lo que evita
# atarlo a la forma concreta de cada modelo.
TFila = TypeVar("TFila")
TEntidad = TypeVar("TEntidad")

_ESTADOS_ACTIVOS = (EstadoDeTrabajo.EN_COLA.value, EstadoDeTrabajo.EN_EJECUCION.value)


class RepositorioDeIngestaPostgres(RepositorioDeIngesta):
    def __init__(self, sesiones: FabricaDeSesiones) -> None:
        self._sesiones = sesiones

    # ── Trabajos ─────────────────────────────────────────────────────

    async def crear_trabajo(
        self, ctx: TenantContext, trabajo: TrabajoDeEscaneo
    ) -> TrabajoDeEscaneo:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            sesion.add(_a_orm_trabajo(trabajo))
            await sesion.flush()
            return trabajo

    async def actualizar_trabajo(self, tenant_id: UUID, trabajo: TrabajoDeEscaneo) -> None:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            fila = await sesion.get(TrabajoDeEscaneoORM, trabajo.id)
            if fila is None:
                return
            fila.estado = trabajo.estado.value
            fila.fase = trabajo.fase.value
            fila.progreso_porcentaje = trabajo.progreso_porcentaje
            fila.contadores = trabajo.contadores.como_dict()
            fila.intento = trabajo.intento
            fila.codigo_de_error = trabajo.codigo_de_error
            fila.mensaje_de_error = trabajo.mensaje_de_error
            fila.iniciado_en = trabajo.iniciado_en
            fila.finalizado_en = trabajo.finalizado_en

    async def obtener_trabajo(
        self, ctx: TenantContext, trabajo_id: UUID
    ) -> TrabajoDeEscaneo | None:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            fila = await sesion.get(TrabajoDeEscaneoORM, trabajo_id)
            return _a_dominio_trabajo(fila) if fila else None

    async def obtener_trabajo_por_tenant(
        self, tenant_id: UUID, trabajo_id: UUID
    ) -> TrabajoDeEscaneo | None:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            fila = await sesion.get(TrabajoDeEscaneoORM, trabajo_id)
            return _a_dominio_trabajo(fila) if fila else None

    async def listar_trabajos(
        self, ctx: TenantContext, pagina: SolicitudDePagina
    ) -> Pagina[TrabajoDeEscaneo]:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            consulta = select(TrabajoDeEscaneoORM).order_by(
                TrabajoDeEscaneoORM.created_at.desc(), TrabajoDeEscaneoORM.id.desc()
            )
            cursor = pagina.cursor_decodificado()
            if cursor is not None:
                # Comparacion de tupla: resuelve el empate cuando dos filas
                # comparten `created_at` al milisegundo, que con insercion
                # por lotes ocurre mas de lo que parece.
                # `tuple_()` genera una comparacion de fila en SQL. Con
                # una tupla de Python, el operador se evaluaria en
                # Python sobre expresiones de SQLAlchemy y lanzaria
                # TypeError al intentar convertirlas a booleano.
                consulta = consulta.where(
                    tuple_(TrabajoDeEscaneoORM.created_at, TrabajoDeEscaneoORM.id)
                    < (cursor.creado_en, cursor.identificador)
                )
            filas = list(await sesion.scalars(consulta.limit(pagina.limite + 1)))
            return _paginar(filas, pagina.limite, _a_dominio_trabajo, _cursor_de_fila)

    async def buscar_por_idempotencia(
        self, ctx: TenantContext, clave: str
    ) -> TrabajoDeEscaneo | None:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            fila = await sesion.scalar(
                select(TrabajoDeEscaneoORM).where(
                    TrabajoDeEscaneoORM.clave_de_idempotencia == clave
                )
            )
            return _a_dominio_trabajo(fila) if fila else None

    async def contar_trabajos_activos(self, tenant_id: UUID) -> int:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            total = await sesion.scalar(
                select(func.count())
                .select_from(TrabajoDeEscaneoORM)
                .where(TrabajoDeEscaneoORM.estado.in_(_ESTADOS_ACTIVOS))
            )
            return int(total or 0)

    # ── Mensajes y adjuntos ──────────────────────────────────────────

    async def ids_de_mensajes_procesados(
        self, tenant_id: UUID, proveedor: str, ids_candidatos: list[str]
    ) -> set[str]:
        if not ids_candidatos:
            return set()
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            filas = await sesion.scalars(
                select(MensajeDeCorreoORM.id_del_proveedor).where(
                    MensajeDeCorreoORM.proveedor == proveedor,
                    MensajeDeCorreoORM.id_del_proveedor.in_(ids_candidatos),
                )
            )
            return set(filas)

    async def guardar_mensaje(self, tenant_id: UUID, mensaje: MensajeDeCorreo) -> MensajeDeCorreo:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            sentencia = (
                pg_insert(MensajeDeCorreoORM)
                .values(
                    id=mensaje.id,
                    tenant_id=mensaje.tenant_id,
                    trabajo_id=mensaje.trabajo_id,
                    proveedor=mensaje.proveedor,
                    id_del_proveedor=mensaje.id_del_proveedor,
                    remitente=mensaje.remitente[:320],
                    asunto=mensaje.asunto,
                    recibido_en=mensaje.recibido_en,
                )
                .on_conflict_do_nothing(constraint="uq_messages_tenant_provider_id")
                .returning(MensajeDeCorreoORM.id)
            )
            insertado = await sesion.scalar(sentencia)
            if insertado is None:
                # Ya existia (reentrega concurrente): se recupera el id real
                # para que los adjuntos cuelguen del mensaje correcto.
                existente = await sesion.scalar(
                    select(MensajeDeCorreoORM.id).where(
                        MensajeDeCorreoORM.proveedor == mensaje.proveedor,
                        MensajeDeCorreoORM.id_del_proveedor == mensaje.id_del_proveedor,
                    )
                )
                if existente is not None:
                    mensaje.id = existente
            return mensaje

    async def existe_adjunto_con_hash(self, tenant_id: UUID, sha256: str) -> bool:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            encontrado = await sesion.scalar(
                select(AdjuntoORM.id).where(AdjuntoORM.sha256 == sha256).limit(1)
            )
            return encontrado is not None

    async def guardar_adjunto(self, tenant_id: UUID, adjunto: Adjunto) -> Adjunto:
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            sentencia = (
                pg_insert(AdjuntoORM)
                .values(
                    id=adjunto.id,
                    tenant_id=adjunto.tenant_id,
                    mensaje_id=adjunto.mensaje_id,
                    nombre_original=adjunto.nombre_original[:255],
                    clave_de_almacenamiento=adjunto.clave_de_almacenamiento,
                    tipo_mime=adjunto.tipo_mime,
                    tamano_bytes=adjunto.tamano_bytes,
                    sha256=adjunto.sha256,
                    estado_antivirus=adjunto.estado_antivirus,
                )
                .on_conflict_do_nothing(constraint="uq_attachments_tenant_sha")
            )
            await sesion.execute(sentencia)
            return adjunto

    async def origen_de_adjuntos(
        self, ctx: TenantContext, adjunto_ids: list[UUID]
    ) -> dict[UUID, OrigenDeAdjunto]:
        if not adjunto_ids:
            return {}
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            # Un solo JOIN para todo el lote. RLS fija el tenant en ambas
            # tablas, asi que no hace falta filtrarlo a mano.
            consulta = (
                select(
                    AdjuntoORM.id,
                    AdjuntoORM.nombre_original,
                    MensajeDeCorreoORM.remitente,
                    MensajeDeCorreoORM.asunto,
                    MensajeDeCorreoORM.recibido_en,
                )
                .join(MensajeDeCorreoORM, AdjuntoORM.mensaje_id == MensajeDeCorreoORM.id)
                .where(AdjuntoORM.id.in_(adjunto_ids))
            )
            filas = await sesion.execute(consulta)
            return {
                fila.id: OrigenDeAdjunto(
                    nombre_adjunto=fila.nombre_original,
                    remitente=fila.remitente,
                    asunto=fila.asunto,
                    recibido_en=fila.recibido_en,
                )
                for fila in filas
            }

    # ── Errores ──────────────────────────────────────────────────────

    async def guardar_errores(self, tenant_id: UUID, errores: list[ErrorDeProcesamiento]) -> None:
        if not errores:
            return
        async with self._sesiones.sesion_de_tenant_por_id(tenant_id) as sesion:
            sesion.add_all(
                ErrorDeProcesamientoORM(
                    id=error.id,
                    tenant_id=error.tenant_id,
                    trabajo_id=error.trabajo_id,
                    etapa=error.etapa.value,
                    codigo=error.codigo,
                    mensaje=error.mensaje,
                    contexto=error.contexto,
                    reintentable=error.reintentable,
                )
                for error in errores
            )

    async def listar_errores(
        self, ctx: TenantContext, pagina: SolicitudDePagina, trabajo_id: UUID | None = None
    ) -> Pagina[ErrorDeProcesamiento]:
        async with self._sesiones.sesion_de_tenant(ctx) as sesion:
            consulta = select(ErrorDeProcesamientoORM).order_by(
                ErrorDeProcesamientoORM.created_at.desc(), ErrorDeProcesamientoORM.id.desc()
            )
            if trabajo_id is not None:
                consulta = consulta.where(ErrorDeProcesamientoORM.trabajo_id == trabajo_id)
            cursor = pagina.cursor_decodificado()
            if cursor is not None:
                consulta = consulta.where(
                    tuple_(ErrorDeProcesamientoORM.created_at, ErrorDeProcesamientoORM.id)
                    < (cursor.creado_en, cursor.identificador)
                )
            filas = list(await sesion.scalars(consulta.limit(pagina.limite + 1)))
            return _paginar(filas, pagina.limite, _a_dominio_error, _cursor_de_fila)


# ── Mapeo ────────────────────────────────────────────────────────────


def _cursor_de_fila(fila: TrabajoDeEscaneoORM | ErrorDeProcesamientoORM) -> Cursor:
    """Cursor a partir de las columnas de orden compartidas por ambas tablas."""
    return Cursor(creado_en=fila.created_at, identificador=fila.id)


def _paginar(
    filas: list[TFila],
    limite: int,
    mapear: Callable[[TFila], TEntidad],
    cursor_de: Callable[[TFila], Cursor],
) -> Pagina[TEntidad]:
    hay_mas = len(filas) > limite
    visibles = filas[:limite]
    siguiente = None
    if hay_mas and visibles:
        siguiente = cursor_de(visibles[-1]).codificar()
    return Pagina(
        elementos=[mapear(f) for f in visibles],
        siguiente_cursor=siguiente,
        hay_mas=hay_mas,
    )


def _a_orm_trabajo(trabajo: TrabajoDeEscaneo) -> TrabajoDeEscaneoORM:
    return TrabajoDeEscaneoORM(
        id=trabajo.id,
        tenant_id=trabajo.tenant_id,
        solicitado_por=trabajo.solicitado_por,
        conexion_id=trabajo.conexion_id,
        estado=trabajo.estado.value,
        fase=trabajo.fase.value,
        progreso_porcentaje=trabajo.progreso_porcentaje,
        parametros={
            "desde": trabajo.parametros.desde.isoformat() if trabajo.parametros.desde else None,
            "hasta": trabajo.parametros.hasta.isoformat() if trabajo.parametros.hasta else None,
            "limite_de_mensajes": trabajo.parametros.limite_de_mensajes,
            "carpeta": trabajo.parametros.carpeta,
        },
        contadores=trabajo.contadores.como_dict(),
        clave_de_idempotencia=trabajo.clave_de_idempotencia,
        intento=trabajo.intento,
        encolado_en=trabajo.encolado_en,
    )


def _a_dominio_trabajo(fila: TrabajoDeEscaneoORM) -> TrabajoDeEscaneo:
    from datetime import date

    crudos = fila.parametros or {}
    return TrabajoDeEscaneo(
        id=fila.id,
        tenant_id=fila.tenant_id,
        solicitado_por=fila.solicitado_por,
        conexion_id=fila.conexion_id,
        estado=EstadoDeTrabajo(fila.estado),
        fase=FaseDeEscaneo(fila.fase),
        parametros=ParametrosDeEscaneo(
            desde=date.fromisoformat(crudos["desde"]) if crudos.get("desde") else None,
            hasta=date.fromisoformat(crudos["hasta"]) if crudos.get("hasta") else None,
            limite_de_mensajes=int(crudos.get("limite_de_mensajes", 100)),
            carpeta=str(crudos.get("carpeta", "INBOX")),
        ),
        contadores=ContadoresDeEscaneo.desde_dict(fila.contadores),
        progreso_porcentaje=fila.progreso_porcentaje,
        clave_de_idempotencia=fila.clave_de_idempotencia,
        intento=fila.intento,
        codigo_de_error=fila.codigo_de_error,
        mensaje_de_error=fila.mensaje_de_error,
        encolado_en=fila.encolado_en,
        iniciado_en=fila.iniciado_en,
        finalizado_en=fila.finalizado_en,
    )


def _a_dominio_error(fila: ErrorDeProcesamientoORM) -> ErrorDeProcesamiento:
    return ErrorDeProcesamiento(
        id=fila.id,
        tenant_id=fila.tenant_id,
        trabajo_id=fila.trabajo_id,
        etapa=EtapaDeError(fila.etapa),
        codigo=fila.codigo,
        mensaje=fila.mensaje,
        contexto=fila.contexto or {},
        reintentable=fila.reintentable,
        ocurrido_en=fila.created_at,
    )
