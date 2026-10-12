"""
Reinicio de los datos de escaneo de un tenant.

Se verifica lo que de verdad importa de una accion destructiva: que
exige permiso de administrador, que purga los tres contextos y que un
fallo al borrar un objeto del almacen no aborta el reinicio (solo deja
un huerfano, inofensivo).
"""

from __future__ import annotations

import pytest

from mailauto.modules.admin.application.reiniciar_datos import ReiniciarDatosDeEscaneo
from mailauto.shared.errors import ErrorDeAutorizacion
from mailauto.shared.security.context import TenantContext


class _RegistrosFalso:
    def __init__(self) -> None:
        self.purgado = False

    async def purgar(self, ctx: TenantContext) -> int:
        self.purgado = True
        return 7


class _IngestaFalso:
    def __init__(self, claves: list[str]) -> None:
        self._claves = claves
        self.purgado = False

    async def claves_de_almacenamiento(self, ctx: TenantContext) -> list[str]:
        return self._claves

    async def purgar_datos_de_escaneo(self, ctx: TenantContext) -> dict[str, int]:
        self.purgado = True
        return {"errores": 1, "adjuntos": 2, "mensajes": 3, "trabajos": 1}


class _ExportacionesFalso:
    async def purgar(self, tenant_id: object) -> int:
        return 2


class _AlmacenFalso:
    def __init__(self, falla_en: tuple[str, ...] = ()) -> None:
        self.borradas: list[str] = []
        self._falla = set(falla_en)

    async def eliminar(self, clave: str) -> None:
        if clave in self._falla:
            raise RuntimeError("el almacen no responde")
        self.borradas.append(clave)


def _caso(almacen: _AlmacenFalso, claves: list[str]) -> ReiniciarDatosDeEscaneo:
    return ReiniciarDatosDeEscaneo(
        registros=_RegistrosFalso(),
        ingesta=_IngestaFalso(claves),
        exportaciones=_ExportacionesFalso(),
        almacen=almacen,
    )


async def test_reinicia_los_tres_contextos_y_resume(contexto_admin: TenantContext) -> None:
    almacen = _AlmacenFalso()
    caso = _caso(almacen, ["t/a", "t/b"])

    resumen = await caso.ejecutar(contexto_admin)

    assert resumen["registros"] == 7
    assert resumen["exportaciones"] == 2
    assert resumen["objetos"] == 2
    assert resumen["adjuntos"] == 2
    assert almacen.borradas == ["t/a", "t/b"]


async def test_un_operador_no_puede_reiniciar(contexto_a: TenantContext) -> None:
    """Es una accion de administrador: un operador no debe poder borrarlo todo."""
    caso = _caso(_AlmacenFalso(), ["t/a"])
    with pytest.raises(ErrorDeAutorizacion):
        await caso.ejecutar(contexto_a)


async def test_un_fallo_al_borrar_un_objeto_no_aborta(contexto_admin: TenantContext) -> None:
    # La base ya quedo consistente; que un objeto no se borre solo deja un
    # huerfano. Abortar aqui dejaria el reinicio a medias por una nimiedad.
    almacen = _AlmacenFalso(falla_en=("t/b",))
    caso = _caso(almacen, ["t/a", "t/b", "t/c"])

    resumen = await caso.ejecutar(contexto_admin)

    assert resumen["objetos"] == 2  # solo las que si se borraron
    assert almacen.borradas == ["t/a", "t/c"]
