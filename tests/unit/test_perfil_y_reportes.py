"""
Tests del perfil SUNAT, la revision humana y los generadores de reporte.

El perfil es lo que decide cuantos campos se leen de cada documento, y
su tolerancia al ruido del OCR es la diferencia entre que el pipeline
sirva para escaneos o solo para PDFs limpios.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from mailauto.modules.extraction.application.revisar_registros import RevisarRegistro
from mailauto.modules.extraction.domain.entities import (
    EstadoDeRevision,
    RegistroTributario,
)
from mailauto.modules.extraction.domain.ports import (
    DocumentoAExtraer,
    FiltrosDeRegistro,
    RepositorioDeRegistros,
)
from mailauto.modules.extraction.domain.value_objects import Importe
from mailauto.modules.extraction.infrastructure.profiles.sunat_arrendamiento import (
    PerfilSunatArrendamiento,
)
from mailauto.modules.reporting.domain.ports import COLUMNAS, FilaDeReporte
from mailauto.modules.reporting.infrastructure.generadores import (
    GeneradorCsv,
    GeneradorExcel,
)
from mailauto.shared.errors import ErrorDeAutorizacion, ErrorDeValidacion
from mailauto.shared.pagination import Pagina, SolicitudDePagina
from mailauto.shared.security.context import TenantContext
from tests.conftest import TENANT_A

# ─────────────────────────────────────────────────────────────────────
# Perfil SUNAT
# ─────────────────────────────────────────────────────────────────────

DOCUMENTO_LIMPIO = """\
SUPERINTENDENCIA NACIONAL DE ADUANAS Y DE ADMINISTRACION TRIBUTARIA
CONSTANCIA DE PAGO - FORMULARIO 1683
IMPUESTO A LA RENTA DE PRIMERA CATEGORIA - ARRENDAMIENTO

