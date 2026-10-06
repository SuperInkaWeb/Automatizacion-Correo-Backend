"""
Revocacion de tokens en el adaptador de proveedor OAuth.

Proposito
    Fijar el comportamiento cuando el aviso de revocacion al proveedor
    falla: el borrado local debe seguir adelante, el fallo debe quedar
    registrado, y el token no debe aparecer en el log.

Por que existe este fichero
    El bloque `except` de `revocar` llego a hacer `pass` con un comentario
    que prometia un reintento desde el cron que no existia. Un fallo de
    revocacion quedaba invisible: el permiso podia seguir vivo en el
    proveedor y nadie se enteraba. Estos tests impiden que se vuelva a ese
    silencio.
"""

from __future__ import annotations

import httpx
import pytest

from mailauto.modules.mailbox.infrastructure.providers import base
from mailauto.modules.mailbox.infrastructure.providers.base import ProveedorGoogle

pytestmark = pytest.mark.security

_TOKEN = "refresh-token-que-no-debe-aparecer-en-el-log"


class _ClienteQueFalla:
    """Doble de `httpx.AsyncClient` cuyo `post` siempre falla."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _ClienteQueFalla:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no hay red")


class _LoggerEspia:
    def __init__(self) -> None:
        self.avisos: list[tuple[str, dict[str, object]]] = []

    def warning(self, evento: str, **datos: object) -> None:
        self.avisos.append((evento, datos))


@pytest.fixture
def proveedor(monkeypatch: pytest.MonkeyPatch) -> tuple[ProveedorGoogle, _LoggerEspia]:
    monkeypatch.setattr(base.httpx, "AsyncClient", _ClienteQueFalla)
    espia = _LoggerEspia()
    monkeypatch.setattr(base, "logger", espia)
    return ProveedorGoogle(client_id="id", client_secret="secreto"), espia


async def test_un_fallo_al_revocar_no_rompe_el_borrado(
    proveedor: tuple[ProveedorGoogle, _LoggerEspia],
) -> None:
    """
    El usuario pidio desvincular: eso tiene que ocurrir aunque el proveedor
    no responda. `revocar` no debe propagar la excepcion.
    """
    google, _ = proveedor
    # No debe lanzar.
    assert await google.revocar(_TOKEN) is None


async def test_el_fallo_al_revocar_queda_registrado(
    proveedor: tuple[ProveedorGoogle, _LoggerEspia],
) -> None:
    """Un fallo que no se registra es un permiso vivo del que nadie se entera."""
    google, espia = proveedor

    await google.revocar(_TOKEN)

    eventos = [evento for evento, _ in espia.avisos]
    assert "revocacion_fallida" in eventos


async def test_el_token_no_aparece_en_el_log(
    proveedor: tuple[ProveedorGoogle, _LoggerEspia],
) -> None:
    """
    El registro informa del fallo y del proveedor, nunca del token: un
    secreto en un log es un secreto filtrado.
    """
    google, espia = proveedor

    await google.revocar(_TOKEN)

    for _, datos in espia.avisos:
        assert _TOKEN not in repr(datos)
    # Pero si debe decir de que proveedor se trata, para poder actuar.
    _, datos = espia.avisos[0]
    assert datos.get("proveedor") == "google"
