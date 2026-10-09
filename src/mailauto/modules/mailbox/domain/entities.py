"""
Entidades del contexto de buzones.

Proposito
    Modelar la vinculacion OAuth entre un usuario y su proveedor de
    correo, que es la credencial mas sensible que custodia el sistema.

Dependencias
    Solo `shared`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from mailauto.shared.types import ahora_utc, uuid7

# Margen con el que se considera que un token "esta por vencer". Refrescar
# con antelacion evita que un job largo se quede sin credencial a mitad de
# camino, que es cuando mas caro sale el fallo.
MARGEN_DE_REFRESCO = timedelta(minutes=5)


class Proveedor(StrEnum):
    GOOGLE = "google"
    MICROSOFT = "microsoft"


class EstadoDeConexion(StrEnum):
    ACTIVA = "active"
    EXPIRADA = "expired"
    REVOCADA = "revoked"
    CON_ERROR = "error"


# Alcances minimos imprescindibles. Son de solo lectura a proposito: el
# sistema no necesita enviar ni modificar correo, y pedir mas de lo
# necesario amplia el daño de un token comprometido y complica la
# verificacion del proveedor.
ALCANCES_MINIMOS: dict[Proveedor, tuple[str, ...]] = {
    Proveedor.GOOGLE: (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
    ),
    Proveedor.MICROSOFT: (
        "openid",
        "email",
        "offline_access",
        "https://graph.microsoft.com/Mail.Read",
    ),
}


@dataclass(slots=True)
class ConexionDeBuzon:
    """
    Vinculacion activa con el buzon de un usuario.

    Los tokens viven aqui en claro solo mientras la conexion esta en
    memoria durante una operacion. En reposo estan cifrados con la DEK del
    tenant (ver `shared/crypto/envelope.py`).
    """

    id: UUID = field(default_factory=uuid7)
    tenant_id: UUID = field(default_factory=uuid7)
    user_id: UUID = field(default_factory=uuid7)
    proveedor: Proveedor = Proveedor.GOOGLE
    correo_de_la_cuenta: str = ""
    access_token: str = ""
    refresh_token: str | None = None
    expira_en: datetime = field(default_factory=ahora_utc)
    alcances_concedidos: tuple[str, ...] = ()
    estado: EstadoDeConexion = EstadoDeConexion.ACTIVA
    verificada_en: datetime | None = None

    # ── Reglas de negocio ────────────────────────────────────────────

    def necesita_refresco(self, momento: datetime | None = None) -> bool:
        """True si el access token vencio o esta dentro del margen de seguridad."""
        referencia = momento or ahora_utc()
        return self.expira_en - referencia <= MARGEN_DE_REFRESCO

    def puede_refrescarse(self) -> bool:
        """Sin refresh token, la unica salida es que el usuario reconecte."""
        return self.refresh_token is not None and self.estado is not EstadoDeConexion.REVOCADA

    def esta_operativa(self) -> bool:
        return self.estado is EstadoDeConexion.ACTIVA

    def tiene_alcances_suficientes(self) -> bool:
        """
        Verifica que el usuario concedio realmente lo que se pidio.

        En las pantallas de consentimiento granular el usuario puede
        desmarcar permisos. Si no se comprueba, el escaneo falla mas tarde
        con un 403 opaco del proveedor en vez de avisar al vincular.
        """
        requeridos = set(ALCANCES_MINIMOS[self.proveedor])
        concedidos = set(self.alcances_concedidos)
        # Solo se exige el alcance de LECTURA de correo. Los de identidad
        # (openid, email) cada proveedor los devuelve con una forma distinta
        # —Google entrega "email" como ".../userinfo.email"— asi que no
        # deben entrar en la comprobacion. El alcance de lectura es el unico
        # que contiene a la vez "mail" y "read" (gmail.readonly, Mail.Read),
        # lo que lo distingue del scope de identidad "email" (que contiene
        # "mail" pero no "read") y evita un falso negativo que bloqueaba la
        # vinculacion de Gmail pese a haberse concedido el permiso.
        alcance_de_correo = {
            a for a in requeridos if "mail" in a.lower() and "read" in a.lower()
        }
        return alcance_de_correo.issubset(concedidos)

    def marcar_revocada(self) -> None:
        self.estado = EstadoDeConexion.REVOCADA
        self.access_token = ""
        self.refresh_token = None

    def aplicar_tokens_renovados(
        self,
        *,
        access_token: str,
        expira_en: datetime,
        refresh_token: str | None = None,
    ) -> None:
        """
        Aplica el resultado de un refresco.

        Si el proveedor devuelve un refresh token nuevo (rotacion), se
        reemplaza; si no lo devuelve, se conserva el anterior. Descartarlo
        por error dejaria la conexion inutilizable al siguiente vencimiento.
        """
        self.access_token = access_token
        self.expira_en = expira_en
        if refresh_token:
            self.refresh_token = refresh_token
        self.estado = EstadoDeConexion.ACTIVA
        self.verificada_en = ahora_utc()


@dataclass(frozen=True, slots=True)
class TokensDelProveedor:
    """Respuesta normalizada de un intercambio o refresco de tokens."""

    access_token: str
    refresh_token: str | None
    expira_en: datetime
    alcances: tuple[str, ...]
    correo_de_la_cuenta: str | None = None


@dataclass(frozen=True, slots=True)
class SolicitudDeAutorizacion:
    """URL de consentimiento y el secreto PKCE que hay que custodiar."""

    url_de_autorizacion: str
    state: str
    code_verifier: str
