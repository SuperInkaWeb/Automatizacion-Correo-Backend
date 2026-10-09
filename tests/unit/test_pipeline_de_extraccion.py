"""
Tests de las politicas de calidad y del orquestador de extraccion.

Aqui se verifica lo que decide cuanto cuesta cada documento y quien
acaba mirandolo: cuando parar de probar motores, como combinar lo que
leyo cada uno y que va a revision humana.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from mailauto.modules.extraction.application.extraer_documento import (
    ControlDePresupuesto,
    ExtraerDocumento,
)
from mailauto.modules.extraction.domain import policies
from mailauto.modules.extraction.domain.entities import (
    CampoExtraido,
    Completitud,
    EstadoDeRevision,
    Estrategia,
    RegistroTributario,
    ResultadoDeEstrategia,
)
from mailauto.modules.extraction.domain.ports import (
    DocumentoAExtraer,
    EstrategiaDeExtraccion,
    FiltrosDeRegistro,
    PerfilDeExtraccion,
    RepositorioDeRegistros,
)
from mailauto.modules.extraction.infrastructure.profiles.sunat_arrendamiento import (
    PerfilSunatArrendamiento,
)
from mailauto.shared.pagination import Pagina, SolicitudDePagina
from mailauto.shared.security.context import TenantContext
from tests.conftest import TENANT_A

TRABAJO = UUID("00000000-0000-7000-8000-00000000000f")


def campo(valor: str, confianza: float, estrategia: Estrategia = Estrategia.TEXTO_NATIVO):  # type: ignore[no-untyped-def]
    return CampoExtraido(valor, confianza, estrategia)


# ─────────────────────────────────────────────────────────────────────
# Politicas
# ─────────────────────────────────────────────────────────────────────


def test_se_detiene_cuando_los_campos_imprescindibles_son_fiables() -> None:
    resultado = ResultadoDeEstrategia(
        estrategia=Estrategia.TEXTO_NATIVO,
        campos={
            "ruc_contribuyente": campo("20131312955", 0.98),
            "periodo": campo("202603", 0.96),
            "importe_pagado": campo("1850.00", 0.95),
        },
    )
    assert policies.es_suficiente_para_detenerse(resultado)


def test_no_se_detiene_si_falta_un_imprescindible() -> None:
    """
    Aunque la media sea altisima: un registro sin importe no sirve
    para el reporte, y el motor siguiente puede encontrarlo.
    """
    resultado = ResultadoDeEstrategia(
        estrategia=Estrategia.TEXTO_NATIVO,
        campos={
            "ruc_contribuyente": campo("20131312955", 1.0),
            "periodo": campo("202603", 1.0),
        },
    )
    assert not policies.es_suficiente_para_detenerse(resultado)


def test_no_se_detiene_si_un_imprescindible_es_dudoso() -> None:
    """
    El caso que una media global dejaria pasar: RUC perfecto, importe
    ilegible. Es justo cuando un motor mejor aporta algo.
    """
    resultado = ResultadoDeEstrategia(
        estrategia=Estrategia.TEXTO_NATIVO,
        campos={
            "ruc_contribuyente": campo("20131312955", 1.0),
            "periodo": campo("202603", 1.0),
            "importe_pagado": campo("18S0.00", 0.35),
        },
    )
    assert not policies.es_suficiente_para_detenerse(resultado)
    assert resultado.confianza_media() > 0.75


def test_combina_el_mejor_valor_de_cada_campo() -> None:
    """
    El texto nativo lee el RUC perfecto y falla en un importe que esta
    dentro de una imagen incrustada; el OCR acierta ahi. Quedarse con
    un solo motor tiraria la mitad de lo que ya se leyo y se pago.
    """
    nativo = ResultadoDeEstrategia(
        estrategia=Estrategia.TEXTO_NATIVO,
        campos={
            "ruc_contribuyente": campo("20131312955", 0.98),
            "importe_pagado": campo("18S0", 0.30),
        },
    )
    ocr = ResultadoDeEstrategia(
        estrategia=Estrategia.OCR_LOCAL,
        campos={
            "ruc_contribuyente": campo("2013I312955", 0.60, Estrategia.OCR_LOCAL),
            "importe_pagado": campo("1850.00", 0.88, Estrategia.OCR_LOCAL),
        },
    )

    mejor = policies.combinar([nativo, ocr])
    assert mejor["ruc_contribuyente"].valor == "20131312955"
    assert mejor["importe_pagado"].valor == "1850.00"
    assert mejor["importe_pagado"].estrategia is Estrategia.OCR_LOCAL


def test_combinar_ignora_los_resultados_fallidos() -> None:
    fallido = ResultadoDeEstrategia(
        estrategia=Estrategia.TABLAS_PDF,
        campos={"importe_pagado": campo("999", 1.0)},
        error="timeout",
    )
    bueno = ResultadoDeEstrategia(
        estrategia=Estrategia.TEXTO_NATIVO, campos={"importe_pagado": campo("1850.00", 0.9)}
    )
    assert policies.combinar([fallido, bueno])["importe_pagado"].valor == "1850.00"


def test_en_empate_gana_la_estrategia_mas_barata() -> None:
    """Por el orden del pipeline, la primera en la lista es la mas fiable."""
    primero = ResultadoDeEstrategia(
        estrategia=Estrategia.TEXTO_NATIVO, campos={"periodo": campo("202603", 0.9)}
    )
    segundo = ResultadoDeEstrategia(
        estrategia=Estrategia.OCR_LOCAL,
        campos={"periodo": campo("202604", 0.9, Estrategia.OCR_LOCAL)},
    )
    assert policies.combinar([primero, segundo])["periodo"].valor == "202603"


@pytest.mark.parametrize(
    ("campos", "esperado"),
    [
        ({}, Completitud.VACIO),
        ({"nombre_contribuyente": ("Alguien", 0.9)}, Completitud.PARCIAL),
        (
            {
                "ruc_contribuyente": ("20131312955", 0.98),
                "periodo": ("202603", 0.96),
                "importe_pagado": ("1850.00", 0.95),
            },
            Completitud.COMPLETO,
        ),
        (
            {
                "ruc_contribuyente": ("20131312955", 0.98),
                "periodo": ("202603", 0.96),
                "importe_pagado": ("1850.00", 0.40),
            },
            Completitud.PARCIAL,
        ),
    ],
)
def test_clasifica_la_completitud(
    campos: dict[str, tuple[str, float]], esperado: Completitud
) -> None:
    convertidos = {n: campo(v, c) for n, (v, c) in campos.items()}
    assert policies.clasificar(convertidos) == esperado


def test_un_registro_vacio_no_entra_en_la_cola_de_revision() -> None:
    """
    No hay nada que corregir: el documento no era lo que se buscaba.
    Llenar la cola de estos casos la vuelve inutil, que es la forma
    mas segura de que nadie la use.
    """
    assert policies.decidir_revision(Completitud.VACIO, {}) is EstadoDeRevision.NO_REQUERIDA


def test_un_registro_parcial_siempre_va_a_revision() -> None:
    assert policies.decidir_revision(Completitud.PARCIAL, {}) is EstadoDeRevision.PENDIENTE


def test_un_completo_con_un_campo_deseable_dudoso_tambien_se_revisa() -> None:
    """
    Barato de corregir, y evita que un nombre mal leido llegue al
    reporte con apariencia de dato bueno.
    """
    campos = {
        "ruc_contribuyente": campo("20131312955", 0.98),
        "periodo": campo("202603", 0.96),
        "importe_pagado": campo("1850.00", 0.95),
        "nombre_contribuyente": campo("1NM0B1L1AR1A", 0.42),
    }
    assert policies.decidir_revision(Completitud.COMPLETO, campos) is EstadoDeRevision.PENDIENTE


def test_la_confianza_global_pondera_los_campos_imprescindibles() -> None:
    """
    Una media simple dejaria que cinco campos accesorios perfectos
    compensaran un importe ilegible.
    """
    campos = {
        "ruc_contribuyente": campo("x", 0.2),
        "periodo": campo("x", 0.2),
        "importe_pagado": campo("x", 0.2),
        "nombre_contribuyente": campo("x", 1.0),
        "nombre_inquilino": campo("x", 1.0),
        "fecha_de_pago": campo("x", 1.0),
    }
    assert policies.confianza_global(campos) < 0.55


# ─────────────────────────────────────────────────────────────────────
# Dobles para el orquestador
# ─────────────────────────────────────────────────────────────────────


class RepositorioFalso(RepositorioDeRegistros):
    def __init__(self, *, ya_existe: bool = False) -> None:
        self.guardados: list[RegistroTributario] = []
        self._ya_existe = ya_existe

    async def guardar(self, tenant_id: UUID, registro: RegistroTributario) -> RegistroTributario:
        self.guardados.append(registro)
        return registro

    async def actualizar(self, ctx: TenantContext, registro: RegistroTributario) -> None:
        return None

    async def obtener(self, ctx: TenantContext, registro_id: UUID) -> RegistroTributario | None:
        return None

    async def listar(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, pagina: SolicitudDePagina
    ) -> Pagina[RegistroTributario]:
        return Pagina(elementos=self.guardados)

    async def listar_para_reporte(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, limite: int
    ) -> list[RegistroTributario]:
        return self.guardados

    async def existe_para_adjunto(self, tenant_id: UUID, adjunto_id: UUID) -> bool:
        return self._ya_existe

    async def contar_pendientes_de_revision(self, ctx: TenantContext) -> int:
        return 0


class EstrategiaFalsa(EstrategiaDeExtraccion):
    def __init__(
        self,
        nombre: Estrategia,
        costo: int,
        campos: dict[str, CampoExtraido] | None = None,
        *,
        admite_todo: bool = True,
        error: str | None = None,
    ) -> None:
        self._nombre = nombre
        self._costo = costo
        self._campos = campos or {}
        self._admite = admite_todo
        self._error = error
        self.invocaciones = 0

    @property
    def nombre(self) -> Estrategia:
        return self._nombre

    @property
    def costo_relativo(self) -> int:
        return self._costo

    def admite(self, documento: DocumentoAExtraer) -> bool:
        return self._admite

    async def leer(
        self, documento: DocumentoAExtraer, perfil: PerfilDeExtraccion
    ) -> ResultadoDeEstrategia:
        self.invocaciones += 1
        return ResultadoDeEstrategia(
            estrategia=self._nombre,
            campos=dict(self._campos),
            duracion_ms=10,
            error=self._error,
        )


def _documento() -> DocumentoAExtraer:
    return DocumentoAExtraer(
        adjunto_id=uuid4(),
        tenant_id=TENANT_A,
        trabajo_id=TRABAJO,
        contenido=b"%PDF-1.7\n",
        tipo_mime="application/pdf",
        nombre="constancia.pdf",
    )


def _completos() -> dict[str, CampoExtraido]:
    return {
        "ruc_contribuyente": campo("20131312955", 0.98),
        "periodo": campo("03/2026", 0.96),
        "importe_pagado": campo("S/ 1850.00", 0.95),
    }


def _pipeline(
    estrategias: list[EstrategiaDeExtraccion],
    repositorio: RepositorioFalso,
    *,
    ia_habilitada: bool = True,
    maximo_ia: int = 10,
) -> ExtraerDocumento:
    return ExtraerDocumento(
        estrategias=estrategias,
        perfil=PerfilSunatArrendamiento(),
        repositorio=repositorio,
        presupuesto=ControlDePresupuesto(
            ia_habilitada=ia_habilitada, maximo_llamadas_por_trabajo=maximo_ia
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Orquestador
# ─────────────────────────────────────────────────────────────────────


async def test_se_para_en_el_primer_motor_si_basta() -> None:
    """
    La diferencia de coste entre pararse aqui y llegar a la IA es de
    varios ordenes de magnitud.
    """
    barato = EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, _completos())
    caro = EstrategiaFalsa(Estrategia.VISION_IA, 100, _completos())
    repositorio = RepositorioFalso()

    await _pipeline([barato, caro], repositorio).ejecutar(_documento())

    assert barato.invocaciones == 1
    assert caro.invocaciones == 0


async def test_llega_a_la_ia_si_los_motores_gratuitos_no_bastan() -> None:
    pobre = EstrategiaFalsa(
        Estrategia.TEXTO_NATIVO, 10, {"nombre_contribuyente": campo("ALGO", 0.5)}
    )
    ia = EstrategiaFalsa(Estrategia.VISION_IA, 100, _completos())
    repositorio = RepositorioFalso()

    await _pipeline([pobre, ia], repositorio).ejecutar(_documento())

    assert pobre.invocaciones == 1
    assert ia.invocaciones == 1


async def test_prueba_los_motores_en_orden_de_costo() -> None:
    """El orden lo fija el pipeline, no el orden en que se registraron."""
    caro = EstrategiaFalsa(Estrategia.VISION_IA, 100, _completos())
    barato = EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, _completos())
    repositorio = RepositorioFalso()

    # Se registran al reves a proposito.
    await _pipeline([caro, barato], repositorio).ejecutar(_documento())

    assert barato.invocaciones == 1
    assert caro.invocaciones == 0


async def test_el_presupuesto_corta_la_ia_y_degrada_a_lo_gratuito() -> None:
    """
    Un tenant sin cupo recibe un resultado parcial que una persona
    puede corregir, que vale mas que ninguno.
    """
    pobre = EstrategiaFalsa(
        Estrategia.TEXTO_NATIVO, 10, {"nombre_contribuyente": campo("ALGO", 0.5)}
    )
    ia = EstrategiaFalsa(Estrategia.VISION_IA, 100, _completos())
    repositorio = RepositorioFalso()

    await _pipeline([pobre, ia], repositorio, ia_habilitada=False).ejecutar(_documento())

    assert ia.invocaciones == 0
    assert repositorio.guardados[0].requiere_revision


async def test_el_tope_de_llamadas_por_trabajo_se_respeta() -> None:
    pobre = EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, {})
    ia = EstrategiaFalsa(Estrategia.VISION_IA, 100, {})
    repositorio = RepositorioFalso()
    pipeline = _pipeline([pobre, ia], repositorio, maximo_ia=2)

    for _ in range(4):
        await pipeline.ejecutar(_documento())

    # Cuatro documentos del mismo trabajo, pero solo dos llamadas de pago.
    assert ia.invocaciones == 2


async def test_un_motor_que_falla_no_aborta_el_pipeline() -> None:
    roto = EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, {}, error="timeout")
    bueno = EstrategiaFalsa(Estrategia.OCR_LOCAL, 50, _completos())
    repositorio = RepositorioFalso()

    await _pipeline([roto, bueno], repositorio).ejecutar(_documento())

    assert len(repositorio.guardados) == 1
    assert repositorio.guardados[0].completitud is Completitud.COMPLETO


async def test_convierte_los_campos_crudos_en_objetos_de_valor() -> None:
    repositorio = RepositorioFalso()
    await _pipeline(
        [EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, _completos())], repositorio
    ).ejecutar(_documento())

    registro = repositorio.guardados[0]
    assert registro.ruc_contribuyente is not None
    assert registro.ruc_contribuyente.valor == "20131312955"
    assert str(registro.periodo) == "202603"
    assert str(registro.importe_pagado) == "1850.00"


async def test_no_reextrae_un_adjunto_ya_procesado() -> None:
    """Idempotencia: la reentrega del job no duplica el registro."""
    motor = EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, _completos())
    repositorio = RepositorioFalso(ya_existe=True)

    resultado = await _pipeline([motor], repositorio).ejecutar(_documento())

    assert resultado is None
    assert motor.invocaciones == 0
    assert repositorio.guardados == []


async def test_un_documento_que_ningun_motor_admite_no_revienta() -> None:
    incompatible = EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, _completos(), admite_todo=False)
    repositorio = RepositorioFalso()

    assert await _pipeline([incompatible], repositorio).ejecutar(_documento()) is None


async def test_registra_el_motor_que_aporto_mas_campos() -> None:
    """Es lo que permite decidir con datos si la IA merece su coste."""
    repositorio = RepositorioFalso()
    await _pipeline(
        [EstrategiaFalsa(Estrategia.TEXTO_NATIVO, 10, _completos())], repositorio
    ).ejecutar(_documento())

    assert repositorio.guardados[0].estrategia_usada is Estrategia.TEXTO_NATIVO
