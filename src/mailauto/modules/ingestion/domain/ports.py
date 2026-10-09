"""
Puertos del contexto de ingesta.

Proposito
    Definir los contratos de proveedor de correo, almacenamiento de
    objetos, cola de trabajos, canal de progreso y persistencia.

Dependencias
    Solo entidades del propio dominio y `shared`.

Decision de diseño
    `ProveedorDeCorreo.descargar_adjunto` recibe un `limite_de_bytes` y
    devuelve un iterador de trozos. Obliga a que toda implementacion
    trabaje en streaming con tope: no hay forma de escribir un adaptador
    que cargue un adjunto de 2 GB en memoria y tumbe el worker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from mailauto.modules.ingestion.domain.entities import (
    Adjunto,
    ErrorDeProcesamiento,
    MensajeDeCorreo,
    TrabajoDeEscaneo,
)
from mailauto.shared.pagination import Pagina, SolicitudDePagina
from mailauto.shared.security.context import TenantContext

# ── Proveedor de correo ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResumenDeMensaje:
    """Cabecera de un mensaje, sin descargar el cuerpo."""

    id_del_proveedor: str
    remitente: str
    asunto: str
    recibido_en: datetime | None


@dataclass(frozen=True, slots=True)
class ReferenciaDeAdjunto:
    """Adjunto anunciado por el proveedor, aun sin descargar."""

    id_del_adjunto: str
    nombre: str
    tipo_mime_declarado: str
    tamano_declarado: int


class ProveedorDeCorreo(ABC):
    """Acceso de solo lectura al buzon de un usuario."""

    @abstractmethod
    def listar_mensajes(
        self,
        access_token: str,
        *,
        desde: date | None,
        hasta: date | None,
        limite: int,
        carpeta: str,
    ) -> AsyncIterator[ResumenDeMensaje]:
        """
        Itera mensajes con adjuntos en el rango dado.

        Es un generador y no una lista: el proveedor pagina, y materializar
        diez mil cabeceras antes de empezar a trabajar retrasa el primer
        resultado sin ninguna ventaja.
        """

    @abstractmethod
    async def listar_adjuntos(
        self, access_token: str, id_del_mensaje: str
    ) -> list[ReferenciaDeAdjunto]:
        """Adjuntos de un mensaje, sin descargar su contenido."""

    @abstractmethod
    def descargar_adjunto(
        self,
        access_token: str,
        *,
        id_del_mensaje: str,
        id_del_adjunto: str,
        limite_de_bytes: int,
    ) -> AsyncIterator[bytes]:
        """
        Descarga el adjunto en trozos, abortando al superar el limite.

        El tope se aplica durante la transferencia, no despues: comprobar
        el tamaño al final implica haberlo cargado entero, que es
        exactamente lo que se quiere evitar.
        """


# ── Credenciales ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CredencialDeBuzon:
    """Lo minimo que la ingesta necesita saber de una conexion."""

    proveedor: str
    access_token: str
    correo_de_la_cuenta: str


class ProveedorDeCredenciales(ABC):
    """
    Entrega un access token vigente para una conexion.

    Existe como puerto, y no como import del modulo de buzones, porque los
    contextos acotados no se importan entre si (contrato
    `modulos-independientes` de importlinter). El composition root conecta
    este puerto con el caso de uso `ObtenerTokenVigente` del otro modulo.
    Asi la ingesta se puede testear con un token falso, sin OAuth.
    """

    @abstractmethod
    async def obtener(self, *, tenant_id: UUID, conexion_id: UUID) -> CredencialDeBuzon: ...


# ── Almacenamiento de objetos ────────────────────────────────────────


class AlmacenDeObjetos(ABC):
    @abstractmethod
    async def guardar(
        self, *, clave: str, contenido: bytes, tipo_mime: str, metadatos: dict[str, str]
    ) -> None: ...

    @abstractmethod
    async def descargar(self, clave: str) -> bytes:
        """
        Devuelve el contenido del objeto.

        Lo usa el worker de extraccion, que necesita los bytes en
        memoria para parsearlos. Para entregar un fichero al navegador
        se usa `url_de_descarga`: hacer de proxy mantendria la conexion
        abierta durante toda la transferencia.
        """

    @abstractmethod
    async def url_de_descarga(self, clave: str, *, ttl_segundos: int) -> str:
        """URL prefirmada de vida corta. Nunca se expone el bucket directamente."""

    @abstractmethod
    async def eliminar(self, clave: str) -> None: ...

    @abstractmethod
    async def esta_disponible(self) -> bool:
        """Sonda para /health/ready."""


# ── Validacion de adjuntos ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResultadoDeValidacion:
    aceptado: bool
    tipo_mime_real: str
    sha256: str
    motivo_de_rechazo: str | None = None


class ValidadorDeAdjuntos(ABC):
    @abstractmethod
    def validar(self, contenido: bytes, *, nombre: str) -> ResultadoDeValidacion:
        """Verifica tipo real, tamaño y coherencia con el nombre declarado."""


# ── Cola de trabajos ─────────────────────────────────────────────────


class ColaDeTrabajos(ABC):
    @abstractmethod
    async def encolar_escaneo(self, *, tenant_id: UUID, trabajo_id: UUID) -> str:
        """Encola el trabajo y devuelve el identificador del job."""

    @abstractmethod
    async def encolar_extraccion(
        self,
        *,
        tenant_id: UUID,
        trabajo_id: UUID,
        adjunto_id: UUID,
        clave_de_almacenamiento: str,
        tipo_mime: str,
        nombre: str,
    ) -> str:
        """
        Encola la extraccion de un adjunto ya almacenado.

        Un job por adjunto y no uno por escaneo: es lo que permite que
        varios workers procesen el mismo buzon en paralelo y que un
        documento problematico se reintente solo, sin arrastrar a los
        demas del lote.
        """

    @abstractmethod
    async def solicitar_cancelacion(self, trabajo_id: UUID) -> None:
        """
        Marca el trabajo para que el worker se detenga.

        Es cooperativo: el worker comprueba la señal entre mensajes. Matar
        el proceso dejaria la transaccion a medias y el estado inconsistente.
        """

    @abstractmethod
    async def cancelacion_solicitada(self, trabajo_id: UUID) -> bool: ...

    @abstractmethod
    async def esta_disponible(self) -> bool: ...


# ── Canal de progreso ────────────────────────────────────────────────


class CanalDeProgreso(ABC):
    """
    Publicacion y suscripcion del progreso en vivo.

    Desacoplar esto de la API permite que cualquier replica atienda el SSE
    de un trabajo que corre en otro worker: el fan-out lo hace Redis.
    """

    @abstractmethod
    async def publicar(self, trabajo_id: UUID, evento: dict[str, object]) -> None: ...

    @abstractmethod
    def suscribirse(self, trabajo_id: UUID) -> AsyncIterator[dict[str, object]]:
        """Itera los eventos de progreso de un trabajo hasta que termina."""


# ── Persistencia ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrigenDeAdjunto:
    """
    Procedencia de un registro: de que correo y adjunto salio.

    Lo consume la UI de revision y de registros para que una persona
    sepa que documento esta mirando. No vive en el registro extraido
    (otro contexto) sino que se compone al leer, uniendo el adjunto con
    su mensaje.
    """

    nombre_adjunto: str
    remitente: str
    asunto: str
    recibido_en: datetime | None


class RepositorioDeIngesta(ABC):
    # Trabajos
    @abstractmethod
    async def crear_trabajo(
        self, ctx: TenantContext, trabajo: TrabajoDeEscaneo
    ) -> TrabajoDeEscaneo: ...

    @abstractmethod
    async def actualizar_trabajo(self, tenant_id: UUID, trabajo: TrabajoDeEscaneo) -> None: ...

    @abstractmethod
    async def obtener_trabajo(
        self, ctx: TenantContext, trabajo_id: UUID
    ) -> TrabajoDeEscaneo | None: ...

    @abstractmethod
    async def obtener_trabajo_por_tenant(
        self, tenant_id: UUID, trabajo_id: UUID
    ) -> TrabajoDeEscaneo | None: ...

    @abstractmethod
    async def listar_trabajos(
        self, ctx: TenantContext, pagina: SolicitudDePagina
    ) -> Pagina[TrabajoDeEscaneo]: ...

    @abstractmethod
    async def buscar_por_idempotencia(
        self, ctx: TenantContext, clave: str
    ) -> TrabajoDeEscaneo | None:
        """Devuelve el trabajo ya creado con esa clave, si existe."""

    @abstractmethod
    async def contar_trabajos_activos(self, tenant_id: UUID) -> int:
        """Para aplicar el limite de escaneos concurrentes por tenant."""

    # Mensajes y adjuntos
    @abstractmethod
    async def ids_de_mensajes_procesados(
        self, tenant_id: UUID, proveedor: str, ids_candidatos: list[str]
    ) -> set[str]:
        """
        De los ids candidatos, cuales ya se procesaron.

        Se consulta por lotes y no uno a uno: con quinientos mensajes, una
        consulta por mensaje son quinientos viajes a la base de datos
        (problema N+1 clasico).
        """

    @abstractmethod
    async def guardar_mensaje(
        self, tenant_id: UUID, mensaje: MensajeDeCorreo
    ) -> MensajeDeCorreo: ...

    @abstractmethod
    async def existe_adjunto_con_hash(self, tenant_id: UUID, sha256: str) -> bool:
        """Deduplicacion por contenido: el mismo PDF reenviado no se duplica."""

    @abstractmethod
    async def guardar_adjunto(self, tenant_id: UUID, adjunto: Adjunto) -> Adjunto: ...

    @abstractmethod
    async def origen_de_adjuntos(
        self, ctx: TenantContext, adjunto_ids: list[UUID]
    ) -> dict[UUID, OrigenDeAdjunto]:
        """
        Procedencia (correo y nombre) de un lote de adjuntos.

        En lote y no uno a uno: la pantalla de registros pide la de una
        pagina entera, y una consulta por adjunto seria el N+1 de siempre.
        """

    # Errores
    @abstractmethod
    async def guardar_errores(
        self, tenant_id: UUID, errores: list[ErrorDeProcesamiento]
    ) -> None: ...

    @abstractmethod
    async def listar_errores(
        self, ctx: TenantContext, pagina: SolicitudDePagina, trabajo_id: UUID | None = None
    ) -> Pagina[ErrorDeProcesamiento]: ...
