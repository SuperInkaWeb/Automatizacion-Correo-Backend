"""
Perfil de extraccion: constancia SUNAT de pago de arrendamiento.

Proposito
    Localizar los campos del formulario 1683 en el texto de un
    documento, sea cual sea el motor que lo haya leido.

Dependencias
    Objetos de valor del dominio. No conoce PDFs, pixeles ni OCR.

Decisiones de diseño
    1. Las etiquetas se buscan con tolerancia a las confusiones tipicas
       del OCR (0/O, 1/l/i, 5/S, 8/B, 2/Z) y a las tildes perdidas. Un
       patron literal funciona con el texto nativo de un PDF y falla
       justo en los documentos escaneados, que son los que de verdad
       necesitan el pipeline. La tolerancia se aplica SOLO a las
       etiquetas: en los valores, un 0 y una O significan cosas
       distintas y confundirlos corrompe el dato.

    2. Cada campo tiene varios patrones, de mas especifico a mas
       general, cada uno con su confianza. Acertar sobre la etiqueta
       exacta merece mas credito que encontrar once digitos sueltos.

    3. El valor pasa siempre por su objeto de valor. Un RUC que no
       supere el digito verificador se descarta aunque el patron haya
       coincidido: el patron dice donde mirar, el objeto de valor dice
       si lo leido tiene sentido.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from mailauto.modules.extraction.domain.entities import (
    CampoExtraido,
    Estrategia,
    RegistroTributario,
)
from mailauto.modules.extraction.domain.ports import (
    DocumentoAExtraer,
    PerfilDeExtraccion,
)
from mailauto.modules.extraction.domain.value_objects import (
    FechaDePago,
    Importe,
    NumeroDeOperacion,
    PeriodoTributario,
    Ruc,
)

NOMBRE_DEL_PERFIL: Final = "sunat_arrendamiento"


# ─────────────────────────────────────────────────────────────────────
# Tolerancia a las confusiones del OCR
# ─────────────────────────────────────────────────────────────────────

# Cada letra y las cifras con las que el OCR la confunde habitualmente,
# mas sus variantes acentuadas. Construir las clases a partir de esta
# tabla evita escribir a mano `[o0]p[e3]r[a4]c[i1l][o0]n` en cada
# patron, que es ilegible y se desincroniza a la primera correccion.
_CONFUSIONES: Final[dict[str, str]] = {
    "a": "aá4@",
    "b": "b8",
    "e": "eé3",
    "g": "g9",
    "i": "ií1l|",
    "l": "l1i|",
    "n": "nñ",
    "o": "oó0",
    "s": "s5$",
    "t": "t7",
    "u": "uú",
    "z": "z2",
}


def _tolerante(palabra: str) -> str:
    """
    Convierte una palabra en una expresion que la reconoce aunque el
    OCR haya cambiado alguna letra por su cifra parecida.

    Los espacios se vuelven `\\s*`, porque el OCR tanto los pierde como
    los inventa dentro de una misma etiqueta.
    """
    partes: list[str] = []
    for caracter in palabra:
        if caracter == " ":
            partes.append(r"\s*")
        elif caracter in _CONFUSIONES:
            partes.append(f"[{re.escape(_CONFUSIONES[caracter])}]")
        else:
            partes.append(re.escape(caracter))
    return "".join(partes)


# Separador entre etiqueta y valor: los dos puntos se pierden con
# frecuencia y quedan solo espacios.
_SEP: Final = r"\s*[:.\-]?\s*"


@dataclass(frozen=True, slots=True)
class _Patron:
    """Una forma de encontrar un campo, con la confianza que merece."""

    expresion: re.Pattern[str]
    confianza: float


def _compilar(patron: str, confianza: float) -> _Patron:
    return _Patron(re.compile(patron, re.IGNORECASE | re.MULTILINE), confianza)


# ── Fragmentos reutilizados ──────────────────────────────────────────

_ARRENDADOR: Final = (
    f"(?:{_tolerante('arrendador')}|{_tolerante('contribuyente')}|{_tolerante('declarante')})"
)
_ARRENDATARIO: Final = f"(?:{_tolerante('arrendatario')}|{_tolerante('inquilino')})"
_DEL: Final = rf"(?:\s*{_tolerante('de')}[l1i]?\s*)?"

# "Nombre", "Razon Social", "Apellidos y Nombres" y la forma compuesta
# "Nombre / Razon Social", que es la que trae el formulario. Sin
# modelar la compuesta entera, su segunda mitad acaba dentro del valor.
_NOMBRE_BASE: Final = (
    f"(?:{_tolerante('nombres')}?|{_tolerante('apellidos y nombres')}"
    f"|{_tolerante('razon social')}|{_tolerante('razon socia')}[l1i])"
)
_ETIQUETA_NOMBRE: Final = rf"{_NOMBRE_BASE}(?:\s*[/y]\s*{_NOMBRE_BASE})?"

_DIGITOS_RUC: Final = r"(\d[\d\s\-]{9,16})"
_FECHA: Final = r"(\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4})"


# ── Patrones por campo ───────────────────────────────────────────────
#
# El grupo 1 de cada expresion es siempre el valor buscado.

_PATRONES: Final[dict[str, tuple[_Patron, ...]]] = {
    "ruc_contribuyente": (
        _compilar(rf"{_tolerante('ruc')}{_DEL}{_ARRENDADOR}{_SEP}{_DIGITOS_RUC}", 0.98),
        _compilar(rf"{_ARRENDADOR}{_SEP}[^\n]{{0,40}}?(\d{{11}})", 0.90),
        _compilar(rf"\b{_tolerante('ruc')}{_SEP}{_DIGITOS_RUC}", 0.85),
    ),
    "nombre_contribuyente": (
        _compilar(rf"{_ETIQUETA_NOMBRE}{_DEL}{_ARRENDADOR}{_SEP}([^\n]{{4,120}})", 0.94),
        _compilar(rf"{_ETIQUETA_NOMBRE}\s*[:]\s*([^\n]{{4,120}})", 0.90),
        _compilar(rf"{_ETIQUETA_NOMBRE}\s{{2,}}([A-ZÁÉÍÓÚÑ][^\n]{{3,120}})", 0.84),
    ),
    "ruc_inquilino": (
        _compilar(
            rf"(?:{_tolerante('ruc')}|{_tolerante('doc')}){_DEL}{_ARRENDATARIO}{_SEP}{_DIGITOS_RUC}",
            0.96,
        ),
        _compilar(rf"{_ARRENDATARIO}{_SEP}[^\n]{{0,40}}?(\d{{11}})", 0.88),
    ),
    "nombre_inquilino": (
        _compilar(rf"{_ETIQUETA_NOMBRE}{_DEL}{_ARRENDATARIO}{_SEP}([^\n]{{4,120}})", 0.94),
        _compilar(rf"{_ARRENDATARIO}\s*[:]\s*([A-ZÁÉÍÓÚÑ][^\n]{{3,120}})", 0.84),
    ),
    "periodo": (
        _compilar(
            rf"{_tolerante('periodo')}(?:\s*{_tolerante('tributario')})?{_SEP}"
            r"([0-9]{4}\s*[-/]?\s*[0-9]{2}|[0-9]{2}\s*[-/]?\s*[0-9]{4}|[a-zñáéíóú]+\s+(?:de\s+)?[0-9]{4})",
            0.96,
        ),
        _compilar(
            rf"{_tolerante('mes')}(?:\s*[yY]?\s*{_tolerante('ano')})?{_SEP}"
            r"([0-9]{2}\s*[-/]\s*[0-9]{4})",
            0.88,
        ),
    ),
    "fecha_de_pago": (
        _compilar(rf"{_tolerante('fecha')}{_DEL}{_tolerante('pago')}{_SEP}{_FECHA}", 0.96),
        _compilar(
            rf"{_tolerante('fecha')}{_DEL}(?:{_tolerante('presentacion')}|{_tolerante('emision')}){_SEP}{_FECHA}",
            0.86,
        ),
    ),
    "numero_de_operacion": (
        _compilar(
            rf"(?:{_tolerante('nro')}|{_tolerante('numero')}|{_tolerante('num')}|n)?\.?\s*"
            rf"{_DEL}{_tolerante('operacion')}{_SEP}([A-Z0-9\-]{{4,30}})",
            0.95,
        ),
        _compilar(
            rf"(?:{_tolerante('nro')}|{_tolerante('numero')})?\.?\s*{_DEL}{_tolerante('orden')}{_SEP}([A-Z0-9\-]{{4,30}})",
            0.82,
        ),
    ),
    "importe": (
        _compilar(
            rf"(?:{_tolerante('importe')}|{_tolerante('monto')}|{_tolerante('total')})"
            rf"(?:\s*(?:{_tolerante('pagado')}|{_tolerante('a pagar')}))?{_SEP}"
            r"((?:s\s*/\.?|us\$|\$)?\s*[\d.,]{1,18})",
            0.95,
        ),
        _compilar(r"\bs\s*/\.?\s*([\d.,]{1,18})", 0.82),
    ),
}

# Validador por campo. El patron dice donde mirar; esto dice si lo que
# hay ahi es un dato real.
_VALIDADORES: Final = {
    "ruc_contribuyente": Ruc.interpretar,
    "ruc_inquilino": Ruc.interpretar,
    "periodo": PeriodoTributario.interpretar,
    "fecha_de_pago": FechaDePago.interpretar,
    "numero_de_operacion": NumeroDeOperacion.interpretar,
    "importe": Importe.interpretar,
}

# Penalizacion cuando el patron coincidio pero el valor no supero su
# validador. El dato se conserva porque suele estar casi bien, pero
# queda marcado para que lo mire una persona.
_PENALIZACION_SIN_VALIDAR: Final = 0.45

# Señales de que el documento es una constancia de arrendamiento. Se
# exigen dos: "1683" por si solo aparece en cualquier correo de la
# entidad y bastaria para aceptar un documento que no es el buscado.
#
# Se cubren las variantes REALES que emiten SUNAT SOL ("Identificacion de
# la Transaccion"), el Banco de la Nacion y pagalo.pe. Estas no dicen
# "primera categoria" sino "1ra Categoria", y no repiten "constancia de
# pago"; identifican el tributo por su nombre ("impuesto a la renta") o
# por su codigo (3011 = renta de 1ra categoria). Sin estas señales, esos
# formatos quedaban en una sola coincidencia (solo "1683") y el pipeline
# los descartaba por completo en vez de extraerlos.
_SEÑALES: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        _tolerante("arrendamiento"),
        r"\b1683\b",
        r"\b3011\b",  # codigo del tributo "renta de 1ra categoria"
        _tolerante("impuesto a la renta"),
        _tolerante("constancia de pago"),
        _tolerante("primera categoria"),
        r"1ra\s*\.?\s*categor",  # "1ra Categoria", "1RA. CATEGOR."
        _tolerante("recibo por arrendamiento"),
        _tolerante("renta de primera"),
    )
)
_SEÑALES_MINIMAS: Final = 2

_LONGITUD_MAXIMA_NOMBRE: Final = 120


class PerfilSunatArrendamiento(PerfilDeExtraccion):
    """Constancia de pago del impuesto a la renta de primera categoria."""

    @property
    def nombre(self) -> str:
        return NOMBRE_DEL_PERFIL

    def reconoce(self, texto: str) -> bool:
        normalizado = _normalizar(texto)
        encontradas = sum(1 for señal in _SEÑALES if señal.search(normalizado))
        return encontradas >= _SEÑALES_MINIMAS

    def extraer_campos(self, texto: str) -> dict[str, CampoExtraido]:
        campos: dict[str, CampoExtraido] = {}
        for nombre, patrones in _PATRONES.items():
            campo = self._buscar(nombre, patrones, texto)
            if campo is not None:
                campos[nombre] = campo
        return campos

    def a_registro(
        self, campos: dict[str, CampoExtraido], documento: DocumentoAExtraer
    ) -> RegistroTributario:
        def crudo(nombre: str) -> str | None:
            campo = campos.get(nombre)
            return campo.valor if campo else None

        return RegistroTributario(
            tenant_id=documento.tenant_id,
            adjunto_id=documento.adjunto_id,
            trabajo_id=documento.trabajo_id,
            perfil=self.nombre,
            ruc_contribuyente=Ruc.interpretar(crudo("ruc_contribuyente")),
            nombre_contribuyente=_limpiar_nombre(crudo("nombre_contribuyente")),
            ruc_inquilino=Ruc.interpretar(crudo("ruc_inquilino")),
            nombre_inquilino=_limpiar_nombre(crudo("nombre_inquilino")),
            periodo=PeriodoTributario.interpretar(crudo("periodo")),
            fecha_de_pago=FechaDePago.interpretar(crudo("fecha_de_pago")),
            numero_de_operacion=NumeroDeOperacion.interpretar(crudo("numero_de_operacion")),
            importe=Importe.interpretar(crudo("importe")),
            campos_crudos={n: c.valor for n, c in campos.items()},
            confianza_por_campo={n: c.confianza for n, c in campos.items()},
        )

    # ── Interno ──────────────────────────────────────────────────────

    def _buscar(
        self, nombre: str, patrones: tuple[_Patron, ...], texto: str
    ) -> CampoExtraido | None:
        """
        Prueba los patrones en orden y devuelve el primero cuyo valor
        supere el validador del campo.

        Si ninguno valida pero alguno coincidio, se devuelve con
        confianza penalizada en lugar de descartarlo: perder el dato
        obligaria a transcribirlo entero a mano, cuando lo habitual es
        que solo haya que corregir un digito.
        """
        mejor_sin_validar: CampoExtraido | None = None
        validador = _VALIDADORES.get(nombre)

        for patron in patrones:
            coincidencia = patron.expresion.search(texto)
            if coincidencia is None:
                continue

            valor = coincidencia.group(1).strip()
            if not valor:
                continue

            if validador is None:
                # Campos de texto libre (nombres): no hay nada que validar.
                return CampoExtraido(valor, patron.confianza, Estrategia.TEXTO_NATIVO)

            if validador(valor) is not None:
                return CampoExtraido(valor, patron.confianza, Estrategia.TEXTO_NATIVO)

            if mejor_sin_validar is None:
                mejor_sin_validar = CampoExtraido(
                    valor,
                    round(patron.confianza * _PENALIZACION_SIN_VALIDAR, 3),
                    Estrategia.TEXTO_NATIVO,
                )

        return mejor_sin_validar


def _normalizar(texto: str) -> str:
    """Minusculas, sin tildes y con espacios colapsados, para comparar."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sin_tildes)


def _limpiar_nombre(crudo: str | None) -> str:
    """
    Recorta la basura que el OCR arrastra detras de un nombre.

    Lo tipico es que la siguiente etiqueta del formulario se pegue al
    valor ("JUAN PEREZ RUC 10..."), asi que se corta en el primer
    indicio de que empieza otro campo.
    """
    if not crudo:
        return ""
    corte = re.split(
        r"\s{3,}|\s+(?:ruc|periodo|fecha|importe|monto|operacion)\b",
        crudo,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return re.sub(r"[\s:;,.\-]+$", "", corte.strip())[:_LONGITUD_MAXIMA_NOMBRE]
