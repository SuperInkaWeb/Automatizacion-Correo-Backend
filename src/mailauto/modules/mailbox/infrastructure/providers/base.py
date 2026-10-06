"""
Base comun de los proveedores OAuth 2.0.

Proposito
    Concentrar el flujo Authorization Code + PKCE, que es identico en
    Google y Microsoft salvo endpoints y detalles de parametros.

Flujo
    construir_autorizacion -> (usuario consiente) -> canjear_codigo
    -> refrescar (periodicamente) -> revocar (al desvincular)

Dependencias
    httpx. No importa nada de infraestructura del proyecto.

Decisiones de diseño
    1. PKCE S256 siempre, aunque usemos client_secret. PKCE protege el
       codigo de autorizacion en transito: si alguien lo intercepta en el
       redirect, no puede canjearlo sin el `code_verifier`, que nunca
       salio del servidor.

    2. `state` de 256 bits de entropia y comparado en tiempo constante.
       Es la defensa contra CSRF en el callback.

    3. Timeout explicito en toda llamada. Sin el, httpx espera
       indefinidamente y un proveedor degradado agota el pool de
       conexiones del worker.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from abc import abstractmethod
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from mailauto.modules.mailbox.domain.entities import (
    ALCANCES_MINIMOS,
    Proveedor,
    SolicitudDeAutorizacion,
    TokensDelProveedor,
)
from mailauto.modules.mailbox.domain.ports import ProveedorOAuth
from mailauto.shared.errors import CredencialesRevocadas, ErrorDeProveedor
from mailauto.shared.observability import metricas
from mailauto.shared.observability.logging import obtener_logger
from mailauto.shared.types import ahora_utc

logger = obtener_logger(__name__)

TIMEOUT_SEGUNDOS = 15.0
_BYTES_DE_ENTROPIA_STATE = 32  # 256 bits
_BYTES_DE_ENTROPIA_VERIFIER = 64  # dentro del rango 43-128 caracteres de RFC 7636


def _b64url_sin_relleno(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode().rstrip("=")


def generar_state() -> str:
    return _b64url_sin_relleno(secrets.token_bytes(_BYTES_DE_ENTROPIA_STATE))


def generar_pkce() -> tuple[str, str]:
    """Devuelve (code_verifier, code_challenge) con el metodo S256."""
    verifier = _b64url_sin_relleno(secrets.token_bytes(_BYTES_DE_ENTROPIA_VERIFIER))
    challenge = _b64url_sin_relleno(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class ProveedorOAuthBase(ProveedorOAuth):
    """Implementa el flujo comun; las subclases aportan endpoints y matices."""

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    # ── A implementar por cada proveedor ─────────────────────────────

    @property
    @abstractmethod
    def _url_autorizacion(self) -> str: ...

    @property
    @abstractmethod
    def _url_token(self) -> str: ...

    @property
    @abstractmethod
    def _url_revocacion(self) -> str: ...

    @abstractmethod
    def _parametros_extra_de_autorizacion(self) -> dict[str, str]:
        """Parametros propios del proveedor (p.ej. `access_type` en Google)."""

    @abstractmethod
    async def _resolver_correo(self, access_token: str) -> str | None:
        """Obtiene el correo del buzon vinculado, para mostrarlo en la UI."""

    # ── Flujo comun ──────────────────────────────────────────────────

    def construir_autorizacion(self, *, redirect_uri: str) -> SolicitudDeAutorizacion:
        state = generar_state()
        verifier, challenge = generar_pkce()

        parametros = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(ALCANCES_MINIMOS[self.nombre]),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **self._parametros_extra_de_autorizacion(),
        }
        return SolicitudDeAutorizacion(
            url_de_autorizacion=f"{self._url_autorizacion}?{urlencode(parametros)}",
            state=state,
            code_verifier=verifier,
        )

    async def canjear_codigo(
        self, *, codigo: str, code_verifier: str, redirect_uri: str
    ) -> TokensDelProveedor:
        datos = {
            "grant_type": "authorization_code",
            "code": codigo,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code_verifier": code_verifier,
        }
        respuesta = await self._pedir_token(datos)
        correo = await self._resolver_correo(respuesta["access_token"])
        return self._normalizar(respuesta, correo)

    async def refrescar(self, refresh_token: str) -> TokensDelProveedor:
        datos = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        respuesta = await self._pedir_token(datos)
        return self._normalizar(respuesta, None)

    async def revocar(self, token: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
                await cliente.post(
                    self._url_revocacion,
                    data={"token": token, "client_id": self._client_id},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            # Un fallo al revocar no debe impedir el borrado local: el
            # usuario pidio desvincular y eso tiene que ocurrir. Pero no se
            # traga en silencio: se registra para que un operador vea que
            # un permiso pudo quedar vivo en el proveedor. No se reintenta
            # —el token no revocado caduca por su cuenta— y no se registra
            # el token en si, solo el hecho y el proveedor.
            logger.warning(
                "revocacion_fallida",
                proveedor=self.nombre.value,
                error=type(exc).__name__,
            )

    # ── Infraestructura interna ──────────────────────────────────────

    async def _pedir_token(self, datos: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
                respuesta = await cliente.post(
                    self._url_token,
                    data=datos,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            # Un fallo de transporte no tiene codigo: se cuenta como `error`
            # para que no desaparezca de la tasa por ser inclasificable.
            metricas.respuestas_de_proveedor.labels(
                proveedor=self.nombre.value, clase="error"
            ).inc()
            raise ErrorDeProveedor(proveedor=self.nombre.value, reintentable=True) from exc

        # Se cuenta antes de interpretar el resultado: asi la metrica refleja
        # lo que el proveedor contesto y no lo que este codigo decide hacer
        # con ello.
        metricas.respuestas_de_proveedor.labels(
            proveedor=self.nombre.value,
            clase=metricas.clase_de_estado(respuesta.status_code),
        ).inc()

        if respuesta.status_code == httpx.codes.BAD_REQUEST:
            cuerpo = self._json_seguro(respuesta)
            codigo_error = cuerpo.get("error", "")
            # `invalid_grant` significa que el usuario revoco el acceso o
            # el refresh token caduco: reintentar no sirve de nada, hay que
            # pedirle que vuelva a conectar.
            if codigo_error in ("invalid_grant", "unauthorized_client"):
                raise CredencialesRevocadas(proveedor=self.nombre.value)
            raise ErrorDeProveedor(
                proveedor=self.nombre.value,
                reintentable=False,
                contexto={"error": codigo_error},
            )

        if respuesta.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise ErrorDeProveedor(proveedor=self.nombre.value, reintentable=True)

        if respuesta.status_code != httpx.codes.OK:
            raise ErrorDeProveedor(
                proveedor=self.nombre.value,
                reintentable=False,
                contexto={"status": respuesta.status_code},
            )

        return self._json_seguro(respuesta)

    @staticmethod
    def _json_seguro(respuesta: httpx.Response) -> dict[str, Any]:
        try:
            cuerpo = respuesta.json()
            return cuerpo if isinstance(cuerpo, dict) else {}
        except ValueError:
            return {}

    def _normalizar(self, respuesta: dict[str, Any], correo: str | None) -> TokensDelProveedor:
        access_token = respuesta.get("access_token")
        if not access_token:
            raise ErrorDeProveedor(proveedor=self.nombre.value, reintentable=False)

        segundos = int(respuesta.get("expires_in", 3600))
        alcances_crudos = respuesta.get("scope", "")
        alcances = tuple(alcances_crudos.split()) if alcances_crudos else ()

        return TokensDelProveedor(
            access_token=str(access_token),
            refresh_token=respuesta.get("refresh_token"),
            expira_en=ahora_utc() + timedelta(seconds=segundos),
            alcances=alcances,
            correo_de_la_cuenta=correo,
        )

    async def _consultar_perfil(self, url: str, access_token: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
                respuesta = await cliente.get(
                    url, headers={"Authorization": f"Bearer {access_token}"}
                )
                if respuesta.status_code != httpx.codes.OK:
                    return {}
                return self._json_seguro(respuesta)
        except httpx.HTTPError:
            # El correo es informativo para la UI; que falte no justifica
            # abortar una vinculacion por lo demas correcta.
            return {}


class ProveedorGoogle(ProveedorOAuthBase):
    """Google OAuth 2.0 (Gmail API)."""

    @property
    def nombre(self) -> Proveedor:
        return Proveedor.GOOGLE

    @property
    def _url_autorizacion(self) -> str:
        return "https://accounts.google.com/o/oauth2/v2/auth"

    @property
    def _url_token(self) -> str:
        return "https://oauth2.googleapis.com/token"

    @property
    def _url_revocacion(self) -> str:
        return "https://oauth2.googleapis.com/revoke"

    def _parametros_extra_de_autorizacion(self) -> dict[str, str]:
        return {
            # `offline` es lo que hace que Google entregue refresh token.
            "access_type": "offline",
            # `consent` fuerza la pantalla aunque ya haya consentimiento
            # previo: sin esto, al reconectar Google omite el refresh token
            # y la conexion muere al primer vencimiento.
            "prompt": "consent",
            "include_granted_scopes": "false",
        }

    async def _resolver_correo(self, access_token: str) -> str | None:
        perfil = await self._consultar_perfil(
            "https://www.googleapis.com/oauth2/v3/userinfo", access_token
        )
        correo = perfil.get("email")
        return str(correo) if correo else None


class ProveedorMicrosoft(ProveedorOAuthBase):
    """Microsoft identity platform v2.0 (Microsoft Graph)."""

    def __init__(self, *, client_id: str, client_secret: str, tenant_id: str = "common") -> None:
        super().__init__(client_id=client_id, client_secret=client_secret)
        self._tenant = tenant_id

    @property
    def nombre(self) -> Proveedor:
        return Proveedor.MICROSOFT

    @property
    def _url_autorizacion(self) -> str:
        return f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/authorize"

    @property
    def _url_token(self) -> str:
        return f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token"

    @property
    def _url_revocacion(self) -> str:
        # Microsoft no expone un endpoint RFC 7009; la revocacion efectiva
        # se hace desde el portal del usuario. Se apunta al logout para
        # cerrar la sesion delegada que si controlamos.
        return f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/logout"

    def _parametros_extra_de_autorizacion(self) -> dict[str, str]:
        return {"response_mode": "query", "prompt": "select_account"}

    async def _resolver_correo(self, access_token: str) -> str | None:
        perfil = await self._consultar_perfil(
            "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName",
            access_token,
        )
        correo = perfil.get("mail") or perfil.get("userPrincipalName")
        return str(correo) if correo else None
