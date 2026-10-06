"""
Tests de la configuracion segura y del filtro de redaccion de logs.

Son dos controles que solo valen si no se pueden desactivar por
descuido, asi que conviene fijarlos con tests explicitos.
"""

from __future__ import annotations

import pytest

from mailauto.bootstrap.settings import Settings
from mailauto.shared.errors import ErrorDeValidacion
from mailauto.shared.observability.logging import procesador_de_redaccion
from mailauto.shared.pagination import Cursor, SolicitudDePagina
from mailauto.shared.types import ahora_utc, uuid7


def _base_de_produccion(**extra: object) -> dict[str, object]:
    return {
        "environment": "production",
        "database_url": "postgresql+asyncpg://u:p@db:5432/mailauto",
        "redis_url": "redis://redis:6379/0",
        "oidc_issuer": "https://tenant.us.auth0.com/",
        "oidc_audience": "https://api.ejemplo.com",
        "master_key_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "kms_provider": "local",
        "kms_local_en_produccion_aceptado": True,
        "google_client_id": "id",
        "google_client_secret": "secreto",
        "oauth_redirect_uris": ["https://app.ejemplo.com/oauth/callback"],
        "cors_origins": ["https://app.ejemplo.com"],
        "docs_enabled": False,
        "storage_access_key": "k",
        "storage_secret_key": "s",
        "metrics_token": "token-de-metricas",
        **extra,
    }


# ── Guardas de produccion ────────────────────────────────────────────


def test_una_configuracion_de_produccion_correcta_es_valida() -> None:
    ajustes = Settings(**_base_de_produccion())  # type: ignore[arg-type]
    assert ajustes.environment.es_productivo


@pytest.mark.parametrize(
    ("campo", "valor", "fragmento"),
    [
        ("cors_origins", ["*"], "CORS_ORIGINS"),
        ("cors_origins", [], "CORS_ORIGINS"),
        ("cors_origins", ["http://app.ejemplo.com"], "https"),
        ("docs_enabled", True, "DOCS_ENABLED"),
        ("db_echo", True, "DB_ECHO"),
        ("kms_local_en_produccion_aceptado", False, "KMS_PROVIDER='local'"),
        ("kms_provider", "aws", "no esta implementado"),
        ("kms_provider", "vault", "no esta implementado"),
        ("oauth_redirect_uris", [], "OAUTH_REDIRECT_URIS"),
        ("store_raw_ocr_text", True, "STORE_RAW_OCR_TEXT"),
        # /metrics revela rutas internas, tasas de error y volumen de uso:
        # abierto es reconocimiento gratuito para quien prepara un ataque.
        ("metrics_token", None, "METRICS_TOKEN"),
    ],
)
def test_produccion_rechaza_configuracion_insegura(
    campo: str, valor: object, fragmento: str
) -> None:
    """
    Cada uno de estos valores seria un fallo de seguridad en produccion.
    La aplicacion no debe arrancar: un contenedor que no levanta es
    visible; una brecha silenciosa no.
    """
    with pytest.raises(ValueError, match=fragmento):
        Settings(**_base_de_produccion(**{campo: valor}))  # type: ignore[arg-type]


def test_desarrollo_permite_configuracion_relajada() -> None:
    """Las guardas no deben estorbar en local."""
    ajustes = Settings(
        **_base_de_produccion(
            environment="development",
            kms_provider="local",
            docs_enabled=True,
            cors_origins=["http://localhost:3000"],
        )  # type: ignore[arg-type]
    )
    assert not ajustes.environment.es_productivo


def test_staging_aplica_las_mismas_guardas_que_produccion() -> None:
    """Staging sirve datos reales: relajarlo ahi es relajarlo donde importa."""
    with pytest.raises(ValueError, match="DOCS_ENABLED"):
        Settings(**_base_de_produccion(environment="staging", docs_enabled=True))  # type: ignore[arg-type]


def test_rechaza_clave_maestra_de_longitud_incorrecta() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Settings(**_base_de_produccion(master_key_b64="AAAA"))  # type: ignore[arg-type]


def test_rechaza_issuer_sin_https() -> None:
    with pytest.raises(ValueError, match="https"):
        Settings(**_base_de_produccion(oidc_issuer="http://tenant.auth0.com/"))  # type: ignore[arg-type]


