"""
Tests del ciclo de vida de una exportacion.

Verifican que un reporte se encole, se genere y se entregue por URL
prefirmada, y que un fallo al generarlo deje constancia en vez de
dejar la fila colgada en "generando" para siempre.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from mailauto.modules.reporting.application.exportar import (
    ConsultarExportacion,
    GenerarExportacion,
    SolicitarExportacion,
)
from mailauto.modules.reporting.domain.ports import (
    DestinoDeReportes,
    EstadoDeExportacion,
    Exportacion,
    FilaDeReporte,
    FormatoDeReporte,
    FuenteDeFilas,
    GeneradorDeReporte,
    RepositorioDeExportaciones,
)
from mailauto.shared.errors import ErrorDeAutorizacion, RecursoNoEncontrado
from mailauto.shared.security.context import Permiso, Rol, TenantContext
from tests.conftest import TENANT_A, USUARIO_A

# ─────────────────────────────────────────────────────────────────────
# Dobles
# ─────────────────────────────────────────────────────────────────────


class RepositorioFalso(RepositorioDeExportaciones):
    def __init__(self, existente: Exportacion | None = None) -> None:
        self.almacenadas: dict[UUID, Exportacion] = {}
        if existente is not None:
            self.almacenadas[existente.id] = existente
        self.actualizaciones = 0

    async def crear(self, exportacion: Exportacion) -> Exportacion:
        self.almacenadas[exportacion.id] = exportacion
        return exportacion

    async def actualizar(self, exportacion: Exportacion) -> None:
        self.actualizaciones += 1
        self.almacenadas[exportacion.id] = exportacion

    async def obtener(self, tenant_id: UUID, exportacion_id: UUID) -> Exportacion | None:
        exportacion = self.almacenadas.get(exportacion_id)
        if exportacion is None or exportacion.tenant_id != tenant_id:
            return None
        return exportacion

    async def purgar(self, tenant_id: UUID) -> int:
        propias = {i: e for i, e in self.almacenadas.items() if e.tenant_id == tenant_id}
        for identificador in propias:
            del self.almacenadas[identificador]
        return len(propias)


class FuenteFalsa(FuenteDeFilas):
    def __init__(self, filas: list[FilaDeReporte], *, revienta: bool = False) -> None:
        self._filas = filas
        self._revienta = revienta
        self.limite_recibido: int | None = None

    async def obtener(
        self, tenant_id: UUID, filtros: dict[str, str], limite: int
    ) -> list[FilaDeReporte]:
        if self._revienta:
            raise RuntimeError("la base de datos no responde")
        self.limite_recibido = limite
        return self._filas


class GeneradorFalso(GeneradorDeReporte):
    def __init__(self, formato: FormatoDeReporte = FormatoDeReporte.EXCEL) -> None:
        self._formato = formato
        self.filas_recibidas: list[FilaDeReporte] = []

    @property
    def formato(self) -> FormatoDeReporte:
        return self._formato

    def generar(self, filas: list[FilaDeReporte], titulo: str) -> bytes:
        self.filas_recibidas = filas
        return b"PK" + b"\x00" * 100


class DestinoFalso(DestinoDeReportes):
    def __init__(self) -> None:
        self.guardados: dict[str, bytes] = {}
        self.ttl_recibido: int | None = None

    async def guardar(self, clave: str, contenido: bytes, tipo_mime: str) -> None:
        self.guardados[clave] = contenido

    async def url_de_descarga(self, clave: str, *, ttl_segundos: int) -> str:
        self.ttl_recibido = ttl_segundos
        return f"https://storage/{clave}?exp={ttl_segundos}"


def _exportacion(
    tenant_id: UUID = TENANT_A, estado: EstadoDeExportacion = EstadoDeExportacion.EN_COLA
) -> Exportacion:
    return Exportacion(tenant_id=tenant_id, solicitado_por=USUARIO_A, estado=estado)


def _generar(
    repositorio: RepositorioFalso,
    fuente: FuenteFalsa,
    destino: DestinoFalso,
    generador: GeneradorFalso,
    *,
    limite: int = 1000,
) -> GenerarExportacion:
    return GenerarExportacion(
        repositorio=repositorio,
        fuente=fuente,
        generadores={FormatoDeReporte.EXCEL: generador},
        destino=destino,
        limite_de_filas=limite,
    )


# ─────────────────────────────────────────────────────────────────────
# Solicitud
# ─────────────────────────────────────────────────────────────────────


async def test_solicitar_persiste_y_encola(contexto_a: TenantContext) -> None:
    """
    Se persiste antes de encolar: si fallara la cola, queda una fila
    visible que el cron puede reintentar.
    """
    repositorio = RepositorioFalso()
    encolados: list[UUID] = []

    async def encolar(*, tenant_id: UUID, exportacion_id: UUID) -> str:
        encolados.append(exportacion_id)
        return "job"

    exportacion = await SolicitarExportacion(repositorio, encolar).ejecutar(
        contexto_a, formato=FormatoDeReporte.EXCEL, filtros={"periodo": "202603"}
    )

    assert exportacion.id in repositorio.almacenadas
    assert encolados == [exportacion.id]
    assert exportacion.estado is EstadoDeExportacion.EN_COLA


async def test_un_rol_sin_permiso_de_reportes_no_puede_exportar() -> None:
    """Los datos tributarios de todo el tenant salen en un solo fichero."""
    repositorio = RepositorioFalso()

    async def encolar(*, tenant_id: UUID, exportacion_id: UUID) -> str:
        return "job"

    sin_permiso = TenantContext(
        tenant_id=TENANT_A,
        user_id=USUARIO_A,
        external_id="auth0|x",
        rol=Rol.VIEWER,
        permisos=frozenset({Permiso.RECORD_READ}),
    )
    with pytest.raises(ErrorDeAutorizacion):
        await SolicitarExportacion(repositorio, encolar).ejecutar(
            sin_permiso, formato=FormatoDeReporte.EXCEL, filtros={}
        )


# ─────────────────────────────────────────────────────────────────────
# Generacion
# ─────────────────────────────────────────────────────────────────────


async def test_genera_sube_y_marca_lista() -> None:
    exportacion = _exportacion()
    repositorio = RepositorioFalso(exportacion)
    destino = DestinoFalso()
    generador = GeneradorFalso()
    filas = [FilaDeReporte(ruc_contribuyente="20131312955")]

    await _generar(repositorio, FuenteFalsa(filas), destino, generador).ejecutar(
        tenant_id=TENANT_A, exportacion_id=exportacion.id
    )

    resultado = repositorio.almacenadas[exportacion.id]
    assert resultado.estado is EstadoDeExportacion.LISTA
    assert resultado.total_filas == 1
    assert resultado.clave_de_almacenamiento in destino.guardados
    assert resultado.finalizado_en is not None


async def test_la_clave_incluye_el_tenant() -> None:
    """Permite politicas de ciclo de vida y borrado masivo por cliente."""
    exportacion = _exportacion()
    repositorio = RepositorioFalso(exportacion)
    destino = DestinoFalso()

    await _generar(repositorio, FuenteFalsa([]), destino, GeneradorFalso()).ejecutar(
        tenant_id=TENANT_A, exportacion_id=exportacion.id
    )

    clave = repositorio.almacenadas[exportacion.id].clave_de_almacenamiento
    assert clave is not None
    assert clave.startswith(f"{TENANT_A}/reportes/")
    assert clave.endswith(".xlsx")


async def test_se_respeta_el_tope_de_filas() -> None:
    """Nadie debe poder arrastrar la tabla entera a memoria de un golpe."""
    exportacion = _exportacion()
    repositorio = RepositorioFalso(exportacion)
    fuente = FuenteFalsa([])

    await _generar(repositorio, fuente, DestinoFalso(), GeneradorFalso(), limite=250).ejecutar(
        tenant_id=TENANT_A, exportacion_id=exportacion.id
    )

    assert fuente.limite_recibido == 250


async def test_un_fallo_deja_constancia_y_no_propaga() -> None:
    """
    Propagar la excepcion dejaria la fila en "generando" para siempre,
    que es donde el usuario va a mirar.
    """
    exportacion = _exportacion()
    repositorio = RepositorioFalso(exportacion)

    await _generar(
        repositorio, FuenteFalsa([], revienta=True), DestinoFalso(), GeneradorFalso()
    ).ejecutar(tenant_id=TENANT_A, exportacion_id=exportacion.id)

    resultado = repositorio.almacenadas[exportacion.id]
    assert resultado.estado is EstadoDeExportacion.FALLIDA
    assert resultado.finalizado_en is not None


async def test_el_mensaje_de_error_no_expone_el_detalle_interno() -> None:
    exportacion = _exportacion()
    repositorio = RepositorioFalso(exportacion)

    await _generar(
        repositorio, FuenteFalsa([], revienta=True), DestinoFalso(), GeneradorFalso()
    ).ejecutar(tenant_id=TENANT_A, exportacion_id=exportacion.id)

    mensaje = repositorio.almacenadas[exportacion.id].mensaje_de_error or ""
    assert "base de datos" not in mensaje
    assert "RuntimeError" not in mensaje


async def test_un_formato_no_soportado_falla_de_forma_controlada() -> None:
    exportacion = Exportacion(tenant_id=TENANT_A, formato=FormatoDeReporte.CSV)
    repositorio = RepositorioFalso(exportacion)

    # Solo se registra el generador de Excel.
    await _generar(repositorio, FuenteFalsa([]), DestinoFalso(), GeneradorFalso()).ejecutar(
        tenant_id=TENANT_A, exportacion_id=exportacion.id
    )

    assert repositorio.almacenadas[exportacion.id].estado is EstadoDeExportacion.FALLIDA


async def test_no_regenera_una_exportacion_ya_terminada() -> None:
    """Reentrega del job: regenerar volveria a cobrar el mismo trabajo."""
    exportacion = _exportacion(estado=EstadoDeExportacion.LISTA)
    repositorio = RepositorioFalso(exportacion)
    destino = DestinoFalso()

    await _generar(repositorio, FuenteFalsa([]), destino, GeneradorFalso()).ejecutar(
        tenant_id=TENANT_A, exportacion_id=exportacion.id
    )

    assert destino.guardados == {}
    assert repositorio.actualizaciones == 0


async def test_una_exportacion_inexistente_no_revienta_el_worker() -> None:
    repositorio = RepositorioFalso()
    await _generar(repositorio, FuenteFalsa([]), DestinoFalso(), GeneradorFalso()).ejecutar(
        tenant_id=TENANT_A, exportacion_id=uuid4()
    )


# ─────────────────────────────────────────────────────────────────────
# Consulta
# ─────────────────────────────────────────────────────────────────────


async def test_mientras_no_esta_lista_no_hay_url(contexto_a: TenantContext) -> None:
    exportacion = _exportacion()
    repositorio = RepositorioFalso(exportacion)

    resultado, url = await ConsultarExportacion(repositorio, DestinoFalso()).ejecutar(
        contexto_a, exportacion.id
    )
    assert resultado.estado is EstadoDeExportacion.EN_COLA
    assert url is None


async def test_cuando_esta_lista_devuelve_una_url_de_vida_corta(
    contexto_a: TenantContext,
) -> None:
    """
    El enlace lleva los datos tributarios del cliente y suele acabar
    pegado en un chat.
    """
    exportacion = _exportacion()
    exportacion.completar(clave="t/reportes/x.xlsx", total_filas=5)
    repositorio = RepositorioFalso(exportacion)
    destino = DestinoFalso()

    _, url = await ConsultarExportacion(repositorio, destino).ejecutar(contexto_a, exportacion.id)
    assert url is not None
    assert destino.ttl_recibido is not None
    assert destino.ttl_recibido <= 300


async def test_no_se_consulta_la_exportacion_de_otro_tenant(
    contexto_a: TenantContext, contexto_b: TenantContext
) -> None:
    ajena = _exportacion(tenant_id=contexto_b.tenant_id)
    repositorio = RepositorioFalso(ajena)

    with pytest.raises(RecursoNoEncontrado):
        await ConsultarExportacion(repositorio, DestinoFalso()).ejecutar(contexto_a, ajena.id)
