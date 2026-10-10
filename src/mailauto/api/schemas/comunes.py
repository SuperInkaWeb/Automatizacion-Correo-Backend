"""
DTOs de entrada y salida de la API.

Proposito
    Mantener separado el contrato publico de las entidades de dominio,
    para poder cambiar el modelo interno sin romper a los clientes y, al
    reves, no verse obligado a exponer todo lo que una entidad contiene.

Dependencias
    Pydantic v2.

Decision de diseño
    La salida se construye con fabricas explicitas (`desde_dominio`) en
    lugar de serializar la entidad. Asi cada campo que llega al cliente es
    una decision consciente: un atributo nuevo en el dominio no se filtra
    solo a la API.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class MetaDePagina(BaseModel):
    """Metadatos de paginacion. Presentes solo en los listados."""

    cursor: str | None = None
    hay_mas: bool = False


class Respuesta(BaseModel, Generic[T]):
    """
    Envoltura uniforme de exito.

    `meta` va tipado y no como diccionario abierto: con `dict[str, Any]`
    el esquema OpenAPI lo expone como un mapa de `unknown` y cada
    cliente tiene que estrechar el cursor a mano en cada listado.
    """

    status: str = "ok"
    data: T
    meta: MetaDePagina | None = None


# ── Identidad ────────────────────────────────────────────────────────


class PerfilSalida(BaseModel):
    user_id: UUID
    tenant_id: UUID
    email: str
    rol: str
    permisos: list[str]


# ── Buzones ──────────────────────────────────────────────────────────


class IniciarVinculacionEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proveedor: Annotated[str, Field(pattern="^(google|microsoft)$")]
    # Se valida contra la allowlist del servidor; el patron solo descarta
    # lo obviamente malformado antes de llegar al caso de uso.
    redirect_uri: Annotated[str, Field(min_length=8, max_length=512)]


class UrlDeAutorizacionSalida(BaseModel):
    url_de_autorizacion: str


class CallbackEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codigo: Annotated[str, Field(min_length=1, max_length=2048)]
    state: Annotated[str, Field(min_length=16, max_length=256)]


class BuzonSalida(BaseModel):
    id: UUID
    proveedor: str
    correo_de_la_cuenta: str
    estado: str
    expira_en: datetime
    verificada_en: datetime | None

    @classmethod
    def desde_dominio(cls, conexion: Any) -> BuzonSalida:  # noqa: ANN401 - entidad de dominio: tiparla acoplaria la API al modulo
        # No se exponen ni los tokens ni los alcances concedidos: lo
        # primero es la credencial y lo segundo revela la configuracion
        # OAuth de la aplicacion.
        return cls(
            id=conexion.id,
            proveedor=conexion.proveedor.value,
            correo_de_la_cuenta=conexion.correo_de_la_cuenta,
            estado=conexion.estado.value,
            expira_en=conexion.expira_en,
            verificada_en=conexion.verificada_en,
        )


# ── Escaneos ─────────────────────────────────────────────────────────


class IniciarEscaneoEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conexion_id: UUID
    desde: date | None = None
    hasta: date | None = None
    limite_de_mensajes: Annotated[int, Field(ge=1, le=50_000)] = 100
    carpeta: Annotated[str, Field(max_length=120, pattern=r"^[\w\-/ ]+$")] = "INBOX"


class ContadoresSalida(BaseModel):
    mensajes_revisados: int = 0
    mensajes_con_adjuntos: int = 0
    adjuntos_descargados: int = 0
    adjuntos_rechazados: int = 0
    adjuntos_duplicados: int = 0
    errores: int = 0


class EscaneoSalida(BaseModel):
    id: UUID
    estado: str
    fase: str
    progreso_porcentaje: int
    contadores: ContadoresSalida
    codigo_de_error: str | None
    mensaje_de_error: str | None
    encolado_en: datetime
    iniciado_en: datetime | None
    finalizado_en: datetime | None

    @classmethod
    def desde_dominio(cls, trabajo: Any) -> EscaneoSalida:  # noqa: ANN401 - entidad de dominio: tiparla acoplaria la API al modulo
        return cls(
            id=trabajo.id,
            estado=trabajo.estado.value,
            fase=trabajo.fase.value,
            progreso_porcentaje=trabajo.progreso_porcentaje,
            contadores=ContadoresSalida(**trabajo.contadores.como_dict()),
            codigo_de_error=trabajo.codigo_de_error,
            mensaje_de_error=trabajo.mensaje_de_error,
            encolado_en=trabajo.encolado_en,
            iniciado_en=trabajo.iniciado_en,
            finalizado_en=trabajo.finalizado_en,
        )


# ── Registros extraidos ──────────────────────────────────────────────


def _moneda_del_registro(registro: Any) -> str | None:  # noqa: ANN401 - entidad de dominio
    """La moneda del primer monto presente; None si el registro no trae ninguno."""
    for monto in (
        registro.importe_pagado,
        registro.monto_alquiler,
        registro.tributo_resultante,
        registro.intereses_moratorios,
    ):
        if monto is not None:
            return str(monto.moneda)
    return None


class RegistroSalida(BaseModel):
    id: UUID
    adjunto_id: UUID
    trabajo_id: UUID
    perfil: str
    ruc_contribuyente: str | None
    nombre_contribuyente: str
    ruc_inquilino: str | None
    nombre_inquilino: str
    tipo_doc_inquilino: str
    tipo_de_bien: str
    periodo: str | None
    fecha_de_pago: str | None
    numero_de_operacion: str | None
    monto_alquiler: str | None
    tributo_resultante: str | None
    importe_pagado: str | None
    intereses_moratorios: str | None
    moneda: str | None
    completitud: str
    estado_de_revision: str
    # Lo que la interfaz resalta al revisar: sin esto, quien corrige
    # tiene que comparar el documento campo por campo.
    campos_dudosos: list[str]
    confianza_por_campo: dict[str, float]
    estrategia_usada: str | None
    creado_en: datetime
    # Procedencia: de que correo y adjunto salio el registro. Se compone
    # al leer (no vive en el registro) y es None si el origen ya no esta
    # o no se consulto. Es lo que permite a la UI decir "de que correo es".
    adjunto_nombre: str | None = None
    correo_remitente: str | None = None
    correo_asunto: str | None = None
    correo_recibido_en: datetime | None = None

    @classmethod
    def desde_dominio(cls, registro: Any, origen: Any = None) -> RegistroSalida:  # noqa: ANN401
        # `campos_crudos` no se expone: contiene el texto tal cual lo
        # leyo el motor, que puede arrastrar fragmentos del documento
        # ajenos a los campos.
        return cls(
            id=registro.id,
            adjunto_id=registro.adjunto_id,
            trabajo_id=registro.trabajo_id,
            perfil=registro.perfil,
            ruc_contribuyente=str(registro.ruc_contribuyente)
            if registro.ruc_contribuyente
            else None,
            nombre_contribuyente=registro.nombre_contribuyente,
            ruc_inquilino=str(registro.ruc_inquilino) if registro.ruc_inquilino else None,
            nombre_inquilino=registro.nombre_inquilino,
            tipo_doc_inquilino=registro.tipo_doc_inquilino,
            tipo_de_bien=registro.tipo_de_bien,
            periodo=str(registro.periodo) if registro.periodo else None,
            fecha_de_pago=str(registro.fecha_de_pago) if registro.fecha_de_pago else None,
            numero_de_operacion=str(registro.numero_de_operacion)
            if registro.numero_de_operacion
            else None,
            monto_alquiler=str(registro.monto_alquiler) if registro.monto_alquiler else None,
            tributo_resultante=str(registro.tributo_resultante)
            if registro.tributo_resultante
            else None,
            importe_pagado=str(registro.importe_pagado) if registro.importe_pagado else None,
            intereses_moratorios=str(registro.intereses_moratorios)
            if registro.intereses_moratorios
            else None,
            moneda=_moneda_del_registro(registro),
            completitud=registro.completitud.value,
            estado_de_revision=registro.estado_de_revision.value,
            campos_dudosos=registro.campos_dudosos(),
            confianza_por_campo=registro.confianza_por_campo,
            estrategia_usada=registro.estrategia_usada.value if registro.estrategia_usada else None,
            creado_en=registro.creado_en,
            adjunto_nombre=origen.nombre_adjunto if origen else None,
            correo_remitente=origen.remitente if origen else None,
            correo_asunto=origen.asunto if origen else None,
            correo_recibido_en=origen.recibido_en if origen else None,
        )


class DocumentoDeAdjuntoSalida(BaseModel):
    """URL prefirmada para ver el adjunto original. Caduca pronto."""

    url: str


class CorreccionEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Diccionario campo -> valor en texto. El caso de uso lo interpreta
    # con los objetos de valor y rechaza lo que no sea corregible.
    correcciones: Annotated[dict[str, str], Field(min_length=1, max_length=12)]


# ── Reportes ─────────────────────────────────────────────────────────


class SolicitarExportacionEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formato: Annotated[str, Field(pattern="^(xlsx|csv)$")] = "xlsx"
    ruc: Annotated[str | None, Field(default=None, pattern=r"^\d{11}$")] = None
    ruc_inquilino: Annotated[str | None, Field(default=None, pattern=r"^\d{11}$")] = None
    periodo: Annotated[str | None, Field(default=None, pattern=r"^\d{6}$")] = None
    periodo_desde: Annotated[str | None, Field(default=None, pattern=r"^\d{6}$")] = None
    periodo_hasta: Annotated[str | None, Field(default=None, pattern=r"^\d{6}$")] = None
    fecha_desde: date | None = None
    fecha_hasta: date | None = None
    solo_aprobados: bool = False
    # "Exportar solo estos": los registros que el usuario marco. Si va
    # vacio, se exporta todo lo que cumplan los demas filtros.
    ids: list[UUID] = Field(default_factory=list)


class ExportacionSalida(BaseModel):
    id: UUID
    formato: str
    estado: str
    total_filas: int
    mensaje_de_error: str | None
    # Presente solo cuando el reporte esta listo. Vida corta: lleva los
    # datos tributarios del cliente.
    url_de_descarga: str | None
    creado_en: datetime
    finalizado_en: datetime | None

    @classmethod
    def desde_dominio(cls, exportacion: Any, url: str | None) -> ExportacionSalida:  # noqa: ANN401
        return cls(
            id=exportacion.id,
            formato=exportacion.formato.value,
            estado=exportacion.estado.value,
            total_filas=exportacion.total_filas,
            mensaje_de_error=exportacion.mensaje_de_error,
            url_de_descarga=url,
            creado_en=exportacion.creado_en,
            finalizado_en=exportacion.finalizado_en,
        )


# ── Errores y auditoria ──────────────────────────────────────────────


class ErrorDeProcesamientoSalida(BaseModel):
    id: UUID
    trabajo_id: UUID
    etapa: str
    codigo: str
    mensaje: str
    reintentable: bool
    ocurrido_en: datetime

    @classmethod
    def desde_dominio(cls, error: Any) -> ErrorDeProcesamientoSalida:  # noqa: ANN401 - entidad de dominio: tiparla acoplaria la API al modulo
        # `contexto` queda fuera: puede contener nombres de archivo y
        # otros datos del correo del usuario.
        return cls(
            id=error.id,
            trabajo_id=error.trabajo_id,
            etapa=error.etapa.value,
            codigo=error.codigo,
            mensaje=error.mensaje,
            reintentable=error.reintentable,
            ocurrido_en=error.ocurrido_en,
        )


class EntradaDeAuditoriaSalida(BaseModel):
    id: int | None
    accion: str
    tipo_de_recurso: str
    recurso_id: UUID | None
    actor_id: UUID | None
    ocurrido_en: datetime

    @classmethod
    def desde_dominio(cls, entrada: Any) -> EntradaDeAuditoriaSalida:  # noqa: ANN401 - entidad de dominio: tiparla acoplaria la API al modulo
        return cls(
            id=entrada.id,
            accion=entrada.accion.value,
            tipo_de_recurso=entrada.tipo_de_recurso,
            recurso_id=entrada.recurso_id,
            actor_id=entrada.actor_id,
            ocurrido_en=entrada.ocurrido_en,
        )


# ── Salud ────────────────────────────────────────────────────────────


class SaludSalida(BaseModel):
    estado: str
    componentes: dict[str, bool] | None = None
