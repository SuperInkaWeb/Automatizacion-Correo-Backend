"""
Configuracion centralizada de la aplicacion.

Proposito
    Cargar y validar toda la configuracion en un unico punto, de modo que
    un despliegue mal configurado falle al arrancar y no horas despues,
    en silencio, con una puerta abierta.

Flujo
    Variables de entorno (o .env en desarrollo) -> Settings -> validadores
    de produccion -> singleton cacheado consumido por el composition root.

Dependencias
    pydantic-settings. No importa nada del proyecto: es la base sobre la
    que se construye todo lo demas.

Decision de diseño
    `_prohibir_configuracion_insegura_en_produccion` levanta ValueError en
    lugar de registrar una advertencia. Una advertencia en un log se ignora;
    un contenedor que no arranca, no. El modo seguro tiene que ser el unico
    modo posible cuando ENVIRONMENT=production.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LONGITUD_CLAVE_AES_256 = 32


class Entorno(StrEnum):
    """Entorno de ejecucion. Determina que validaciones de seguridad aplican."""

    DESARROLLO = "development"
    PRUEBAS = "testing"
    STAGING = "staging"
    PRODUCCION = "production"

    @property
    def es_productivo(self) -> bool:
        """Staging comparte las reglas de produccion: sirve datos reales."""
        return self in (Entorno.STAGING, Entorno.PRODUCCION)


class Settings(BaseSettings):
    """Configuracion completa, validada al construirse."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",  # una variable desconocida es un error de despliegue
    )

    # ── Aplicacion ───────────────────────────────────────────────────
    environment: Entorno = Entorno.DESARROLLO
    app_name: str = "automatizacion-correos"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # ── Base de datos ────────────────────────────────────────────────
    database_url: PostgresDsn
    db_pool_size: Annotated[int, Field(ge=1, le=100)] = 10
    db_max_overflow: Annotated[int, Field(ge=0, le=100)] = 5
    db_command_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30
    db_echo: bool = False

    # ── Redis (cola de jobs, pub/sub de progreso, rate limiting) ─────
    redis_url: RedisDsn

    # ── Identidad (OIDC) ─────────────────────────────────────────────
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_cache_seconds: Annotated[int, Field(ge=60, le=86_400)] = 3600
    oidc_roles_claim: str = "https://automatizacion-correos/roles"

    # ── Criptografia ─────────────────────────────────────────────────
    # Clave maestra (KEK) en base64. En produccion debe venir de un KMS o
    # gestor de secretos, nunca de un fichero en disco.
    master_key_b64: str
    # Solo `local` esta implementado: la clave maestra sale de
    # MASTER_KEY_B64. `aws` y `vault` son trabajo futuro y el arranque los
    # rechaza en vez de fingir que cifra con un KMS inexistente.
    kms_provider: str = "local"  # local (implementado) | aws | vault (pendientes)
    # Reconocimiento explicito para usar la clave local en produccion. Sin
    # un KMS real, la clave vive en un secreto del entorno (Railway,
    # Cloudflare, etc.): aceptable para empezar, pero es una decision
    # consciente, no un descuido. Por eso hay que activarlo a mano.
    kms_local_en_produccion_aceptado: bool = False

    # ── OAuth de proveedores de correo ───────────────────────────────
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_tenant_id: str = "common"
    # Allowlist estricta: el redirect_uri recibido debe coincidir
    # exactamente con uno de estos. Mitiga el robo de codigo OAuth.
    oauth_redirect_uris: list[str] = Field(default_factory=list)
    oauth_state_ttl_seconds: Annotated[int, Field(ge=60, le=1800)] = 600

    # ── Almacenamiento de objetos ────────────────────────────────────
    storage_endpoint_url: str | None = None  # None = AWS S3 real
    storage_bucket: str = "adjuntos"
    storage_region: str = "us-east-1"
    storage_access_key: str
    storage_secret_key: str
    storage_presign_ttl_seconds: Annotated[int, Field(ge=30, le=3600)] = 300

    # ── Limites de ingesta ───────────────────────────────────────────
    max_attachment_bytes: Annotated[int, Field(ge=1024, le=104_857_600)] = 26_214_400
    max_attachments_per_message: Annotated[int, Field(ge=1, le=100)] = 20
    max_messages_per_scan: Annotated[int, Field(ge=1, le=50_000)] = 5_000
    max_scan_date_range_days: Annotated[int, Field(ge=1, le=1830)] = 366
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/webp",
        ]
    )

    # ── Extraccion ───────────────────────────────────────────────────
    # Sin clave, la estrategia de vision no se registra y el pipeline
    # funciona igual con los tres motores gratuitos.
    anthropic_api_key: str | None = None
    vision_ai_habilitada: bool = False
    vision_modelo: str = "claude-opus-5-5"
    # Esfuerzo bajo: transcribir campos de un formulario conocido es
    # una tarea de clasificacion, no de razonamiento.
    vision_esfuerzo: str = "low"
    # Tope de llamadas de pago por escaneo. Acota el gasto de un
    # trabajo con cientos de adjuntos ilegibles.
    vision_maximo_llamadas_por_trabajo: Annotated[int, Field(ge=0, le=10_000)] = 50

    # ── Reportes ─────────────────────────────────────────────────────
    reporte_maximo_filas: Annotated[int, Field(ge=1, le=200_000)] = 50_000

    # ── Cuotas y rate limiting ───────────────────────────────────────
    rate_limit_default_per_minute: Annotated[int, Field(ge=1)] = 120
    rate_limit_scan_per_hour: Annotated[int, Field(ge=1)] = 20
    rate_limit_oauth_per_hour: Annotated[int, Field(ge=1)] = 10
    tenant_max_concurrent_scans: Annotated[int, Field(ge=1, le=50)] = 3

    # ── Workers ──────────────────────────────────────────────────────
    worker_max_jobs: Annotated[int, Field(ge=1, le=100)] = 8
    worker_job_timeout_seconds: Annotated[int, Field(ge=30, le=7200)] = 1800
    worker_max_tries: Annotated[int, Field(ge=1, le=10)] = 3

    # ── CORS y frontend ──────────────────────────────────────────────
    cors_origins: list[str] = Field(default_factory=list)

    # ── Privacidad ───────────────────────────────────────────────────
    # El texto OCR crudo es dato fiscal. Apagado por defecto (ver §16 de
    # ARQUITECTURA.md); si se activa, se cifra y se purga por TTL.
    store_raw_ocr_text: bool = False
    retention_days_attachments: Annotated[int, Field(ge=1, le=3650)] = 365

    # ── Observabilidad ───────────────────────────────────────────────
    # Si se define, `/metrics` exige `Authorization: Bearer <valor>`. En
    # produccion es obligatorio: el endpoint revela rutas internas, tasas
    # de error y volumen de uso, y sirve de reconocimiento gratuito.
    metrics_token: str | None = None
    otel_exporter_endpoint: str | None = None
    sentry_dsn: str | None = None
    docs_enabled: bool = True

    # ── Validadores de campo ─────────────────────────────────────────

    @field_validator("master_key_b64")
    @classmethod
    def _validar_longitud_clave_maestra(cls, valor: str) -> str:
        """
        AES-256 exige exactamente 32 bytes.

        Una clave mas corta no da un cifrado mas debil: da una falsa
        sensacion de seguridad, porque la aplicacion arrancaria y nadie
        notaria nada hasta una auditoria.
        """
        try:
            crudo = base64.b64decode(valor, validate=True)
        except Exception as exc:
            raise ValueError("MASTER_KEY_B64 no es base64 valido") from exc
        if len(crudo) != _LONGITUD_CLAVE_AES_256:
            raise ValueError(
                f"MASTER_KEY_B64 debe decodificar a {_LONGITUD_CLAVE_AES_256} bytes "
                f"(AES-256); se recibieron {len(crudo)}"
            )
        return valor

    @field_validator("oidc_issuer")
    @classmethod
    def _normalizar_issuer(cls, valor: str) -> str:
        """El `iss` del token siempre termina en barra; normalizar evita falsos rechazos."""
        if not valor.startswith("https://"):
            raise ValueError("OIDC_ISSUER debe usar https")
        return valor if valor.endswith("/") else f"{valor}/"

    @field_validator("oauth_redirect_uris")
    @classmethod
    def _validar_redirect_uris(cls, valores: list[str]) -> list[str]:
        """Sin esta allowlist, un `redirect_uri` manipulado entrega el codigo OAuth a un tercero."""
        for uri in valores:
            if not uri.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValueError(f"redirect_uri inseguro: {uri}")
        return valores

    # ── Validadores de modelo ────────────────────────────────────────

    @model_validator(mode="after")
    def _prohibir_configuracion_insegura_en_produccion(self) -> Self:
        """
        Reglas que solo aplican en staging y produccion.

        Preferimos que el contenedor no arranque a que arranque inseguro:
        un fallo de despliegue es visible, una brecha silenciosa no.
        """
        if not self.environment.es_productivo:
            return self

        problemas: list[str] = []

        if "*" in self.cors_origins:
            problemas.append("CORS_ORIGINS no puede contener '*'")
        if not self.cors_origins:
            problemas.append("CORS_ORIGINS no puede estar vacio")
        if any(o.startswith("http://") for o in self.cors_origins):
            problemas.append("CORS_ORIGINS debe usar https")
        if self.docs_enabled:
            problemas.append("DOCS_ENABLED debe ser false: /docs expone el mapa de la API")
        if self.db_echo:
            problemas.append("DB_ECHO debe ser false: vuelca SQL con datos al log")
        if self.kms_provider in ("aws", "vault"):
            problemas.append(
                f"KMS_PROVIDER={self.kms_provider!r} todavia no esta implementado: el "
                "codigo seguiria usando la clave local. Usa 'local' con "
                "KMS_LOCAL_EN_PRODUCCION_ACEPTADO=true, o implementa el adaptador."
            )
        elif self.kms_provider == "local" and not self.kms_local_en_produccion_aceptado:
            problemas.append(
                "KMS_PROVIDER='local' en produccion exige aceptar el compromiso: la "
                "clave maestra vivira en un secreto del entorno, no en un KMS gestionado. "
                "Si lo asumes, pon KMS_LOCAL_EN_PRODUCCION_ACEPTADO=true."
            )
        if not self.oauth_redirect_uris:
            problemas.append("OAUTH_REDIRECT_URIS no puede estar vacio")
        if any("localhost" in u or "127.0.0.1" in u for u in self.oauth_redirect_uris):
            problemas.append("OAUTH_REDIRECT_URIS no puede apuntar a localhost")
        if self.store_raw_ocr_text:
            problemas.append(
                "STORE_RAW_OCR_TEXT debe ser false por defecto: persiste dato fiscal en claro"
            )
        if self.storage_endpoint_url and "localhost" in self.storage_endpoint_url:
            problemas.append("STORAGE_ENDPOINT_URL apunta a localhost")
        if not self.metrics_token:
            problemas.append(
                "METRICS_TOKEN es obligatorio: /metrics revela rutas internas, "
                "tasas de error y volumen de uso"
            )

        if problemas:
            detalle = "\n  - ".join(problemas)
            raise ValueError(
                f"Configuracion insegura para environment={self.environment}:\n  - {detalle}"
            )
        return self

    @model_validator(mode="after")
    def _exigir_clave_si_la_vision_esta_habilitada(self) -> Self:
        """
        Habilitar la vision sin credencial dejaria el pipeline
        degradado en silencio: los documentos ilegibles irian todos a
        revision humana sin que nadie entendiera por que.
        """
        if self.vision_ai_habilitada and not self.anthropic_api_key:
            raise ValueError("VISION_AI_HABILITADA requiere ANTHROPIC_API_KEY")
        return self

    @model_validator(mode="after")
    def _exigir_al_menos_un_proveedor_de_correo(self) -> Self:
        """Sin ningun proveedor configurado la aplicacion no puede cumplir su funcion."""
        google_ok = bool(self.google_client_id and self.google_client_secret)
        microsoft_ok = bool(self.microsoft_client_id and self.microsoft_client_secret)
        if not (google_ok or microsoft_ok):
            raise ValueError(
                "Debe configurarse al menos un proveedor de correo "
                "(GOOGLE_CLIENT_ID/SECRET o MICROSOFT_CLIENT_ID/SECRET)"
            )
        return self

    # ── Propiedades derivadas ────────────────────────────────────────

    @property
    def master_key(self) -> bytes:
        """Clave maestra decodificada. Nunca se registra en logs ni se serializa."""
        return base64.b64decode(self.master_key_b64)

    @property
    def google_habilitado(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def microsoft_habilitado(self) -> bool:
        return bool(self.microsoft_client_id and self.microsoft_client_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Singleton de configuracion.

    Cacheado para que la validacion ocurra una sola vez y para que los
    tests puedan sustituirlo con `get_settings.cache_clear()`.
    """
    # pydantic-settings puebla los campos desde el entorno.
    return Settings()
