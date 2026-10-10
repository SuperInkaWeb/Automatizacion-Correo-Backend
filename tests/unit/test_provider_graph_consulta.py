"""
Consulta de mensajes contra Microsoft Graph.

Por que existe este fichero
    Graph devuelve 400 ("the restriction or sort order is too complex to
    process together") si se combina un `$filter` sobre `hasAttachments`
    con un `$orderby` por `receivedDateTime`: el orden y el filtro caen
    sobre propiedades distintas. Eso tumbaba todos los escaneos de Outlook
    con `error_de_proveedor`. Este test fija que la consulta no vuelva a
    pedir ese `$orderby` incompatible.
"""

from __future__ import annotations

from typing import Any

import pytest

from mailauto.modules.ingestion.infrastructure.providers import correo
from mailauto.modules.ingestion.infrastructure.providers.correo import (
    ProveedorMicrosoftGraph,
)


class _RespuestaFalsa:
    status_code = 200

    @staticmethod
    def json() -> dict[str, Any]:
        return {"value": []}  # sin mensajes: la iteracion termina en una pagina


class _ClienteCaptura:
    """Doble de httpx.AsyncClient que guarda los params de cada peticion."""

    def __init__(self, capturados: list[dict[str, Any]]) -> None:
        self._capturados = capturados

    def __call__(self, *_args: object, **_kwargs: object) -> _ClienteCaptura:
        # El proveedor hace `httpx.AsyncClient(timeout=...)`: esta instancia
        # ya construida se devuelve a si misma como si fuera la clase.
        return self

    async def __aenter__(self) -> _ClienteCaptura:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def request(
        self, _metodo: str, _url: str, *, params: dict[str, Any] | None = None, **_kw: object
    ) -> _RespuestaFalsa:
        self._capturados.append(params or {})
        return _RespuestaFalsa()


async def test_la_consulta_de_graph_no_pide_orderby(monkeypatch: pytest.MonkeyPatch) -> None:
    capturados: list[dict[str, Any]] = []
    monkeypatch.setattr(correo.httpx, "AsyncClient", _ClienteCaptura(capturados))

    proveedor = ProveedorMicrosoftGraph()
    async for _ in proveedor.listar_mensajes(
        "token", desde=None, hasta=None, limite=100, carpeta="INBOX"
    ):
        pass

    assert capturados, "no se llego a consultar a Graph"
    params = capturados[0]
    # El $orderby incompatible no debe estar; el filtro por adjuntos si.
    assert "$orderby" not in params
    assert "hasAttachments eq true" in params["$filter"]
