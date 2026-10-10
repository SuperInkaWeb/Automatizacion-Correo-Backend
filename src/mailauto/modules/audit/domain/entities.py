"""
Entidades del contexto de auditoria.

Proposito
    Dejar constancia inmutable de toda accion privilegiada, que es lo que
    permite responder "quien hizo esto y cuando" despues de un incidente
    (OWASP A09).

Dependencias
    Solo `shared`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from mailauto.shared.types import ahora_utc, uuid7


class AccionAuditada(StrEnum):
    """
    Catalogo cerrado de acciones registrables.

    Una enumeracion y no texto libre: asi las consultas de auditoria son
    fiables y nadie inventa un nombre de accion que luego no aparece en
    los informes.
    """

    BUZON_VINCULADO = "mailbox.linked"
    BUZON_DESVINCULADO = "mailbox.unlinked"
    ESCANEO_INICIADO = "scan.started"
    ESCANEO_CANCELADO = "scan.cancelled"
    REPORTE_EXPORTADO = "report.exported"
    REGISTRO_CORREGIDO = "record.corrected"
    REGISTRO_APROBADO = "record.approved"
    REGISTRO_RECHAZADO = "record.rejected"
    REGISTRO_ELIMINADO = "record.deleted"
    ADJUNTO_DESCARGADO = "attachment.downloaded"
    DATOS_PURGADOS = "admin.purged"
    ROL_MODIFICADO = "admin.role_changed"


@dataclass(slots=True)
class EntradaDeAuditoria:
    """
    Una accion registrada. Append-only: no se actualiza ni se borra.

    `metadatos` guarda el contexto imprescindible para entender el hecho,
    nunca el dato sensible en si: se registra que se exporto un reporte,
    no su contenido.
    """

    id: int | None = None
    tenant_id: UUID = field(default_factory=uuid7)
    actor_id: UUID | None = None
    actor_ip: str | None = None
    accion: AccionAuditada = AccionAuditada.ESCANEO_INICIADO
    tipo_de_recurso: str = ""
    recurso_id: UUID | None = None
    metadatos: dict[str, Any] = field(default_factory=dict)
    ocurrido_en: datetime = field(default_factory=ahora_utc)
