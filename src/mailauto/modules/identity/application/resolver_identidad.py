"""
Caso de uso: resolver la identidad de una peticion.

Proposito
    Traducir un token ya verificado criptograficamente en el
    `TenantContext` que gobierna el resto de la peticion.

Flujo
    ClaimsVerificados -> usuario (alta diferida si es su primer acceso)
    -> tenant solicitado o unico disponible -> membresia -> rol
    -> TenantContext

Dependencias
    Puerto `RepositorioDeIdentidad`. No conoce la base de datos ni HTTP.

Decision de diseño
    Aprovisionamiento diferido: el usuario se crea en su primer acceso con
    un token valido, en lugar de exigir un alta previa sincronizada con el
    IdP. Evita el problema clasico de dos fuentes de verdad desincronizadas.
    Lo que *no* se crea automaticamente es la membresia: un usuario nuevo
    sin invitacion no entra a ningun tenant, de modo que tener una cuenta
    en el IdP no concede acceso a datos de nadie.
"""

from __future__ import annotations

from uuid import UUID

from mailauto.modules.identity.domain.entities import Usuario
from mailauto.modules.identity.domain.ports import RepositorioDeIdentidad
from mailauto.shared.errors import ErrorDeAutorizacion, RecursoNoEncontrado
from mailauto.shared.security.context import TenantContext
from mailauto.shared.security.jwt_verifier import ClaimsVerificados


class ResolverIdentidad:
    """Convierte claims verificados en contexto de tenant."""

    def __init__(
        self, repositorio: RepositorioDeIdentidad, *, auto_aprovisionar: bool = False
    ) -> None:
        self._repositorio = repositorio
        # Autoservicio: si esta activo, un usuario nuevo sin membresia
        # recibe su propio espacio al iniciar sesion, en vez de quedar
        # sin acceso hasta ser invitado (ver settings).
        self._auto_aprovisionar = auto_aprovisionar

    async def ejecutar(
        self,
        claims: ClaimsVerificados,
        *,
        tenant_solicitado: UUID | None = None,
        ip_origen: str | None = None,
        request_id: str | None = None,
    ) -> TenantContext:
        usuario = await self._obtener_o_crear_usuario(claims)

        if not usuario.esta_activo:
            raise ErrorDeAutorizacion("La cuenta esta suspendida.")

        membresia = await self._resolver_membresia(usuario, tenant_solicitado)

        tenant = await self._repositorio.obtener_tenant(membresia.tenant_id)
        if tenant is None:
            raise RecursoNoEncontrado("El espacio de trabajo no existe.")
        if not tenant.esta_activo:
            raise ErrorDeAutorizacion("El espacio de trabajo esta suspendido.")

        await self._repositorio.registrar_acceso(usuario.id)

        return TenantContext.construir(
            tenant_id=tenant.id,
            user_id=usuario.id,
            external_id=usuario.external_id,
            rol=membresia.rol,
            ip_origen=ip_origen,
            request_id=request_id,
        )

    async def _obtener_o_crear_usuario(self, claims: ClaimsVerificados) -> Usuario:
        existente = await self._repositorio.buscar_usuario_por_external_id(claims.sub)
        if existente is not None:
            return existente

        return await self._repositorio.crear_usuario(
            Usuario(
                external_id=claims.sub,
                email=claims.email or "",
                nombre_visible=claims.email or claims.sub,
            )
        )

    async def _resolver_membresia(self, usuario: Usuario, tenant_solicitado: UUID | None):  # type: ignore[no-untyped-def]
        """
        Determina bajo que tenant opera la peticion.

        Si el cliente pide uno concreto, se verifica la membresia. Si no lo
        pide y solo hay uno, se usa ese. Con varios y sin eleccion
        explicita, se rechaza en vez de adivinar: elegir por el usuario es
        como se filtran datos al tenant equivocado.
        """
        if tenant_solicitado is not None:
            membresia = await self._repositorio.obtener_membresia(usuario.id, tenant_solicitado)
            if membresia is None:
                # 403 y no 404: distinguirlos permitiria enumerar que
                # tenants existen probando identificadores.
                raise ErrorDeAutorizacion()
            return membresia

        membresias = await self._repositorio.listar_membresias(usuario.id)
        if not membresias:
            if self._auto_aprovisionar:
                # Autoservicio: se le da su propio espacio como dueño. El
                # repositorio lo hace de forma idempotente y segura ante
                # peticiones concurrentes.
                return await self._repositorio.aprovisionar_tenant_personal(usuario)
            raise ErrorDeAutorizacion("Tu cuenta no esta asociada a ningun espacio de trabajo.")
        if len(membresias) > 1:
            raise ErrorDeAutorizacion("Indica el espacio de trabajo con la cabecera X-Tenant-Id.")
        return membresias[0]