RUC del Arrendador: 20131312955
Nombre / Razon Social: INMOBILIARIA LOS OLIVOS SAC
RUC del Arrendatario: 20100047218
Nombre del Inquilino: COMERCIAL SAN MARTIN EIRL
Periodo Tributario: 03/2026
Fecha de Pago: 15/04/2026
Numero de Operacion: 0012345678
Importe Pagado: S/ 1,850.00
"""

# El mismo documento pasado por un OCR mediocre: ceros por oes, unos
# por eles, separadores perdidos y dos puntos que desaparecen.
DOCUMENTO_DEGRADADO = """\
C0NSTANCIA DE PAG0  F0RMULARI0 1683
IMPUEST0 A LA RENTA DE PRIMERA CATEG0RIA ARRENDAMIENT0
RUC de1 Arrendador  20131312955
Razon Socia1   INM0BILIARIA L0S 0LIV0S SAC
RUC de1 Arrendatario  20100047218
Peri0do Tributario   032026
Fecha de pago  15-04-2026
0peracion  0012345678
Imp0rte  S/ 1850,00
"""


@pytest.fixture
def perfil() -> PerfilSunatArrendamiento:
    return PerfilSunatArrendamiento()


def test_reconoce_una_constancia_limpia(perfil: PerfilSunatArrendamiento) -> None:
    assert perfil.reconoce(DOCUMENTO_LIMPIO)


def test_reconoce_la_constancia_aunque_el_ocr_la_destroce(
    perfil: PerfilSunatArrendamiento,
) -> None:
    """
    Reconocer solo el texto limpio dejaria fuera justo los documentos
    que mas necesitan el pipeline.
    """
    assert perfil.reconoce(DOCUMENTO_DEGRADADO)


# Formatos REALES que no decian "constancia de pago" ni "primera
# categoria": antes quedaban en una sola señal ("1683") y se descartaban.
_FORMATO_SOL = """\
Identificacion de la Transaccion:
Numero de Formulario: 1683
Datos Generales:
RUC: 10071840871
Periodo: 202510
Tributo: 3011 - Impuesto a la Renta de 1ra Categoria
Monto de Alquiler: S/ 6,500.00
Importe Pagado: S/ 329.00
"""

_FORMATO_BANCO_NACION = """\
BANCO DE LA NACION
Impuesto a la Renta 1ra.Categoria - Form 1683
RUC del arrendador : 10195656784  Periodo : 05/2026
Importe pagado : S/ 425.00
"""


@pytest.mark.parametrize("texto", [_FORMATO_SOL, _FORMATO_BANCO_NACION])
def test_reconoce_los_formatos_reales_de_sunat(
    perfil: PerfilSunatArrendamiento, texto: str
) -> None:
    """
    Regresion: SUNAT SOL y el Banco de la Nacion escriben "1ra Categoria"
    (no "primera categoria") e identifican el tributo por su codigo 3011.
    Sin reconocer esas variantes, el pipeline los descartaba y un correo
    con cuatro recibos de estos solo producia un registro.
    """
    assert perfil.reconoce(texto)


@pytest.mark.parametrize(
    "texto",
    [
        "Factura de electricidad del mes de marzo",
        "SUNAT le informa sobre su clave SOL",
        # Otra categoria de renta: comparte "impuesto a la renta" pero no
        # es arrendamiento (ni 1683, ni 3011, ni 1ra categoria).
        "Impuesto a la Renta de 3ra Categoria - Formulario 1662",
        "",
    ],
)
def test_no_reconoce_documentos_ajenos(perfil: PerfilSunatArrendamiento, texto: str) -> None:
    """
    Exigir dos señales evita gastar OCR y llamadas de pago en una
    factura de luz que alguien reenvio por error.
    """
    assert not perfil.reconoce(texto)


def test_extrae_los_ocho_campos_de_un_documento_limpio(
    perfil: PerfilSunatArrendamiento,
) -> None:
    campos = perfil.extraer_campos(DOCUMENTO_LIMPIO)
    assert set(campos) == {
        "ruc_contribuyente",
        "nombre_contribuyente",
        "ruc_inquilino",
        "nombre_inquilino",
        "periodo",
        "fecha_de_pago",
        "numero_de_operacion",
        "importe_pagado",
    }


def test_la_etiqueta_compuesta_no_se_cuela_en_el_valor(
    perfil: PerfilSunatArrendamiento,
) -> None:
    """
    "Nombre / Razon Social:" es una sola etiqueta. Sin modelarla
    entera, su segunda mitad acaba dentro del nombre leido.
    """
    campos = perfil.extraer_campos(DOCUMENTO_LIMPIO)
    assert campos["nombre_contribuyente"].valor == "INMOBILIARIA LOS OLIVOS SAC"


def test_extrae_del_documento_degradado(perfil: PerfilSunatArrendamiento) -> None:
    """Siete campos sobre un texto con ceros por oes y unos por eles."""
    campos = perfil.extraer_campos(DOCUMENTO_DEGRADADO)
    assert campos["ruc_contribuyente"].valor == "20131312955"
    assert campos["ruc_inquilino"].valor == "20100047218"
    assert campos["periodo"].valor == "032026"
    assert campos["fecha_de_pago"].valor == "15-04-2026"
    assert campos["numero_de_operacion"].valor == "0012345678"
    assert len(campos) >= 7


def test_la_confianza_premia_la_etiqueta_mas_especifica(
    perfil: PerfilSunatArrendamiento,
) -> None:
    """Acertar sobre "RUC del Arrendador" vale mas que hallar once digitos sueltos."""
    especifico = perfil.extraer_campos("RUC del Arrendador: 20131312955")
    generico = perfil.extraer_campos("RUC: 20131312955")
    assert especifico["ruc_contribuyente"].confianza > generico["ruc_contribuyente"].confianza


def test_un_valor_que_no_valida_se_conserva_penalizado(
    perfil: PerfilSunatArrendamiento,
) -> None:
    """
    Perder el dato obligaria a transcribirlo entero a mano, cuando lo
    normal es que solo haya que corregir un digito.
    """
    campos = perfil.extraer_campos("RUC del Arrendador: 20131312954")
    assert campos["ruc_contribuyente"].valor == "20131312954"
    assert not campos["ruc_contribuyente"].es_fiable


def test_convierte_los_campos_en_un_registro_con_objetos_de_valor(
    perfil: PerfilSunatArrendamiento,
) -> None:
    documento = DocumentoAExtraer(
        adjunto_id=uuid4(),
        tenant_id=TENANT_A,
        trabajo_id=uuid4(),
        contenido=b"",
        tipo_mime="application/pdf",
        nombre="c.pdf",
    )
    registro = perfil.a_registro(perfil.extraer_campos(DOCUMENTO_LIMPIO), documento)

    assert registro.ruc_contribuyente is not None
    assert registro.ruc_contribuyente.valor == "20131312955"
    assert str(registro.periodo) == "202603"
    assert str(registro.importe_pagado) == "1850.00"
    assert registro.importe_pagado is not None
    assert registro.importe_pagado.moneda == "PEN"


# Un recibo real tiene cuatro montos distintos; antes se colapsaban en
# uno solo y el motor guardaba cualquiera.
_DOCUMENTO_CON_VARIOS_MONTOS = """\
Numero de Formulario: 1683
RUC: 10071840871
Periodo: 202510
Tributo: 3011 - Impuesto a la Renta de 1ra Categoria
Documento Inquilino: RUC - 20563529378
Tipo de Bien: PREDIO
Monto de Alquiler: S/ 6,500.00
Tributo Resultante: S/ 325.00
Importe Pagado: S/ 329.00
"""


def test_separa_los_cuatro_montos_del_recibo(perfil: PerfilSunatArrendamiento) -> None:
    """
    Cada monto va a su campo: el alquiler pactado no es el tributo ni lo
    pagado. Anclar "Tributo: 3011" como codigo (no monto) es parte de
    esto: sin ello, 3011 entraba como importe.
    """
    campos = perfil.extraer_campos(_DOCUMENTO_CON_VARIOS_MONTOS)

    assert str(Importe.interpretar(campos["monto_alquiler"].valor)) == "6500.00"
    assert str(Importe.interpretar(campos["tributo_resultante"].valor)) == "325.00"
    assert str(Importe.interpretar(campos["importe_pagado"].valor)) == "329.00"
    assert campos["tipo_de_bien"].valor == "PREDIO"
    assert "tipo_doc_inquilino" in campos


def test_lee_un_importe_enmascarado_con_asteriscos(
    perfil: PerfilSunatArrendamiento,
) -> None:
    """El Banco de la Nacion imprime "S/ *********326.00"; el patron no debe
    romperse al ver los asteriscos entre el simbolo y el numero."""
    campos = perfil.extraer_campos("Importe pagado : S/ *********326.00")
    assert str(Importe.interpretar(campos["importe_pagado"].valor)) == "326.00"


# ─────────────────────────────────────────────────────────────────────
# Revision humana
# ─────────────────────────────────────────────────────────────────────


class RepositorioDeRevisionFalso(RepositorioDeRegistros):
    def __init__(self, registro: RegistroTributario | None) -> None:
        self.registro = registro
        self.actualizados: list[RegistroTributario] = []

    async def guardar(self, tenant_id: UUID, registro: RegistroTributario) -> RegistroTributario:
        return registro

    async def actualizar(self, ctx: TenantContext, registro: RegistroTributario) -> None:
        self.actualizados.append(registro)

    async def obtener(self, ctx: TenantContext, registro_id: UUID) -> RegistroTributario | None:
        return self.registro

    async def listar(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, pagina: SolicitudDePagina
    ) -> Pagina[RegistroTributario]:
        return Pagina(elementos=[])

    async def listar_para_reporte(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, limite: int
    ) -> list[RegistroTributario]:
        return []

    async def existe_para_adjunto(self, tenant_id: UUID, adjunto_id: UUID) -> bool:
        return False

    async def contar_pendientes_de_revision(self, ctx: TenantContext) -> int:
        return 0


def _pendiente(tenant_id: UUID = TENANT_A) -> RegistroTributario:
    return RegistroTributario(
        tenant_id=tenant_id,
        perfil="sunat_arrendamiento",
        estado_de_revision=EstadoDeRevision.PENDIENTE,
        confianza_por_campo={"ruc_contribuyente": 0.35},
    )


async def test_la_correccion_pasa_por_los_objetos_de_valor(
    contexto_a: TenantContext,
) -> None:
    """
    Una persona tambien teclea mal un RUC. Aceptarlo sin verificar
    dejaria entrar justo el error que el pipeline existe para evitar.
    """
    repositorio = RepositorioDeRevisionFalso(_pendiente())
    caso = RevisarRegistro(repositorio)

    with pytest.raises(ErrorDeValidacion, match="ruc_contribuyente"):
        await caso.corregir_y_aprobar(contexto_a, uuid4(), {"ruc_contribuyente": "20131312954"})


async def test_una_correccion_valida_se_aplica_y_aprueba(
    contexto_a: TenantContext,
) -> None:
    repositorio = RepositorioDeRevisionFalso(_pendiente())
    registro = await RevisarRegistro(repositorio).corregir_y_aprobar(
        contexto_a, uuid4(), {"ruc_contribuyente": "20131312955", "importe_pagado": "1850.00"}
    )

    assert registro.ruc_contribuyente is not None
    assert registro.ruc_contribuyente.valor == "20131312955"
    assert registro.estado_de_revision is EstadoDeRevision.APROBADO
    assert registro.revisado_por == contexto_a.user_id
    # Un campo corregido a mano pasa a confianza plena: lo valido una
    # persona mirando el documento.
    assert registro.confianza_por_campo["ruc_contribuyente"] == 1.0


async def test_rechaza_corregir_un_campo_que_no_es_corregible(
    contexto_a: TenantContext,
) -> None:
    """
    Se rechaza en vez de ignorarse en silencio: quien corrige debe
    saber que su cambio no se aplico.
    """
    repositorio = RepositorioDeRevisionFalso(_pendiente())
    with pytest.raises(ErrorDeValidacion, match="perfil"):
        await RevisarRegistro(repositorio).corregir_y_aprobar(
            contexto_a, uuid4(), {"perfil": "otro"}
        )


async def test_rechaza_una_correccion_vacia(contexto_a: TenantContext) -> None:
    repositorio = RepositorioDeRevisionFalso(_pendiente())
    with pytest.raises(ErrorDeValidacion):
        await RevisarRegistro(repositorio).corregir_y_aprobar(contexto_a, uuid4(), {})


async def test_no_se_puede_revisar_un_registro_de_otro_tenant(
    contexto_a: TenantContext, contexto_b: TenantContext
) -> None:
    repositorio = RepositorioDeRevisionFalso(_pendiente(contexto_b.tenant_id))
    with pytest.raises(ErrorDeAutorizacion):
        await RevisarRegistro(repositorio).aprobar_sin_cambios(contexto_a, uuid4())


async def test_rechazar_marca_el_registro_como_no_utilizable(
    contexto_a: TenantContext,
) -> None:
    repositorio = RepositorioDeRevisionFalso(_pendiente())
    registro = await RevisarRegistro(repositorio).rechazar(contexto_a, uuid4())
    assert registro.estado_de_revision is EstadoDeRevision.RECHAZADO


async def test_aprobar_lo_que_no_estaba_en_la_cola_no_lo_altera(
    contexto_a: TenantContext,
) -> None:
    """La interfaz puede mostrarlo desde el listado general; no es un error."""
    registro = RegistroTributario(
        tenant_id=TENANT_A, estado_de_revision=EstadoDeRevision.NO_REQUERIDA
    )
    repositorio = RepositorioDeRevisionFalso(registro)

    resultado = await RevisarRegistro(repositorio).aprobar_sin_cambios(contexto_a, uuid4())
    assert resultado.estado_de_revision is EstadoDeRevision.NO_REQUERIDA


def test_los_campos_dudosos_guian_al_revisor() -> None:
    """Sin esto, quien corrige compara el documento campo por campo."""
    registro = RegistroTributario(
        confianza_por_campo={
            "ruc_contribuyente": 0.98,
            "importe_pagado": 0.31,
            "periodo": 0.45,
        }
    )
    assert registro.campos_dudosos() == ["importe_pagado", "periodo"]


# ─────────────────────────────────────────────────────────────────────
# Generadores de reporte
# ─────────────────────────────────────────────────────────────────────


def _filas() -> list[FilaDeReporte]:
    return [
        FilaDeReporte(
            ruc_contribuyente="20131312955",
            nombre_contribuyente="INMOBILIARIA LOS OLIVOS SAC",
            periodo="03/2026",
            fecha_de_pago="15/04/2026",
            numero_de_operacion="0012345678",
            importe_pagado="1850.00",
            moneda="PEN",
            estado="complete",
            revision="not_required",
        ),
        FilaDeReporte(
            ruc_contribuyente="20100047218",
            nombre_contribuyente="COMERCIAL SAN MARTIN EIRL",
            periodo="03/2026",
            importe_pagado="",
            estado="partial",
            revision="pending",
        ),
    ]


def test_el_excel_se_genera_y_es_un_fichero_valido() -> None:
    contenido = GeneradorExcel().generar(_filas(), "Registros")
    # Firma de un .xlsx: es un ZIP.
    assert contenido[:2] == b"PK"
    assert len(contenido) > 1000


def test_el_excel_escribe_el_importe_como_numero() -> None:
    """
    Si la columna fuera texto no sumaria, y lo primero que hace quien
    recibe el reporte es seleccionarla para ver el total.
    """
    import io

    from openpyxl import load_workbook

    contenido = GeneradorExcel().generar(_filas(), "Registros")
    hoja = load_workbook(io.BytesIO(contenido)).active
    assert hoja is not None

    columna_importe = next(
        c for c, (clave, _) in enumerate(COLUMNAS, start=1) if clave == "importe_pagado"
    )
    celda = hoja.cell(row=2, column=columna_importe)
    assert isinstance(celda.value, (int, float))
    assert float(celda.value) == pytest.approx(1850.00)


def test_el_excel_lleva_la_cabecera_en_el_orden_definido() -> None:
    import io

    from openpyxl import load_workbook

    contenido = GeneradorExcel().generar(_filas(), "Registros")
    hoja = load_workbook(io.BytesIO(contenido)).active
    assert hoja is not None

    cabecera = [hoja.cell(row=1, column=i).value for i in range(1, len(COLUMNAS) + 1)]
    assert cabecera == [etiqueta for _, etiqueta in COLUMNAS]


def test_el_csv_lleva_bom_para_que_excel_lea_las_tildes() -> None:
    """
    Sin el BOM, Excel en Windows interpreta el fichero en la
    codificacion local y las tildes salen rotas. Es la queja numero
    uno con exportaciones en español.
    """
    contenido = GeneradorCsv().generar(_filas(), "Registros")
    assert contenido.startswith(b"\xef\xbb\xbf")


def test_el_csv_contiene_todas_las_filas() -> None:
    texto = GeneradorCsv().generar(_filas(), "Registros").decode("utf-8-sig")
    lineas = [linea for linea in texto.splitlines() if linea.strip()]
    assert len(lineas) == 3  # cabecera + dos filas
    assert "20131312955" in lineas[1]


def test_ambos_formatos_comparten_el_mismo_orden_de_columnas() -> None:
    """
    El formateo ocurre al construir la fila, no al escribir: asi el
    Excel y el CSV muestran exactamente lo mismo.
    """
    texto = GeneradorCsv().generar(_filas(), "R").decode("utf-8-sig")
    cabecera_csv = texto.splitlines()[0].split(";")
    assert cabecera_csv == [etiqueta for _, etiqueta in COLUMNAS]


def test_un_reporte_sin_filas_sigue_generando_la_cabecera() -> None:
    """Un Excel vacio pero valido es mejor que un error para el usuario."""
    contenido = GeneradorExcel().generar([], "Registros")
    assert contenido[:2] == b"PK"
