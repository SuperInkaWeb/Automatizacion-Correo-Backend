"""
Puertos del contexto de identidad.

Proposito
    Definir el contrato de persistencia que necesita la capa de
    aplicacion, sin decir nada sobre como se implementa.

Dependencias
    Solo las entidades del propio dominio.

Nota sobre aislamiento
    Este es el unico repositorio que opera sin `TenantContext`: resuelve
    precisamente *cual* es el tenant, asi que no puede exigirlo de entrada.
    Por eso sus tablas (`tenants`, `users`, `memberships`) no llevan RLS
    por tenant y el acceso se restringe por otra via: solo la capa de
    identidad las consulta, siempre filtrando por `external_id` del token
    ya verificado criptograficamente.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from mailauto.modules.identity.domain.entities import Membresia, Tenant, Usuario


class RepositorioDeIdentidad(ABC):
    """Persistencia de usuarios, tenants y membresias."""

    @abstractmethod
    async def buscar_usuario_por_external_id(self, external_id: str) -> Usuario | None:
        """Devuelve el usuario asociado al `sub` del IdP, o None si no existe."""

    @abstractmethod
    async def crear_usuario(self, usuario: Usuario) -> Usuario:
        """Alta de usuario en el primer acceso (aprovisionamiento diferido)."""

    @abstractmethod
    async def registrar_acceso(self, user_id: UUID) -> None:
        """Actualiza la marca de ultimo acceso."""

    @abstractmethod
    async def listar_membresias(self, user_id: UUID) -> list[Membresia]:
        """Todas las membresias del usuario, en todos sus tenants."""

    @abstractmethod
    async def obtener_membresia(self, user_id: UUID, tenant_id: UUID) -> Membresia | None:
        """Membresia concreta, o None si el usuario no pertenece a ese tenant."""

    @abstractmethod
    async def obtener_tenant(self, tenant_id: UUID) -> Tenant | None:
        """Tenant por id."""

    @abstractmethod
    async def crear_tenant_con_propietario(
        self, tenant: Tenant, user_id: UUID
    ) -> tuple[Tenant, Membresia]:
        """
        Crea un tenant y su membresia de propietario en una sola
        transaccion. Son indivisibles: un tenant sin dueño seria
        inaccesible y permaneceria huerfano.
        """

    @abstractmethod
    async def aprovisionar_tenant_personal(self, usuario: Usuario) -> Membresia:
        """
        Da al usuario un espacio propio como dueño, de forma idempotente.

        Pensado para el autoservicio: lo llama el resolver cuando un
        usuario nuevo sin membresia inicia sesion. Debe ser seguro ante
        llamadas concurrentes (el panel dispara varias peticiones al
        cargar): si el usuario ya tiene membresia, la devuelve en vez de
        crear un segundo espacio.
        """
