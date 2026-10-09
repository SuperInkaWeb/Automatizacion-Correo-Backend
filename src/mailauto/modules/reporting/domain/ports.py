"""
Dominio y puertos del contexto de reportes.

Proposito
    Convertir registros ya extraidos en un fichero descargable, sin
    saber nada de como se extrajeron.

Dependencias
    Solo `shared`.

Decision de diseño
    `FilaDeReporte` es un DTO propio y no la entidad de extraccion.
    Los contextos acotados no se importan entre si, y ademas el reporte
    tiene su propia forma: columnas en el orden que espera un contador,
    valores ya formateados como texto y sin los metadatos de confianza
    que solo sirven dentro del pipeline. El composition root adapta uno
    al otro, igual que con las credenciales de los buzones.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from mailauto.shared.types import ahora_utc, uuid7


class FormatoDeReporte(StrEnum):
    EXCEL = "xlsx"
    CSV = "csv"

    @property
    def tipo_mime(self) -> str:
        return {
            FormatoDeReporte.EXCEL: (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            FormatoDeReporte.CSV: "text/csv",
        }[self]


class EstadoDeExportacion(StrEnum):
    EN_COLA = "queued"
    GENERANDO = "running"
    LISTA = "ready"
    FALLIDA = "failed"

    @property
    def es_terminal(self) -> bool:
        return self in (EstadoDeExportacion.LISTA, EstadoDeExportacion.FALLIDA)


@dataclass(frozen=True, slots=True)
class FilaDeReporte:
    """
    Una fila del reporte, con todo ya convertido a texto.

    El formateo ocurre al construir la fila y no al escribir el
    fichero: asi el Excel y el CSV muestran exactamente lo mismo, en
    lugar de cada generador inventando su propio formato de fecha.
    """

    ruc_contribuyente: str = ""
    nombre_contribuyente: str = ""
    tipo_doc_inquilino: str = ""
    ruc_inquilino: str = ""
    nombre_inquilino: str = ""
    tipo_de_bien: str = ""
    periodo: str = ""
    monto_alquiler: str = ""
    tributo_resultante: str = ""
    importe_pagado: str = ""
    intereses_moratorios: str = ""
    moneda: str = ""
    fecha_de_pago: str = ""
    numero_de_operacion: str = ""
    estado: str = ""
    revision: str = ""
    archivo_origen: str = ""


# Cabeceras del reporte, en el orden en que se escriben. Es la unica
# definicion del orden de columnas: generadores y filas la comparten.
COLUMNAS: tuple[tuple[str, str], ...] = (
    ("ruc_contribuyente", "RUC Arrendador"),
    ("nombre_contribuyente", "Nombre / Razon Social"),
    ("tipo_doc_inquilino", "Tipo Doc. Inquilino"),
    ("ruc_inquilino", "RUC Arrendatario"),
    ("nombre_inquilino", "Inquilino"),
    ("tipo_de_bien", "Tipo de Bien"),
    ("periodo", "Periodo"),
    ("monto_alquiler", "Monto Alquiler"),
    ("tributo_resultante", "Tributo Resultante"),
    ("importe_pagado", "Importe Pagado"),
    ("intereses_moratorios", "Intereses Moratorios"),
    ("moneda", "Moneda"),
    ("fecha_de_pago", "Fecha de Pago"),
    ("numero_de_operacion", "N. Operacion"),
    ("estado", "Estado de Extraccion"),
    ("revision", "Revision"),
    ("archivo_origen", "Archivo"),
)


@dataclass(slots=True)
class Exportacion:
    """Una solicitud de reporte y su ciclo de vida."""

    id: UUID = field(default_factory=uuid7)
    tenant_id: UUID = field(default_factory=uuid7)
    solicitado_por: UUID | None = None
    formato: FormatoDeReporte = FormatoDeReporte.EXCEL
    estado: EstadoDeExportacion = EstadoDeExportacion.EN_COLA
    filtros: dict[str, str] = field(default_factory=dict)
    total_filas: int = 0
    clave_de_almacenamiento: str | None = None
    mensaje_de_error: str | None = None
    creado_en: datetime = field(default_factory=ahora_utc)
    finalizado_en: datetime | None = None

    def marcar_generando(self) -> None:
        self.estado = EstadoDeExportacion.GENERANDO

    def completar(self, *, clave: str, total_filas: int) -> None:
        self.estado = EstadoDeExportacion.LISTA
        self.clave_de_almacenamiento = clave
        self.total_filas = total_filas
        self.finalizado_en = ahora_utc()

    def fallar(self, mensaje: str) -> None:
        self.estado = EstadoDeExportacion.FALLIDA
        self.mensaje_de_error = mensaje
        self.finalizado_en = ahora_utc()


# ── Puertos ──────────────────────────────────────────────────────────


class FuenteDeFilas(ABC):
    """
    De donde salen las filas del reporte.

    El composition root la conecta con el repositorio de extraccion.
    Desde aqui, el origen de los datos es intercambiable y el modulo
    se testea con una lista en memoria.
    """

    @abstractmethod
    async def obtener(
        self, tenant_id: UUID, filtros: dict[str, str], limite: int
    ) -> list[FilaDeReporte]: ...


class GeneradorDeReporte(ABC):
    """Convierte filas en los bytes de un fichero."""

    @property
    @abstractmethod
    def formato(self) -> FormatoDeReporte: ...

    @abstractmethod
    def generar(self, filas: list[FilaDeReporte], titulo: str) -> bytes: ...


class DestinoDeReportes(ABC):
    """Donde se deja el fichero generado y como se entrega."""

    @abstractmethod
    async def guardar(self, clave: str, contenido: bytes, tipo_mime: str) -> None: ...

    @abstractmethod
    async def url_de_descarga(self, clave: str, *, ttl_segundos: int) -> str: ...


class RepositorioDeExportaciones(ABC):
    @abstractmethod
    async def crear(self, exportacion: Exportacion) -> Exportacion: ...

    @abstractmethod
    async def actualizar(self, exportacion: Exportacion) -> None: ...

    @abstractmethod
    async def obtener(self, tenant_id: UUID, exportacion_id: UUID) -> Exportacion | None: ...
