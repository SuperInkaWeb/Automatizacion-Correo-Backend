"""
Puertos del dominio de extraccion.

Proposito
    Abstraer los motores de lectura, el perfil de documento, el acceso
    al adjunto y la persistencia de registros.

Dependencias
    Solo el dominio propio, `shared` y la paginacion compartida.

Decision de diseño
    `PerfilDeExtraccion` separa QUE se busca de COMO se lee. El motor
    (PyMuPDF, Tesseract, vision IA) no sabe nada de RUC ni de periodos;
    el perfil no sabe nada de PDFs ni de pixeles. Cuando SUNAT cambie el
    formato de una constancia —y lo hara— se edita un perfil, no el
    motor, y el cambio no puede romper la lectura de los demas
    documentos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from mailauto.modules.extraction.domain.entities import (
    CampoExtraido,
    Estrategia,
    RegistroTributario,
    ResultadoDeEstrategia,
)
from mailauto.shared.pagination import Pagina, SolicitudDePagina
from mailauto.shared.security.context import TenantContext

# ── Documento de entrada ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DocumentoAExtraer:
    """Un adjunto ya descargado y validado, listo para leer."""

    adjunto_id: UUID
    tenant_id: UUID
    trabajo_id: UUID
    contenido: bytes
    tipo_mime: str
    nombre: str

    @property
    def es_pdf(self) -> bool:
        return self.tipo_mime == "application/pdf"

    @property
    def es_imagen(self) -> bool:
        return self.tipo_mime.startswith("image/")


# ── Perfil de documento ──────────────────────────────────────────────


class PerfilDeExtraccion(ABC):
    """
    Declara que campos buscar en un tipo de documento y como
    reconocerlos en el texto.
    """

    @property
    @abstractmethod
    def nombre(self) -> str: ...

    @abstractmethod
    def reconoce(self, texto: str) -> bool:
        """
        ¿Este texto corresponde al tipo de documento del perfil?

        Permite descartar pronto un adjunto que no es lo que se busca,
        sin gastar OCR ni llamadas de pago en una factura de luz que
        alguien reenvio por error.
        """

    @abstractmethod
    def extraer_campos(self, texto: str) -> dict[str, CampoExtraido]:
        """Localiza los campos del perfil en un texto ya obtenido."""

    @abstractmethod
    def a_registro(
        self, campos: dict[str, CampoExtraido], documento: DocumentoAExtraer
    ) -> RegistroTributario:
        """Convierte los campos crudos en una entidad con objetos de valor."""


# ── Motores de lectura ───────────────────────────────────────────────


class EstrategiaDeExtraccion(ABC):
    """
    Un motor de lectura. Las implementaciones van de la mas barata a la
    mas cara y se prueban en ese orden.
    """

    @property
    @abstractmethod
    def nombre(self) -> Estrategia: ...

    @property
    @abstractmethod
    def costo_relativo(self) -> int:
        """Orden de ejecucion: menor se intenta antes."""

    @abstractmethod
    def admite(self, documento: DocumentoAExtraer) -> bool:
        """¿Puede este motor leer este tipo de documento?"""

    @abstractmethod
    async def leer(
        self, documento: DocumentoAExtraer, perfil: PerfilDeExtraccion
    ) -> ResultadoDeEstrategia:
        """
        Lee el documento y devuelve los campos encontrados.

        No debe lanzar: un motor que falla devuelve un resultado con
        `error`, para que el pipeline pruebe el siguiente en vez de
        abortar. Un PDF corrupto no puede tumbar el escaneo entero.
        """


# ── Acceso al contenido ──────────────────────────────────────────────


class LectorDeAdjuntos(ABC):
    """
    Recupera el contenido de un adjunto desde el almacenamiento.

    Es un puerto propio y no el `AlmacenDeObjetos` de ingesta porque los
    contextos acotados no se importan entre si: el composition root
    conecta ambos.
    """

    @abstractmethod
    async def leer(self, tenant_id: UUID, clave: str) -> bytes: ...


# ── Persistencia ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FiltrosDeRegistro:
    """Filtros del listado de registros. Todos opcionales y combinables."""

    trabajo_id: UUID | None = None
    ruc: str | None = None  # RUC del arrendador (contribuyente)
    periodo: str | None = None  # periodo exacto (AAAAMM)
    solo_pendientes_de_revision: bool = False
    # Filtros del reporte a medida.
    ruc_inquilino: str | None = None
    periodo_desde: str | None = None  # AAAAMM; el orden lexico = cronologico
    periodo_hasta: str | None = None
    fecha_desde: date | None = None  # rango de fecha de pago
    fecha_hasta: date | None = None
    solo_aprobados: bool = False  # excluye pendientes y rechazados
    ids: tuple[UUID, ...] = ()  # "exportar solo estos" (seleccion del usuario)


class RepositorioDeRegistros(ABC):
    @abstractmethod
    async def guardar(
        self, tenant_id: UUID, registro: RegistroTributario
    ) -> RegistroTributario: ...

    @abstractmethod
    async def actualizar(self, ctx: TenantContext, registro: RegistroTributario) -> None: ...

    @abstractmethod
    async def obtener(self, ctx: TenantContext, registro_id: UUID) -> RegistroTributario | None: ...

    @abstractmethod
    async def listar(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, pagina: SolicitudDePagina
    ) -> Pagina[RegistroTributario]: ...

    @abstractmethod
    async def listar_para_reporte(
        self, ctx: TenantContext, filtros: FiltrosDeRegistro, limite: int
    ) -> list[RegistroTributario]:
        """
        Carga registros para exportar.

        Separado de `listar` porque no pagina por cursor: el export
        necesita el conjunto completo. Lleva `limite` obligatorio para
        que nadie pueda pedir la tabla entera por descuido.
        """

    @abstractmethod
    async def existe_para_adjunto(self, tenant_id: UUID, adjunto_id: UUID) -> bool:
        """Evita extraer dos veces el mismo adjunto si el job se reentrega."""

    @abstractmethod
    async def contar_pendientes_de_revision(self, ctx: TenantContext) -> int: ...
