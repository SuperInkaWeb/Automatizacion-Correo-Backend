"""
Objetos de valor del dominio de extraccion.

Proposito
    Que un dato tributario invalido no pueda existir en el sistema. Cada
    uno valida en su constructor y es inmutable, asi que una vez creado
    ya no hay forma de dejarlo en un estado incorrecto.

Dependencias
    Solo biblioteca estandar.

Decision de diseño
    Validacion en el constructor y no en una funcion `validar_ruc()` que
    haya que acordarse de llamar. `Ruc("123")` lanza; a partir de ahi,
    cualquier funcion que reciba un `Ruc` sabe que es valido sin
    comprobar nada. Es la diferencia entre una regla que se aplica y una
    que se documenta.

    Cada objeto ofrece ademas `interpretar()`, que devuelve el valor o
    None en lugar de lanzar: es lo que usa el pipeline, donde un campo
    ilegible es un resultado esperado y no un error.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

# ─────────────────────────────────────────────────────────────────────
# RUC
# ─────────────────────────────────────────────────────────────────────

_LONGITUD_RUC: Final = 11
# Pesos del algoritmo de digito verificador de SUNAT, aplicados a los
# diez primeros digitos de izquierda a derecha.
_PESOS_RUC: Final = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
# Los dos primeros digitos identifican el tipo de contribuyente. Un RUC
# que empiece por otro valor no existe, aunque el digito verificador
# cuadre por casualidad.
_PREFIJOS_VALIDOS: Final = frozenset({"10", "15", "16", "17", "20"})


@dataclass(frozen=True, slots=True)
class Ruc:
    """
    Registro Unico de Contribuyentes peruano: once digitos con digito
    verificador modulo 11.

    Verificar el digito no es cosmetico: el OCR confunde con frecuencia
    0/O, 1/l y 5/S, y sin esta comprobacion esos errores entran en el
    reporte como RUC aparentemente validos. Con ella, una cifra mal
    leida se detecta en el 90 % de los casos.
    """

    valor: str

    def __post_init__(self) -> None:
        if not self.es_valido(self.valor):
            raise ValueError(f"RUC invalido: {self.valor!r}")

    @staticmethod
    def es_valido(crudo: str) -> bool:
        if len(crudo) != _LONGITUD_RUC or not crudo.isdigit():
            return False
        if crudo[:2] not in _PREFIJOS_VALIDOS:
            return False
        return Ruc._digito_verificador(crudo[:10]) == int(crudo[10])

    @staticmethod
    def _digito_verificador(diez_digitos: str) -> int:
        suma = sum(int(d) * peso for d, peso in zip(diez_digitos, _PESOS_RUC, strict=True))
        resto = suma % 11
        # La regla de SUNAT: 11 - resto, y los resultados 10 y 11 se
        # colapsan a 0 y 1 respectivamente.
        return (11 - resto) % 10

    @classmethod
    def interpretar(cls, crudo: str | None) -> Ruc | None:
        """Extrae un RUC de un texto sucio. Devuelve None si no hay ninguno valido."""
        if not crudo:
            return None
        solo_digitos = re.sub(r"\D", "", crudo)
        if cls.es_valido(solo_digitos):
            return cls(solo_digitos)
        # El OCR suele pegar el RUC a la etiqueta o partirlo con espacios.
        # Se prueban todas las ventanas de once digitos antes de rendirse.
        for inicio in range(len(solo_digitos) - _LONGITUD_RUC + 1):
            ventana = solo_digitos[inicio : inicio + _LONGITUD_RUC]
            if cls.es_valido(ventana):
                return cls(ventana)
        return None

    @property
    def es_persona_natural(self) -> bool:
        return self.valor.startswith("10")

    def __str__(self) -> str:
        return self.valor


# ─────────────────────────────────────────────────────────────────────
# Periodo tributario
# ─────────────────────────────────────────────────────────────────────

_ANIO_MINIMO: Final = 1993  # creacion de la SUNAT en su forma actual
_ANIO_MAXIMO: Final = 2100

_MESES_EN_TEXTO: Final = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "setiembre": 9,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


@dataclass(frozen=True, slots=True)
class PeriodoTributario:
    """
    Periodo mensual, normalizado a YYYYMM.

    La normalizacion importa porque el mismo periodo aparece en los
    documentos como "03/2026", "2026-03", "marzo 2026" o "202603". Sin
    unificarlo, agrupar por periodo en el reporte produce cuatro filas
    donde deberia haber una.
    """

    anio: int
    mes: int

    def __post_init__(self) -> None:
        if not _ANIO_MINIMO <= self.anio <= _ANIO_MAXIMO:
            raise ValueError(f"Año fuera de rango: {self.anio}")
        if not 1 <= self.mes <= 12:
            raise ValueError(f"Mes fuera de rango: {self.mes}")

    @classmethod
    def interpretar(cls, crudo: str | None) -> PeriodoTributario | None:
        if not crudo:
            return None
        texto = _sin_tildes(crudo.strip().lower())

        # "marzo 2026" / "marzo de 2026"
        for nombre, numero in _MESES_EN_TEXTO.items():
            if nombre in texto:
                anio = re.search(r"(19|20)\d{2}", texto)
                if anio:
                    return cls(int(anio.group()), numero)

        # 202603 o 03/2026 o 2026-03 o 03-2026
        compacto = re.search(r"\b((?:19|20)\d{2})(0[1-9]|1[0-2])\b", texto)
        if compacto:
            return cls(int(compacto.group(1)), int(compacto.group(2)))

        separado = re.search(r"\b(0?[1-9]|1[0-2])\s*[/\-.]\s*((?:19|20)\d{2})\b", texto)
        if separado:
            return cls(int(separado.group(2)), int(separado.group(1)))

        invertido = re.search(r"\b((?:19|20)\d{2})\s*[/\-.]\s*(0?[1-9]|1[0-2])\b", texto)
        if invertido:
            return cls(int(invertido.group(1)), int(invertido.group(2)))

        # MMYYYY pegado ("032026"), que es lo que queda cuando el OCR se
        # come el separador. Se intenta el ultimo por ser el mas
        # ambiguo: si los cuatro primeros digitos formaran un año
        # valido, el patron YYYYMM de arriba ya habria ganado.
        pegado = re.search(r"\b(0[1-9]|1[0-2])((?:19|20)\d{2})\b", texto)
        if pegado:
            return cls(int(pegado.group(2)), int(pegado.group(1)))

        return None

    def __str__(self) -> str:
        return f"{self.anio:04d}{self.mes:02d}"

    @property
    def legible(self) -> str:
        return f"{self.mes:02d}/{self.anio:04d}"


# ─────────────────────────────────────────────────────────────────────
# Importe
# ─────────────────────────────────────────────────────────────────────

_MONEDAS: Final = {
    "s/": "PEN",
    "s/.": "PEN",
    "soles": "PEN",
    "pen": "PEN",
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "dolares": "USD",
}
_IMPORTE_MAXIMO: Final = Decimal("999999999.99")


@dataclass(frozen=True, slots=True)
class Importe:
    """
    Cantidad monetaria con moneda explicita.

    `Decimal` y no `float`: con float, 0.1 + 0.2 no es 0.3, y en un
    reporte tributario que debe cuadrar al centimo eso es inaceptable.
    """

    cantidad: Decimal
    moneda: str = "PEN"

    def __post_init__(self) -> None:
        if self.cantidad < 0:
            raise ValueError("El importe no puede ser negativo")
        if self.cantidad > _IMPORTE_MAXIMO:
            raise ValueError("El importe excede el maximo representable")
        if len(self.moneda) != 3 or not self.moneda.isalpha():
            raise ValueError(f"Moneda invalida: {self.moneda!r}")

    @classmethod
    def interpretar(cls, crudo: str | None) -> Importe | None:
        if not crudo:
            return None
        texto = crudo.strip().lower()

        moneda = "PEN"
        for simbolo, codigo in _MONEDAS.items():
            if simbolo in texto:
                moneda = codigo
                break

        numero = re.search(r"\d[\d\s.,]*\d|\d", texto)
        if not numero:
            return None

        try:
            return cls(_a_decimal(numero.group()), moneda)
        except (InvalidOperation, ValueError):
            return None

    def __str__(self) -> str:
        return f"{self.cantidad:.2f}"


def _a_decimal(crudo: str) -> Decimal:
    """
    Convierte un numero escrito en formato peruano o anglosajon.

    El caso ambiguo es "1.234": puede ser mil doscientos treinta y cuatro
    con separador de miles, o uno coma doscientos treinta y cuatro. Se
    resuelve por el ultimo separador presente, que es el decimal en ambas
    convenciones cuando hay dos.
    """
    limpio = crudo.replace(" ", "")
    tiene_coma = "," in limpio
    tiene_punto = "." in limpio

    if tiene_coma and tiene_punto:
        # Con los dos separadores, el ultimo es el decimal en ambas
        # convenciones: 1.234,56 (europeo) y 1,234.56 (anglosajon).
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif tiene_coma or tiene_punto:
        sep = "," if tiene_coma else "."
        if limpio.count(sep) > 1:
            # Varias apariciones del mismo separador solo pueden agrupar
            # miles: 1,234,567 -> 1234567.
            limpio = limpio.replace(sep, "")
        else:
            entero, _, frac = limpio.partition(sep)
            # Tres cifras tras un unico separador son miles (6,500 = seis
            # mil quinientos, no 6.50); una o dos son los centimos.
            limpio = entero + frac if len(frac) == 3 else f"{entero}.{frac}"

    return Decimal(limpio).quantize(Decimal("0.01"))


# ─────────────────────────────────────────────────────────────────────
# Fecha de pago
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FechaDePago:
    """
    Fecha de un pago, siempre interpretada como DD/MM/YYYY.

    En Peru el formato es dia/mes/año. Aceptar tambien MM/DD daria
    ambiguedad irresoluble en los doce dias de cada mes en que ambos
    numeros son validos, y un error de fecha en un reporte tributario
    cambia el periodo al que se imputa el pago.
    """

    valor: date

    @classmethod
    def interpretar(cls, crudo: str | None) -> FechaDePago | None:
        if not crudo:
            return None
        texto = crudo.strip()

        coincidencia = re.search(r"\b(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})\b", texto)
        if coincidencia:
            dia, mes, anio = (int(g) for g in coincidencia.groups())
            # Un año de dos cifras se interpreta en el siglo actual: no
            # hay documentos tributarios de 1926 en este sistema.
            if anio < 100:
                anio += 2000
            return cls._construir(anio, mes, dia)

        iso = re.search(r"\b((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})\b", texto)
        if iso:
            anio, mes, dia = (int(g) for g in iso.groups())
            return cls._construir(anio, mes, dia)

        return None

    @classmethod
    def _construir(cls, anio: int, mes: int, dia: int) -> FechaDePago | None:
        try:
            return cls(date(anio, mes, dia))
        except ValueError:
            # Fecha imposible (31 de febrero, mes 13). El OCR las produce
            # al confundir digitos; descartarla es mejor que inventar una.
            return None

    def __str__(self) -> str:
        return self.valor.isoformat()

    @property
    def legible(self) -> str:
        return self.valor.strftime("%d/%m/%Y")


# ─────────────────────────────────────────────────────────────────────
# Numero de operacion
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NumeroDeOperacion:
    """
    Identificador del pago ante el banco o la SUNAT.

    Se limita a alfanumericos y guiones: el OCR arrastra con frecuencia
    la etiqueta o signos de puntuacion pegados al valor, y eso rompe
    luego el cruce con los extractos bancarios.
    """

    valor: str

    def __post_init__(self) -> None:
        if not 4 <= len(self.valor) <= 40:
            raise ValueError(f"Numero de operacion de longitud invalida: {self.valor!r}")
        if not re.fullmatch(r"[A-Za-z0-9\-]+", self.valor):
            raise ValueError(f"Numero de operacion con caracteres invalidos: {self.valor!r}")

    @classmethod
    def interpretar(cls, crudo: str | None) -> NumeroDeOperacion | None:
        if not crudo:
            return None
        limpio = re.sub(r"[^A-Za-z0-9\-]", "", crudo.strip())
        try:
            return cls(limpio)
        except ValueError:
            return None

    def __str__(self) -> str:
        return self.valor


def _sin_tildes(texto: str) -> str:
    """Normaliza para comparar: 'Setiembre' y 'setiembre' deben coincidir."""
    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
