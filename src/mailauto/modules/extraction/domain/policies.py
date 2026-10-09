"""
Politicas de extraccion: reglas puras, sin dependencias.

Proposito
    Decidir cuando un resultado es suficientemente bueno, como combinar
    lo que leyeron varias estrategias y que registros necesitan que los
    mire una persona.

Dependencias
    Solo el dominio propio. Es el fichero mas testeable del sistema y el
    que concentra las decisiones de calidad.
"""

from __future__ import annotations

from mailauto.modules.extraction.domain.entities import (
    UMBRAL_DE_CONFIANZA,
    CampoExtraido,
    Completitud,
    EstadoDeRevision,
    ResultadoDeEstrategia,
)

# Campos sin los cuales el registro no sirve para nada: identifican al
# contribuyente, al periodo y al dinero. Si falta alguno, no hay fila
# que llevar al reporte.
CAMPOS_IMPRESCINDIBLES: frozenset[str] = frozenset(
    {"ruc_contribuyente", "periodo", "importe_pagado"}
)

# Campos que enriquecen el registro pero cuya ausencia no lo invalida.
CAMPOS_DESEABLES: frozenset[str] = frozenset(
    {
        "nombre_contribuyente",
        "ruc_inquilino",
        "nombre_inquilino",
        "tipo_doc_inquilino",
        "tipo_de_bien",
        "fecha_de_pago",
        "numero_de_operacion",
        "monto_alquiler",
        "tributo_resultante",
        "intereses_moratorios",
    }
)


def es_suficiente_para_detenerse(resultado: ResultadoDeEstrategia) -> bool:
    """
    ¿Vale la pena seguir probando estrategias mas caras?

    Se corta solo si TODOS los campos imprescindibles estan presentes y
    por encima del umbral. Conformarse con la confianza media dejaria
    pasar un resultado con el RUC perfecto y el importe ilegible, que es
    precisamente el caso en que una estrategia mejor aporta algo.
    """
    for nombre in CAMPOS_IMPRESCINDIBLES:
        campo = resultado.campos.get(nombre)
        if campo is None or not campo.es_fiable:
            return False
    return True


def combinar(
    resultados: list[ResultadoDeEstrategia],
) -> dict[str, CampoExtraido]:
    """
    Toma el mejor valor de cada campo entre todas las estrategias.

    Campo a campo y no "la mejor estrategia completa": es habitual que
    el texto nativo del PDF lea el RUC sin margen de error y falle en un
    importe que esta dentro de una imagen incrustada, donde el OCR si
    acierta. Quedarse con un solo motor desperdiciaria la mitad de lo
    que ya se leyo y se pago.

    Ante un empate exacto de confianza gana el primero, que por el orden
    del pipeline es el de la estrategia mas barata y fiable.
    """
    mejores: dict[str, CampoExtraido] = {}
    for resultado in resultados:
        if not resultado.tuvo_exito:
            continue
        for nombre, campo in resultado.campos.items():
            actual = mejores.get(nombre)
            if actual is None or campo.confianza > actual.confianza:
                mejores[nombre] = campo
    return mejores


def clasificar(campos: dict[str, CampoExtraido]) -> Completitud:
    """Califica el resultado global a partir de los campos reunidos."""
    presentes = {n for n, c in campos.items() if c.valor.strip()}

    if not presentes:
        return Completitud.VACIO
    if not CAMPOS_IMPRESCINDIBLES.issubset(presentes):
        return Completitud.PARCIAL

    # Estan todos los imprescindibles: queda mirar si alguno es dudoso.
    hay_dudoso = any(not campos[nombre].es_fiable for nombre in CAMPOS_IMPRESCINDIBLES)
    return Completitud.PARCIAL if hay_dudoso else Completitud.COMPLETO


def decidir_revision(
    completitud: Completitud, campos: dict[str, CampoExtraido]
) -> EstadoDeRevision:
    """
    ¿Necesita este registro que lo mire una persona?

    Un registro vacio no entra en la cola: no hay nada que corregir, el
    documento simplemente no era lo que se esperaba. Llenar la cola de
    revision con esos casos la vuelve inutil, que es la forma mas
    segura de que nadie la use.
    """
    if completitud is Completitud.VACIO:
        return EstadoDeRevision.NO_REQUERIDA
    if completitud is Completitud.PARCIAL:
        return EstadoDeRevision.PENDIENTE

    # Completo, pero con algun campo deseable dudoso: tambien se revisa.
    # Es barato de corregir y evita que un nombre mal leido llegue al
    # reporte con apariencia de dato bueno.
    hay_deseable_dudoso = any(
        nombre in CAMPOS_DESEABLES and not campo.es_fiable for nombre, campo in campos.items()
    )
    return EstadoDeRevision.PENDIENTE if hay_deseable_dudoso else EstadoDeRevision.NO_REQUERIDA


def confianza_global(campos: dict[str, CampoExtraido]) -> float:
    """
    Confianza del registro, ponderando mas los campos imprescindibles.

    Una media simple permitiria que cinco campos accesorios perfectos
    compensaran un importe ilegible, y el registro pareceria fiable
    cuando el dato que importa no lo es.
    """
    if not campos:
        return 0.0

    peso_total = 0.0
    acumulado = 0.0
    for nombre, campo in campos.items():
        peso = 3.0 if nombre in CAMPOS_IMPRESCINDIBLES else 1.0
        acumulado += campo.confianza * peso
        peso_total += peso

    return acumulado / peso_total


def supera_el_umbral(confianza: float) -> bool:
    return confianza >= UMBRAL_DE_CONFIANZA
