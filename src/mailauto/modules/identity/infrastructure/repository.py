"""
Repositorio de identidad sobre PostgreSQL.

Proposito
    Implementar el puerto `RepositorioDeIdentidad` traduciendo entre
    entidades de dominio y modelos ORM.

Dependencias
    SQLAlchemy async, `FabricaDeSesiones`, modelos del propio modulo.

Nota
    Usa `sesion_de_sistema_sin_aislamiento` porque opera sobre las tablas
    que definen el aislamiento. Es uno de los dos unicos lugares del
    sistema donde ese metodo esta justificado (el otro son los jobs de
    mantenimiento del cron).
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mailauto.modules.identity.domain.entities import (
    EstadoDeCuenta,
    Membresia,
    Tenant,
    Usuario,
)
from mailauto.modules.identity.domain.ports import RepositorioDeIdentidad
from mailauto.modules.identity.infrastructure.models import (
    MembresiaORM,
    TenantORM,
    UsuarioORM,
)
from mailauto.shared.db.session import FabricaDeSesiones
from mailauto.shared.security.context import Rol
from mailauto.shared.types import ahora_utc, uuid7


class RepositorioDeIdentidadPostgres(RepositorioDeIdentidad):
    def __init__(self, sesiones: FabricaDeSesiones) -> None:
        self._sesiones = sesiones

    # ── Usuarios ─────────────────────────────────────────────────────

    async def buscar_usuario_por_external_id(self, external_id: str) -> Usuario | None:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            fila = await sesion.scalar(
                select(UsuarioORM).where(UsuarioORM.external_id == external_id)
            )
            return _a_usuario(fila) if fila else None

    async def crear_usuario(self, usuario: Usuario) -> Usuario:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            # ON CONFLICT en vez de INSERT a secas: al cargar el panel se
            # disparan varias peticiones en paralelo y todas resuelven la
            # identidad a la vez. Sin esto, la segunda reventaria por el
            # indice unico de `external_id`. Se inserta o, si ya existe, se
            # relee: en ambos casos se devuelve el mismo usuario.
            await sesion.execute(
                pg_insert(UsuarioORM)
                .values(
                    id=usuario.id,
                    external_id=usuario.external_id,
                    email=usuario.email,
                    nombre_visible=usuario.nombre_visible,
                    estado=usuario.estado.value,
                )
                .on_conflict_do_nothing(index_elements=["external_id"])
            )
            fila = await sesion.scalar(
                select(UsuarioORM).where(UsuarioORM.external_id == usuario.external_id)
            )
            assert fila is not None  # recien insertado o ya existente
            return _a_usuario(fila)

    async def registrar_acceso(self, user_id: UUID) -> None:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            await sesion.execute(
                update(UsuarioORM)
                .where(UsuarioORM.id == user_id)
                .values(ultimo_acceso_en=ahora_utc())
            )

    # ── Membresias ───────────────────────────────────────────────────

    async def listar_membresias(self, user_id: UUID) -> list[Membresia]:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            filas = await sesion.scalars(
                select(MembresiaORM).where(MembresiaORM.user_id == user_id)
            )
            return [_a_membresia(f) for f in filas]

    async def obtener_membresia(self, user_id: UUID, tenant_id: UUID) -> Membresia | None:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            fila = await sesion.scalar(
                select(MembresiaORM).where(
                    MembresiaORM.user_id == user_id,
                    MembresiaORM.tenant_id == tenant_id,
                )
            )
            return _a_membresia(fila) if fila else None

    # ── Tenants ──────────────────────────────────────────────────────

    async def obtener_tenant(self, tenant_id: UUID) -> Tenant | None:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            fila = await sesion.get(TenantORM, tenant_id)
            return _a_tenant(fila) if fila else None

    async def crear_tenant_con_propietario(
        self, tenant: Tenant, user_id: UUID
    ) -> tuple[Tenant, Membresia]:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            fila_tenant = TenantORM(
                id=tenant.id,
                nombre=tenant.nombre,
                slug=tenant.slug,
                estado=tenant.estado.value,
            )
            sesion.add(fila_tenant)
            await sesion.flush()

            membresia = Membresia(tenant_id=tenant.id, user_id=user_id, rol=Rol.OWNER)
            sesion.add(
                MembresiaORM(
                    id=membresia.id,
                    tenant_id=membresia.tenant_id,
                    user_id=membresia.user_id,
                    rol=membresia.rol.value,
                )
            )
            await sesion.flush()
            return _a_tenant(fila_tenant), membresia

    async def aprovisionar_tenant_personal(self, usuario: Usuario) -> Membresia:
        async with self._sesiones.sesion_de_sistema_sin_aislamiento() as sesion:
            # Lock por usuario mientras dura la transaccion: serializa el
            # aprovisionamiento concurrente (varias peticiones del panel a
            # la vez) para no crear dos espacios al mismo usuario. No bloquea
            # a otros usuarios: la clave del lock es distinta para cada uno.
            await sesion.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:clave))"),
                {"clave": f"aprovisionar:{usuario.id}"},
            )

            existente = await sesion.scalar(
                select(MembresiaORM).where(MembresiaORM.user_id == usuario.id).limit(1)
            )
            if existente is not None:
                return _a_membresia(existente)

            tenant_id = uuid7()
            base = _slug_desde(usuario.email or usuario.nombre_visible)
            sesion.add(
                TenantORM(
                    id=tenant_id,
                    nombre=(usuario.nombre_visible or usuario.email or "Mi espacio")[:200],
                    # Sufijo con parte del id: garantiza que el slug es unico
                    # aunque dos usuarios deriven el mismo nombre.
                    slug=f"{base}-{tenant_id.hex[:8]}",
                    estado=EstadoDeCuenta.ACTIVA.value,
                )
            )
            await sesion.flush()

            membresia = Membresia(tenant_id=tenant_id, user_id=usuario.id, rol=Rol.OWNER)
            sesion.add(
                MembresiaORM(
                    id=membresia.id,
                    tenant_id=tenant_id,
                    user_id=usuario.id,
                    rol=Rol.OWNER.value,
                )
            )
            await sesion.flush()
            return membresia


# ── Mapeo ORM -> dominio ─────────────────────────────────────────────


def _slug_desde(texto: str) -> str:
    """Slug a partir de un nombre o correo: minusculas, guiones, acotado."""
    base = re.sub(r"[^a-z0-9]+", "-", (texto or "").lower()).strip("-")
    return base[:60] or "espacio"


def _a_usuario(fila: UsuarioORM) -> Usuario:
    return Usuario(
        id=fila.id,
        external_id=fila.external_id,
        email=fila.email,
        nombre_visible=fila.nombre_visible,
        estado=EstadoDeCuenta(fila.estado),
        ultimo_acceso_en=fila.ultimo_acceso_en,
    )


def _a_tenant(fila: TenantORM) -> Tenant:
    return Tenant(
        id=fila.id,
        nombre=fila.nombre,
        slug=fila.slug,
        estado=EstadoDeCuenta(fila.estado),
        creado_en=fila.created_at,
    )


def _a_membresia(fila: MembresiaORM) -> Membresia:
    return Membresia(
        id=fila.id,
        tenant_id=fila.tenant_id,
        user_id=fila.user_id,
        rol=Rol(fila.rol),
        creado_en=fila.created_at,
    )
