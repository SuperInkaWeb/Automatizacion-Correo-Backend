"""
Entidades del dominio de extraccion.

Proposito
    Representar lo que se extrae de un documento, con cuanta confianza
    y por que estrategia, para poder decidir si vale tal cual o necesita
    que lo revise una persona.

Dependencias
    Objetos de valor del propio dominio y `shared`.

Decision de diseño
    La confianza se guarda por campo, no por documento. Un registro
    puede tener el RUC perfectamente legible y el importe borroso; con
    una sola cifra global habria que descartar el documento entero o
    aceptarlo con un dato dudoso. Por campo, la cola de revision puede
    señalar exactamente que mirar, y la correccion cuesta segundos en
    vez de minutos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from mailauto.modules.extraction.domain.value_objects import (
    FechaDePago,
    Importe,
    NumeroDeOperacion,
    PeriodoTributario,
    Ruc,
)
from mailauto.shared.types import ahora_utc, uuid7

# Umbral por debajo del cual un campo se considera dudoso y arrastra el
# registro a revision humana. 0.80 sale de la practica: el texto nativo
# de un PDF ronda 1.0, el OCR sobre un documento limpio 0.85-0.95, y por
# debajo de 0.80 los errores de lectura dejan de ser anecdoticos.
UMBRAL_DE_CONFIANZA = 0.80


class Completitud(StrEnum):
    """Calidad global del resultado de una extraccion."""

    COMPLETO = "complete"
    PARCIAL = "partial"
    VACIO = "empty"


class EstadoDeRevision(StrEnum):
    NO_REQUERIDA = "not_required"
    PENDIENTE = "pending"
    APROBADO = "approved"
    RECHAZADO = "rejected"


class Estrategia(StrEnum):
    """
    Motores de lectura, en orden de costo creciente.

    El orden del enum es el orden en que se intentan: la primera que
    alcanza el umbral gana y las siguientes no llegan a ejecutarse.
    """

    TEXTO_NATIVO = "native_pdf_text"
    TABLAS_PDF = "pdf_tables"
    OCR_LOCAL = "local_ocr"
    VISION_IA = "vision_ai"


@dataclass(frozen=True, slots=True)
class CampoExtraido:
    """Un valor leido del documento, con su confianza y su procedencia."""

    valor: str
    confianza: float
    estrategia: Estrategia

    def __post_init__(self) -> None:
        if not 0.0 <= self.confianza <= 1.0:
            raise ValueError(f"Confianza fuera de rango: {self.confianza}")

    @property
    def es_fiable(self) -> bool:
        return self.confianza >= UMBRAL_DE_CONFIANZA


@dataclass(slots=True)
class ResultadoDeEstrategia:
    """
    Lo que devuelve un motor de lectura: campos crudos con su confianza.

    Crudos a proposito: la estrategia lee texto, no interpreta dominio.
    La conversion a objetos de valor la hace el agregador, en un unico
    sitio, de modo que todas las estrategias se benefician de la misma
    interpretacion y no hay cuatro versiones del parseo de fechas.
    """

    estrategia: Estrategia
    campos: dict[str, CampoExtraido] = field(default_factory=dict)
    texto_crudo: str = ""
    duracion_ms: int = 0
    error: str | None = None

    @property
    def tuvo_exito(self) -> bool:
        return self.error is None and bool(self.campos)

    def confianza_media(self) -> float:
        if not self.campos:
            return 0.0
        return sum(c.confianza for c in self.campos.values()) / len(self.campos)


@dataclass(slots=True)
class RegistroTributario:
    """
    Un pago extraido de un documento, listo para el reporte.

    Los campos tipados estan separados de `campos_crudos` a proposito:
    los primeros son los que se consultan e indexan, el segundo conserva
    lo que el perfil leyo tal cual, para poder diagnosticar una
    extraccion dudosa sin volver a abrir el adjunto.
    """

    id: UUID = field(default_factory=uuid7)
    tenant_id: UUID = field(default_factory=uuid7)
    adjunto_id: UUID = field(default_factory=uuid7)
    trabajo_id: UUID = field(default_factory=uuid7)
    perfil: str = ""

    # Datos del contribuyente y de la operacion
    ruc_contribuyente: Ruc | None = None
    nombre_contribuyente: str = ""
    ruc_inquilino: Ruc | None = None
    nombre_inquilino: str = ""
    tipo_doc_inquilino: str = ""
    tipo_de_bien: str = ""
    periodo: PeriodoTributario | None = None
    fecha_de_pago: FechaDePago | None = None
    numero_de_operacion: NumeroDeOperacion | None = None
    # Los cuatro importes del recibo, antes colapsados en uno solo: el
    # alquiler pactado, el tributo calculado, lo efectivamente pagado y
    # los intereses por mora. Distinguirlos es lo que pide un reporte
    # tributario real, donde no es lo mismo lo que se debe que lo que se
    # pago.
    monto_alquiler: Importe | None = None
    tributo_resultante: Importe | None = None
    importe_pagado: Importe | None = None
    intereses_moratorios: Importe | None = None

    # Calidad
    campos_crudos: dict[str, str] = field(default_factory=dict)
    confianza_por_campo: dict[str, float] = field(default_factory=dict)
    completitud: Completitud = Completitud.VACIO
    estado_de_revision: EstadoDeRevision = EstadoDeRevision.NO_REQUERIDA
    revisado_por: UUID | None = None
    revisado_en: datetime | None = None

    # Trazabilidad
    estrategia_usada: Estrategia | None = None
    duracion_ms: int = 0
    creado_en: datetime = field(default_factory=ahora_utc)

    # ── Reglas de negocio ────────────────────────────────────────────

    @property
    def requiere_revision(self) -> bool:
        return self.estado_de_revision is EstadoDeRevision.PENDIENTE

    @property
    def esta_aprobado(self) -> bool:
        return self.estado_de_revision in (
            EstadoDeRevision.NO_REQUERIDA,
            EstadoDeRevision.APROBADO,
        )

    def campos_dudosos(self) -> list[str]:
        """Campos por debajo del umbral. Es lo que la UI resalta al revisar."""
        return sorted(
            nombre
            for nombre, confianza in self.confianza_por_campo.items()
            if confianza < UMBRAL_DE_CONFIANZA
        )

    def aprobar(self, revisor_id: UUID) -> None:
        """
        Da el registro por bueno.

        Se permite aprobar tambien un registro rechazado: una segunda
        mirada puede corregir el rechazo, y bloquearlo obligaria a
        borrar y reprocesar el adjunto para arreglar un error humano.
        """
        if self.estado_de_revision is EstadoDeRevision.NO_REQUERIDA:
            return
        self.estado_de_revision = EstadoDeRevision.APROBADO
        self.revisado_por = revisor_id
        self.revisado_en = ahora_utc()

    def rechazar(self, revisor_id: UUID) -> None:
        """Marca el registro como no utilizable: documento ilegible o ajeno."""
        self.estado_de_revision = EstadoDeRevision.RECHAZADO
        self.revisado_por = revisor_id
        self.revisado_en = ahora_utc()

    def aplicar_correccion(self, revisor_id: UUID, correcciones: dict[str, Any]) -> None:
        """
        Aplica los valores corregidos por una persona.

        Un campo corregido a mano pasa a confianza 1.0: lo valido un
        humano mirando el documento, que es la referencia contra la que
        se mide cualquier motor automatico.
        """
        for nombre, valor in correcciones.items():
            if valor is None:
                continue
            setattr(self, nombre, valor)
            self.confianza_por_campo[nombre] = 1.0
            self.campos_crudos[nombre] = str(valor)

        self.revisado_por = revisor_id
        self.revisado_en = ahora_utc()