def test_exige_al_menos_un_proveedor_de_correo() -> None:
    with pytest.raises(ValueError, match="proveedor de correo"):
        Settings(
            **_base_de_produccion(google_client_id=None, google_client_secret=None)  # type: ignore[arg-type]
        )


def test_normaliza_el_issuer_con_barra_final() -> None:
    """El `iss` del token siempre acaba en barra; sin normalizar, todo token se rechaza."""
    ajustes = Settings(**_base_de_produccion(oidc_issuer="https://tenant.auth0.com"))  # type: ignore[arg-type]
    assert ajustes.oidc_issuer == "https://tenant.auth0.com/"


# ── Redaccion de logs ────────────────────────────────────────────────


def test_redacta_secretos_en_el_primer_nivel() -> None:
    evento = procesador_de_redaccion(
        None, "info", {"event": "login", "access_token": "ya29.secreto", "ok": True}
    )
    assert evento["access_token"] == "***"
    assert evento["ok"] is True


def test_redacta_secretos_anidados() -> None:
    """
    El caso real: alguien loguea un payload entero para depurar. Aunque
    ese log llegue a produccion, el valor sale enmascarado.
    """
    evento = procesador_de_redaccion(
        None,
        "debug",
        {"payload": {"usuario": {"refresh_token": "1//secreto", "id": 42}}},
    )
    assert evento["payload"]["usuario"]["refresh_token"] == "***"
    assert evento["payload"]["usuario"]["id"] == 42


def test_redacta_dentro_de_listas() -> None:
    evento = procesador_de_redaccion(
        None, "info", {"conexiones": [{"proveedor": "google", "token": "abc"}]}
    )
    assert evento["conexiones"][0]["token"] == "***"
    assert evento["conexiones"][0]["proveedor"] == "google"


@pytest.mark.parametrize(
    "clave",
    [
        "password",
        "Authorization",
        "X-Refresh-Token",
        "client_secret",
        "code_verifier",
        "ruc",
        "email",
        "asunto",
        "ocr_text",
    ],
)
def test_redacta_todas_las_claves_sensibles(clave: str) -> None:
    evento = procesador_de_redaccion(None, "info", {clave: "valor-sensible"})
    assert evento[clave] == "***"


def test_no_redacta_lo_que_no_es_sensible() -> None:
    evento = procesador_de_redaccion(
        None, "info", {"trabajo_id": "abc", "duracion_ms": 120, "estado": "ok"}
    )
    assert evento["trabajo_id"] == "abc"
    assert evento["duracion_ms"] == 120


def test_la_redaccion_corta_estructuras_muy_anidadas() -> None:
    """Protege frente a estructuras ciclicas o absurdamente profundas."""
    profundo: dict[str, object] = {"n": "fin"}
    for _ in range(20):
        profundo = {"n": profundo}
    resultado = procesador_de_redaccion(None, "info", profundo)
    assert resultado is not None  # no desborda la pila


# ── Paginacion ───────────────────────────────────────────────────────


def test_el_cursor_va_y_vuelve() -> None:
    original = Cursor(creado_en=ahora_utc(), identificador=uuid7())
    recuperado = Cursor.decodificar(original.codificar())
    assert recuperado.identificador == original.identificador


def test_el_cursor_es_opaco() -> None:
    """Base64url sin relleno: seguro en una query string y sin estructura visible."""
    codificado = Cursor(creado_en=ahora_utc(), identificador=uuid7()).codificar()
    assert "=" not in codificado
    assert "/" not in codificado
    assert "+" not in codificado


@pytest.mark.parametrize("basura", ["no-es-base64!!", "YWJj", "", "e30"])
def test_un_cursor_manipulado_se_rechaza(basura: str) -> None:
    with pytest.raises(ErrorDeValidacion):
        Cursor.decodificar(basura)


def test_el_limite_de_pagina_tiene_tope() -> None:
    """Ninguna respuesta sin limite: es la regla que evita el volcado completo."""
    with pytest.raises(ValueError, match="less than or equal"):
        SolicitudDePagina(limite=10_000)
