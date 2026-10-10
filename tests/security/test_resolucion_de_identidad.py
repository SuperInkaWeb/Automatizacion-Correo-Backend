"""
Tests de la resolución de identidad.

Es el punto donde un token verificado se convierte en el `TenantContext`
que gobierna toda la petición. Un error aquí no da un 500: da acceso al
tenant equivocado.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from mailauto.modules.identity.application.resolver_identidad import ResolverIdentidad
from mailauto.modules.identity.domain.entities import (
    EstadoDeCuenta,
    Membresia,
    Tenant,
    Usuario,
)
from mailauto.modules.identity.domain.ports import RepositorioDeIdentidad
from mailauto.shared.errors import ErrorDeAutorizacion, RecursoNoEncontrado
from mailauto.shared.security.context import Permiso, Rol
from mailauto.shared.security.jwt_verifier import ClaimsVerificados
from tests.conftest import TENANT_A, TENANT_B, USUARIO_A

pytestmark = pytest.mark.security


class RepositorioDeIdentidadFalso(RepositorioDeIdentidad):
    def __init__(
        self,
        *,
        usuario: Usuario | None = None,
        membresias: list[Membresia] | None = None,
        tenants: dict[UUID, Tenant] | None = None,
    ) -> None:
        self.usuario = usuario
        self.membresias = membresias or []
        # `if is None` y no `or`: un diccionario vacio es un caso de
        # prueba legitimo (ningun tenant existe) y con `or` caeria en los
        # valores por defecto, haciendo pasar el test por el motivo
        # equivocado.
        self.tenants = (
            tenants
            if tenants is not None
            else {
                TENANT_A: Tenant(id=TENANT_A, nombre="A", slug="a"),
                TENANT_B: Tenant(id=TENANT_B, nombre="B", slug="b"),
            }
        )
        self.creados: list[Usuario] = []
        self.accesos: list[UUID] = []

    async def buscar_usuario_por_external_id(self, external_id: str) -> Usuario | None:
        return self.usuario

    async def crear_usuario(self, usuario: Usuario) -> Usuario:
        self.creados.append(usuario)
        self.usuario = usuario
        return usuario

    async def registrar_acceso(self, user_id: UUID) -> None:
        self.accesos.append(user_id)

    async def listar_membresias(self, user_id: UUID) -> list[Membresia]:
        return self.membresias

    async def obtener_membresia(self, user_id: UUID, tenant_id: UUID) -> Membresia | None:
        return next((m for m in self.membresias if m.tenant_id == tenant_id), None)

    async def obtener_tenant(self, tenant_id: UUID) -> Tenant | None:
        return self.tenants.get(tenant_id)

    async def crear_tenant_con_propietario(
        self, tenant: Tenant, user_id: UUID
    ) -> tuple[Tenant, Membresia]:
        raise NotImplementedError

    async def aprovisionar_tenant_personal(self, usuario: Usuario) -> Membresia:
        if self.membresias:
            return self.membresias[0]
        tenant = Tenant(nombre=usuario.email or "Mi espacio", slug="auto")
        self.tenants[tenant.id] = tenant
        membresia = Membresia(tenant_id=tenant.id, user_id=usuario.id, rol=Rol.OWNER)
        self.membresias.append(membresia)
        return membresia


def _claims(sub: str = "auth0|usuario") -> ClaimsVerificados:
    return ClaimsVerificados(sub=sub, email="persona@ejemplo.com", roles=(), scopes=(), expira_en=0)


def _usuario() -> Usuario:
    return Usuario(id=USUARIO_A, external_id="auth0|usuario", email="persona@ejemplo.com")


def _membresia(tenant_id: UUID, rol: Rol = Rol.OPERATOR) -> Membresia:
    return Membresia(tenant_id=tenant_id, user_id=USUARIO_A, rol=rol)


# ── Camino normal ────────────────────────────────────────────────────


async def test_resuelve_el_unico_tenant_del_usuario() -> None:
    repositorio = RepositorioDeIdentidadFalso(usuario=_usuario(), membresias=[_membresia(TENANT_A)])
    contexto = await ResolverIdentidad(repositorio).ejecutar(_claims())

    assert contexto.tenant_id == TENANT_A
    assert contexto.user_id == USUARIO_A
    assert contexto.rol is Rol.OPERATOR
    assert Permiso.SCAN_RUN in contexto.permisos
    assert repositorio.accesos == [USUARIO_A]


async def test_el_rol_del_contexto_sale_de_la_membresia_no_del_token() -> None:
    """
    Los roles que vienen en el token los controla el proveedor de
    identidad; los permisos efectivos dentro del tenant los decide esta
    aplicacion. Tomarlos del token permitiria escalar privilegios desde
    la configuracion del IdP.
    """
    repositorio = RepositorioDeIdentidadFalso(
        usuario=_usuario(), membresias=[_membresia(TENANT_A, Rol.VIEWER)]
    )
    claims = ClaimsVerificados(
        sub="auth0|usuario",
        email="x@y.z",
        roles=("owner", "admin"),
        scopes=("admin:write",),
        expira_en=0,
    )
    contexto = await ResolverIdentidad(repositorio).ejecutar(claims)

    assert contexto.rol is Rol.VIEWER
    assert Permiso.ADMIN_WRITE not in contexto.permisos


# ── Aprovisionamiento diferido ───────────────────────────────────────


async def test_crea_el_usuario_en_su_primer_acceso() -> None:
    """Evita mantener dos fuentes de verdad sincronizadas con el IdP."""
    repositorio = RepositorioDeIdentidadFalso(usuario=None, membresias=[])
    with pytest.raises(ErrorDeAutorizacion):
        await ResolverIdentidad(repositorio).ejecutar(_claims())

    assert len(repositorio.creados) == 1
    assert repositorio.creados[0].external_id == "auth0|usuario"


async def test_tener_cuenta_en_el_idp_no_concede_acceso_a_ningun_tenant() -> None:
    """
    El usuario se crea solo, pero la membresia no. Si se autoconcediera,
    cualquiera que pudiera registrarse en el IdP entraria a datos ajenos.
    """
    repositorio = RepositorioDeIdentidadFalso(usuario=_usuario(), membresias=[])
    with pytest.raises(ErrorDeAutorizacion, match="espacio de trabajo"):
        await ResolverIdentidad(repositorio).ejecutar(_claims())


async def test_con_autoservicio_un_usuario_nuevo_recibe_su_propio_espacio() -> None:
    """
    Con `auto_aprovisionar` activo, un usuario sin membresia no se queda
    fuera: recibe su propio espacio como dueño y puede usar la aplicacion
    de inmediato. Es seguro porque es un tenant NUEVO y solo suyo, no el
    de otro: el aislamiento por RLS sigue intacto.
    """
    repositorio = RepositorioDeIdentidadFalso(usuario=_usuario(), membresias=[])
    contexto = await ResolverIdentidad(repositorio, auto_aprovisionar=True).ejecutar(_claims())

    assert contexto.rol is Rol.OWNER
    assert contexto.user_id == USUARIO_A
    assert len(repositorio.membresias) == 1
    assert repositorio.accesos == [USUARIO_A]


# ── Selección de tenant ──────────────────────────────────────────────


async def test_con_varios_tenants_exige_elegir_explicitamente() -> None:
    """
    Adivinar por el usuario es como se acaba sirviendo datos del tenant
    equivocado. Mejor pedir la cabecera.
    """
    repositorio = RepositorioDeIdentidadFalso(
        usuario=_usuario(),
        membresias=[_membresia(TENANT_A), _membresia(TENANT_B)],
    )
    with pytest.raises(ErrorDeAutorizacion, match="X-Tenant-Id"):
        await ResolverIdentidad(repositorio).ejecutar(_claims())


async def test_respeta_el_tenant_solicitado_si_hay_membresia() -> None:
    repositorio = RepositorioDeIdentidadFalso(
        usuario=_usuario(),
        membresias=[_membresia(TENANT_A), _membresia(TENANT_B, Rol.ADMIN)],
    )
    contexto = await ResolverIdentidad(repositorio).ejecutar(_claims(), tenant_solicitado=TENANT_B)
    assert contexto.tenant_id == TENANT_B
    assert contexto.rol is Rol.ADMIN


async def test_rechaza_un_tenant_sin_membresia(contexto_b: object) -> None:
    repositorio = RepositorioDeIdentidadFalso(usuario=_usuario(), membresias=[_membresia(TENANT_A)])
    with pytest.raises(ErrorDeAutorizacion):
        await ResolverIdentidad(repositorio).ejecutar(_claims(), tenant_solicitado=TENANT_B)


async def test_un_tenant_ajeno_da_403_y_no_404() -> None:
    """
    Distinguir "no existe" de "no es tuyo" convertiria el codigo de
    estado en un oraculo para enumerar que tenants existen.
    """
    repositorio = RepositorioDeIdentidadFalso(usuario=_usuario(), membresias=[_membresia(TENANT_A)])
    with pytest.raises(ErrorDeAutorizacion) as excinfo:
        await ResolverIdentidad(repositorio).ejecutar(
            _claims(), tenant_solicitado=UUID("00000000-0000-7000-8000-0000000000ff")
        )
    assert excinfo.value.estado_http == 403


# ── Cuentas y tenants suspendidos ────────────────────────────────────


async def test_una_cuenta_suspendida_no_entra() -> None:
    suspendido = _usuario()
    suspendido.estado = EstadoDeCuenta.SUSPENDIDA
    repositorio = RepositorioDeIdentidadFalso(usuario=suspendido, membresias=[_membresia(TENANT_A)])
    with pytest.raises(ErrorDeAutorizacion, match="suspendida"):
        await ResolverIdentidad(repositorio).ejecutar(_claims())


async def test_un_tenant_suspendido_bloquea_a_todos_sus_usuarios() -> None:
    repositorio = RepositorioDeIdentidadFalso(
        usuario=_usuario(),
        membresias=[_membresia(TENANT_A)],
        tenants={
            TENANT_A: Tenant(id=TENANT_A, nombre="A", slug="a", estado=EstadoDeCuenta.SUSPENDIDA)
        },
    )
    with pytest.raises(ErrorDeAutorizacion, match="suspendido"):
        await ResolverIdentidad(repositorio).ejecutar(_claims())


async def test_una_membresia_que_apunta_a_un_tenant_inexistente_da_404() -> None:
    """Dato inconsistente, no intento de acceso: aqui si corresponde 404."""
    repositorio = RepositorioDeIdentidadFalso(
        usuario=_usuario(), membresias=[_membresia(TENANT_A)], tenants={}
    )
    with pytest.raises(RecursoNoEncontrado):
        await ResolverIdentidad(repositorio).ejecutar(_claims())


# ── Trazabilidad ─────────────────────────────────────────────────────


async def test_el_contexto_conserva_ip_y_request_id_para_la_auditoria() -> None:
    repositorio = RepositorioDeIdentidadFalso(usuario=_usuario(), membresias=[_membresia(TENANT_A)])
    contexto = await ResolverIdentidad(repositorio).ejecutar(
        _claims(), ip_origen="203.0.113.7", request_id="abc-123"
    )
    assert contexto.ip_origen == "203.0.113.7"
    assert contexto.request_id == "abc-123"
