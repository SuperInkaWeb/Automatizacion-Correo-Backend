"""
Adaptadores de proveedores de correo (Gmail API y Microsoft Graph).

Proposito
    Implementar `ProveedorDeCorreo` para los dos buzones soportados,
    normalizando sus diferencias y respetando sus limites de tasa.

Dependencias
    httpx.

Decisiones de diseño
    1. Reintento con backoff exponencial que respeta `Retry-After`.
       Ignorar esa cabecera y reintentar de inmediato es lo que provoca
       que el proveedor amplie la penalizacion o suspenda la aplicacion.

    2. Tope de bytes aplicado sobre la respuesta HTTP en curso, no sobre
       el resultado. Gmail devuelve el adjunto como base64 dentro de un
       JSON, asi que ahi el tope se aplica al cuerpo crudo con el factor
       de expansion de base64 (4/3). Graph sirve bytes directos y se
       corta por trozo.

    3. Ningun adaptador decide que hacer ante un error: traduce la
       respuesta del proveedor a los errores del dominio y deja la
       decision al pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import random
from abc import abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any, Final

import httpx

from mailauto.modules.ingestion.domain.ports import (
    ProveedorDeCorreo,
    ReferenciaDeAdjunto,
    ResumenDeMensaje,
)
from mailauto.shared.errors import (
    CredencialesRevocadas,
    ErrorDeProveedor,
    ProveedorSaturado,
)
from mailauto.shared.observability.logging import obtener_logger
from mailauto.shared.types import asegurar_utc

logger = obtener_logger(__name__)

_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)
_MAXIMO_REINTENTOS: Final = 4
_ESPERA_BASE_SEGUNDOS: Final = 1.0
_ESPERA_MAXIMA_SEGUNDOS: Final = 30.0
_TAMANO_DE_TROZO: Final = 64 * 1024
# base64 expande 4 bytes por cada 3: el tope sobre el cuerpo crudo debe
# contemplarlo, mas un margen para la envoltura JSON.
_FACTOR_BASE64: Final = 1.40


class ProveedorDeCorreoHttp(ProveedorDeCorreo):
    """Base con el manejo de reintentos y errores comun a ambos proveedores."""

    @property
    @abstractmethod
    def _nombre(self) -> str: ...

    async def _peticion(
        self,
        cliente: httpx.AsyncClient,
        metodo: str,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET/POST con reintentos. Devuelve el JSON ya decodificado."""
        for intento in range(_MAXIMO_REINTENTOS):
            try:
                respuesta = await cliente.request(
                    metodo,
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                if intento == _MAXIMO_REINTENTOS - 1:
                    raise ErrorDeProveedor(proveedor=self._nombre, reintentable=True) from exc
                await self._esperar(intento, None)
                continue

            if respuesta.status_code == httpx.codes.OK:
                try:
                    cuerpo = respuesta.json()
                    return cuerpo if isinstance(cuerpo, dict) else {}
                except ValueError as exc:
                    raise ErrorDeProveedor(proveedor=self._nombre, reintentable=False) from exc

            self._traducir_error(respuesta, intento)
            await self._esperar(intento, respuesta.headers.get("Retry-After"))

        raise ErrorDeProveedor(proveedor=self._nombre, reintentable=True)

    def _traducir_error(self, respuesta: httpx.Response, intento: int) -> None:
        """Lanza el error de dominio correspondiente, o retorna para reintentar."""
        codigo = respuesta.status_code

        if codigo in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            # El token dejo de valer a mitad de la operacion: el usuario
            # revoco el acceso o cambio su contraseña. Reintentar es inutil.
            raise CredencialesRevocadas(proveedor=self._nombre)

        if codigo == httpx.codes.TOO_MANY_REQUESTS:
            if intento == _MAXIMO_REINTENTOS - 1:
                espera = self._segundos_de_retry_after(respuesta.headers.get("Retry-After"))
                raise ProveedorSaturado(proveedor=self._nombre, reintentar_en_segundos=espera or 60)
            return  # reintentable

        if codigo >= httpx.codes.INTERNAL_SERVER_ERROR:
            if intento == _MAXIMO_REINTENTOS - 1:
                raise ErrorDeProveedor(proveedor=self._nombre, reintentable=True)
            return  # reintentable

        raise ErrorDeProveedor(
            proveedor=self._nombre,
            reintentable=False,
            contexto={"status": codigo},
        )

    @staticmethod
    def _segundos_de_retry_after(valor: str | None) -> int | None:
        if not valor:
            return None
        try:
            return max(1, int(float(valor)))
        except ValueError:
            return None

    async def _esperar(self, intento: int, retry_after: str | None) -> None:
        """
        Backoff exponencial con jitter, acotado por `Retry-After`.

        El jitter evita que todos los workers que recibieron 429 a la vez
        vuelvan a golpear el proveedor en el mismo instante (efecto manada).
        """
        indicado = self._segundos_de_retry_after(retry_after)
        if indicado is not None:
            await asyncio.sleep(min(indicado, _ESPERA_MAXIMA_SEGUNDOS))
            return
        base = min(_ESPERA_BASE_SEGUNDOS * (2**intento), _ESPERA_MAXIMA_SEGUNDOS)
        await asyncio.sleep(base * (0.5 + random.random() / 2))  # noqa: S311 - no es criptografico


# ─────────────────────────────────────────────────────────────────────
# Gmail
# ─────────────────────────────────────────────────────────────────────


class ProveedorGmail(ProveedorDeCorreoHttp):
    _BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

    @property
    def _nombre(self) -> str:
        return "google"

    async def listar_mensajes(
        self,
        access_token: str,
        *,
        desde: date | None,
        hasta: date | None,
        limite: int,
        carpeta: str,
    ) -> AsyncIterator[ResumenDeMensaje]:
        consulta = self._construir_consulta(desde, hasta, carpeta)
        entregados = 0
        token_de_pagina: str | None = None

        async with httpx.AsyncClient(timeout=_TIMEOUT) as cliente:
            while entregados < limite:
                params: dict[str, Any] = {
                    "q": consulta,
                    "maxResults": min(100, limite - entregados),
                }
                if token_de_pagina:
                    params["pageToken"] = token_de_pagina

                pagina = await self._peticion(
                    cliente,
                    "GET",
                    f"{self._BASE}/messages",
                    access_token=access_token,
                    params=params,
                )
                mensajes = pagina.get("messages", [])
                if not mensajes:
                    return

                for referencia in mensajes:
                    if entregados >= limite:
                        return
                    detalle = await self._peticion(
                        cliente,
                        "GET",
                        f"{self._BASE}/messages/{referencia['id']}",
                        access_token=access_token,
                        params={
                            "format": "metadata",
                            "metadataHeaders": ["From", "Subject", "Date"],
                        },
                    )
                    entregados += 1
                    yield self._a_resumen(referencia["id"], detalle)

                token_de_pagina = pagina.get("nextPageToken")
                if not token_de_pagina:
                    return

    async def listar_adjuntos(
        self, access_token: str, id_del_mensaje: str
    ) -> list[ReferenciaDeAdjunto]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cliente:
            detalle = await self._peticion(
                cliente,
                "GET",
                f"{self._BASE}/messages/{id_del_mensaje}",
                access_token=access_token,
                params={"format": "full"},
            )
        return self._recorrer_partes(detalle.get("payload", {}))

    async def descargar_adjunto(
        self,
        access_token: str,
        *,
        id_del_mensaje: str,
        id_del_adjunto: str,
        limite_de_bytes: int,
    ) -> AsyncIterator[bytes]:
        # Gmail entrega el adjunto en base64url dentro de un JSON, asi que
        # el corte se aplica al cuerpo crudo ajustado por la expansion de
        # base64. No hay forma de pedirle bytes en bruto.
        tope_crudo = int(limite_de_bytes * _FACTOR_BASE64) + 1024
        url = f"{self._BASE}/messages/{id_del_mensaje}/attachments/{id_del_adjunto}"

        async with (
            httpx.AsyncClient(timeout=_TIMEOUT) as cliente,
            cliente.stream(
                "GET", url, headers={"Authorization": f"Bearer {access_token}"}
            ) as respuesta,
        ):
            if respuesta.status_code != httpx.codes.OK:
                await respuesta.aread()
                self._traducir_error(respuesta, _MAXIMO_REINTENTOS - 1)
                return

            crudo = bytearray()
            async for trozo in respuesta.aiter_bytes(_TAMANO_DE_TROZO):
                crudo.extend(trozo)
                if len(crudo) > tope_crudo:
                    # Se aborta la descarga en curso: el `return` cierra
                    # el stream y la conexion no sigue transfiriendo.
                    return

        yield self._decodificar(bytes(crudo))

    # ── Auxiliares de Gmail ──────────────────────────────────────────

    @staticmethod
    def _construir_consulta(desde: date | None, hasta: date | None, carpeta: str) -> str:
        partes = ["has:attachment"]
        if carpeta and carpeta.upper() != "INBOX":
            # El nombre de carpeta se entrecomilla: si llevara espacios o
            # dos puntos alteraria la sintaxis de la consulta de Gmail.
            partes.append(f'label:"{carpeta}"')
        else:
            partes.append("in:inbox")
        if desde:
            partes.append(f"after:{desde:%Y/%m/%d}")
        if hasta:
            partes.append(f"before:{hasta:%Y/%m/%d}")
        return " ".join(partes)

    @staticmethod
    def _a_resumen(identificador: str, detalle: dict[str, Any]) -> ResumenDeMensaje:
        cabeceras = {
            c.get("name", "").lower(): c.get("value", "")
            for c in detalle.get("payload", {}).get("headers", [])
        }
        recibido = None
        marca = detalle.get("internalDate")
        if marca:
            try:
                # internalDate viene en milisegundos desde epoch, en UTC.
                # Sin `tz` explicito se interpretaria en la zona del
                # servidor y la fecha del correo se desplazaria segun
                # donde corra el worker.
                recibido = datetime.fromtimestamp(int(marca) / 1000, tz=UTC)
            except (ValueError, OSError):
                recibido = None
        return ResumenDeMensaje(
            id_del_proveedor=identificador,
            remitente=cabeceras.get("from", "")[:320],
            asunto=cabeceras.get("subject", ""),
            recibido_en=recibido,
        )

    @staticmethod
    def _recorrer_partes(parte: dict[str, Any]) -> list[ReferenciaDeAdjunto]:
        """Recorre el arbol MIME buscando partes con `attachmentId`."""
        resultado: list[ReferenciaDeAdjunto] = []

        def recorrer(nodo: dict[str, Any], profundidad: int = 0) -> None:
            # Tope de profundidad: un MIME anidado maliciosamente podria
            # provocar recursion sin fin.
            if profundidad > 10:
                return
            cuerpo = nodo.get("body", {})
            id_adjunto = cuerpo.get("attachmentId")
            nombre = nodo.get("filename", "")
            if id_adjunto and nombre:
                resultado.append(
                    ReferenciaDeAdjunto(
                        id_del_adjunto=id_adjunto,
                        nombre=nombre,
                        tipo_mime_declarado=nodo.get("mimeType", ""),
                        tamano_declarado=int(cuerpo.get("size", 0)),
                    )
                )
            for hijo in nodo.get("parts", []):
                recorrer(hijo, profundidad + 1)

        recorrer(parte)
        return resultado

    @staticmethod
    def _decodificar(crudo: bytes) -> bytes:
        import json

        try:
            cuerpo = json.loads(crudo)
            datos = cuerpo.get("data", "")
            relleno = "=" * (-len(datos) % 4)
            return base64.urlsafe_b64decode(datos + relleno)
        except (ValueError, binascii.Error) as exc:
            raise ErrorDeProveedor(
                proveedor="google", reintentable=False, contexto={"etapa": "decodificar"}
            ) from exc


# ─────────────────────────────────────────────────────────────────────
# Microsoft Graph
# ─────────────────────────────────────────────────────────────────────


class ProveedorMicrosoftGraph(ProveedorDeCorreoHttp):
    _BASE = "https://graph.microsoft.com/v1.0/me"

    @property
    def _nombre(self) -> str:
        return "microsoft"

    async def listar_mensajes(
        self,
        access_token: str,
        *,
        desde: date | None,
        hasta: date | None,
        limite: int,
        carpeta: str,
    ) -> AsyncIterator[ResumenDeMensaje]:
        filtros = ["hasAttachments eq true"]
        if desde:
            filtros.append(f"receivedDateTime ge {desde:%Y-%m-%d}T00:00:00Z")
        if hasta:
            filtros.append(f"receivedDateTime le {hasta:%Y-%m-%d}T23:59:59Z")

        url = f"{self._BASE}/mailFolders/{carpeta or 'inbox'}/messages"
        # Sin `$orderby`: Graph rechaza (400) combinar un `$filter` sobre
        # `hasAttachments` con un `$orderby` por `receivedDateTime`, porque
        # el orden y el filtro caen sobre propiedades distintas. No hace
        # falta pedirlo: Graph ya devuelve los mensajes de una carpeta en
        # orden descendente por `receivedDateTime` por defecto, que es justo
        # el que se quiere (del mas reciente al mas antiguo).
        params: dict[str, Any] | None = {
            "$filter": " and ".join(filtros),
            "$select": "id,subject,from,receivedDateTime",
            "$top": min(50, limite),
        }
        entregados = 0

        async with httpx.AsyncClient(timeout=_TIMEOUT) as cliente:
            while url and entregados < limite:
                pagina = await self._peticion(
                    cliente, "GET", url, access_token=access_token, params=params
                )
                for crudo in pagina.get("value", []):
                    if entregados >= limite:
                        return
                    entregados += 1
                    yield self._a_resumen(crudo)

                # `@odata.nextLink` ya trae todos los parametros embebidos:
                # volver a enviarlos duplicaria el filtro y Graph lo rechaza.
                url = pagina.get("@odata.nextLink", "")
                params = None

    async def listar_adjuntos(
        self, access_token: str, id_del_mensaje: str
    ) -> list[ReferenciaDeAdjunto]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cliente:
            respuesta = await self._peticion(
                cliente,
                "GET",
                f"{self._BASE}/messages/{id_del_mensaje}/attachments",
                access_token=access_token,
                params={"$select": "id,name,contentType,size"},
            )

        referencias = []
        for crudo in respuesta.get("value", []):
            # Solo adjuntos de fichero: `itemAttachment` (un correo
            # incrustado) y `referenceAttachment` (un enlace a OneDrive)
            # no son documentos y el segundo implicaria salir a buscar una
            # URL externa, que es exactamente el patron SSRF a evitar.
            if crudo.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            referencias.append(
                ReferenciaDeAdjunto(
                    id_del_adjunto=str(crudo.get("id", "")),
                    nombre=str(crudo.get("name", "")),
                    tipo_mime_declarado=str(crudo.get("contentType", "")),
                    tamano_declarado=int(crudo.get("size", 0)),
                )
            )
        return referencias

    async def descargar_adjunto(
        self,
        access_token: str,
        *,
        id_del_mensaje: str,
        id_del_adjunto: str,
        limite_de_bytes: int,
    ) -> AsyncIterator[bytes]:
        # Graph sirve los bytes en bruto en `/$value`: se puede cortar
        # trozo a trozo sin factor de correccion.
        url = f"{self._BASE}/messages/{id_del_mensaje}/attachments/{id_del_adjunto}/$value"
        acumulado = 0

        async with (
            httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cliente,
            cliente.stream(
                "GET", url, headers={"Authorization": f"Bearer {access_token}"}
            ) as respuesta,
        ):
            if respuesta.status_code != httpx.codes.OK:
                await respuesta.aread()
                self._traducir_error(respuesta, _MAXIMO_REINTENTOS - 1)
                return

            async for trozo in respuesta.aiter_bytes(_TAMANO_DE_TROZO):
                acumulado += len(trozo)
                if acumulado > limite_de_bytes:
                    return
                yield trozo

    @staticmethod
    def _a_resumen(crudo: dict[str, Any]) -> ResumenDeMensaje:
        remitente = (
            crudo.get("from", {}).get("emailAddress", {}).get("address", "")
            if isinstance(crudo.get("from"), dict)
            else ""
        )
        recibido = None
        marca = crudo.get("receivedDateTime")
        if marca:
            try:
                recibido = asegurar_utc(datetime.fromisoformat(marca.replace("Z", "+00:00")))
            except ValueError:
                recibido = None
        return ResumenDeMensaje(
            id_del_proveedor=str(crudo.get("id", "")),
            remitente=str(remitente)[:320],
            asunto=str(crudo.get("subject", "")),
            recibido_en=recibido,
        )
