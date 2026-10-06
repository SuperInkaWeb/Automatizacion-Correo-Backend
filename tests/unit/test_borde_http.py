"""
Tests del borde HTTP: montaje de la app, middlewares y formato de errores.

Verifican la pila completa de entrada sin tocar base de datos ni Redis.
Es lo que detecta que una ruta no monta, que falta una cabecera de
seguridad o que un error filtra detalles internos: fallos que no aparecen
en ningun test de dominio y que solo se descubririan desplegando.

No se usa `with TestClient(app)` a proposito: eso ejecutaria el lifespan,
que abre conexiones reales. Estas pruebas cubren el borde, no el arranque
de la infraestructura.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mailauto.bootstrap.app import crear_app
from mailauto.bootstrap.settings import Settings
from mailauto.shared.errors import TokenInvalido
from tests.conftest import ajustes_de_pruebas


def _ajustes_de_produccion(**extra: Any) -> Settings:  # noqa: ANN401 - campos de configuracion heterogeneos
    propios: dict[str, Any] = {
        "environment": "production",
        "kms_provider": "local",
        "kms_local_en_produccion_aceptado": True,
        "docs_enabled": False,
        "cors_origins": ["https://app.ejemplo.com"],
        "oauth_redirect_uris": ["https://app.ejemplo.com/oauth/callback"],
        "metrics_token": "token-de-metricas",
    }
    return ajustes_de_pruebas(**{**propios, **extra})


class _VerificadorQueRechaza:
    """Rechaza cualquier token: basta para ejercitar el camino de error."""

    async def verificar(self, token: str) -> None:
        raise TokenInvalido()


def _cliente_de(ajustes: Settings) -> TestClient:
    app = crear_app(ajustes)
    # Sin lifespan no hay contenedor. Se inyecta uno minimo para que el
    # borde funcione sin abrir conexiones reales.
    # `redis=None` deja el limitador de tasa en modo pasante: estas
    # pruebas cubren el borde, no el conteo de peticiones.
    app.state.contenedor = SimpleNamespace(verificador=_VerificadorQueRechaza(), redis=None)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def cliente() -> TestClient:
    return _cliente_de(ajustes_de_pruebas())


# ── Montaje ──────────────────────────────────────────────────────────


def test_la_aplicacion_monta_todas_las_rutas_esperadas() -> None:
    rutas = {
        f"{metodo} {ruta}"
        for ruta, operaciones in crear_app(ajustes_de_pruebas()).openapi()["paths"].items()
        for metodo in operaciones
    }
    esperadas = {
        "get /health/live",
        "get /health/ready",
        "get /api/v1/me",
        "get /api/v1/mailboxes",
        "post /api/v1/mailboxes/authorize",
        "post /api/v1/mailboxes/callback",
        "delete /api/v1/mailboxes/{conexion_id}",
        "post /api/v1/scans",
        "get /api/v1/scans",
        "get /api/v1/scans/{trabajo_id}",
        "post /api/v1/scans/{trabajo_id}/cancel",
        "get /api/v1/scans/{trabajo_id}/stream",
        "get /api/v1/audit",
    }
    assert esperadas <= rutas


def test_la_sonda_de_vida_no_depende_de_nada_externo(cliente: TestClient) -> None:
    """
    Si /health/live tocara la base de datos, una caida de PostgreSQL haria
    que el orquestador reiniciase en bucle contenedores perfectamente sanos.
    """
    respuesta = cliente.get("/health/live")
    assert respuesta.status_code == 200
    assert respuesta.json()["data"]["estado"] == "vivo"


# ── Cabeceras de seguridad ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("cabecera", "valor"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cache-Control", "no-store"),
    ],
)
def test_toda_respuesta_lleva_cabeceras_de_endurecimiento(
    cliente: TestClient, cabecera: str, valor: str
) -> None:
    respuesta = cliente.get("/health/live")
    assert respuesta.headers[cabecera] == valor


def test_la_csp_de_la_api_no_permite_cargar_nada(cliente: TestClient) -> None:
    """La API devuelve JSON: no hay recurso legitimo que cargar desde ella."""
    csp = cliente.get("/health/live").headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_sin_hsts_fuera_de_produccion(cliente: TestClient) -> None:
    """HSTS sobre http://localhost dejaria el dominio inaccesible en el navegador."""
    assert "Strict-Transport-Security" not in cliente.get("/health/live").headers


def test_con_hsts_en_produccion() -> None:
    cliente = _cliente_de(_ajustes_de_produccion())
    assert "Strict-Transport-Security" in cliente.get("/health/live").headers


# ── Correlacion ──────────────────────────────────────────────────────


def test_cada_respuesta_lleva_un_identificador_de_peticion(
    cliente: TestClient,
) -> None:
    assert cliente.get("/health/live").headers["X-Request-ID"]


def test_se_respeta_el_identificador_del_cliente_si_es_un_uuid(
    cliente: TestClient,
) -> None:
    propio = "123e4567-e89b-12d3-a456-426614174000"
    respuesta = cliente.get("/health/live", headers={"X-Request-ID": propio})
    assert respuesta.headers["X-Request-ID"] == propio


def test_se_descarta_un_identificador_manipulado(cliente: TestClient) -> None:
    """
    Un valor arbitrario del cliente acabaria en los logs tal cual, y es
    la via clasica para inyectar lineas falsas en un agregador.
    """
    respuesta = cliente.get("/health/live", headers={"X-Request-ID": "no-es-uuid\ninyectado"})
    assert respuesta.headers["X-Request-ID"] != "no-es-uuid\ninyectado"


# ── Autenticacion y formato de errores ───────────────────────────────


@pytest.mark.parametrize(
    "ruta",
    ["/api/v1/me", "/api/v1/mailboxes", "/api/v1/scans", "/api/v1/audit"],
)
def test_las_rutas_protegidas_exigen_token(cliente: TestClient, ruta: str) -> None:
    assert cliente.get(ruta).status_code == 401


@pytest.mark.parametrize(
    "cabecera",
    ["", "Bearer", "Basic abc", "Bearer ", "token-suelto"],
)
def test_se_rechazan_las_cabeceras_de_autorizacion_malformadas(
    cliente: TestClient, cabecera: str
) -> None:
    respuesta = cliente.get("/api/v1/me", headers={"Authorization": cabecera})
    assert respuesta.status_code == 401


def test_los_errores_siguen_el_formato_problem_details(cliente: TestClient) -> None:
    cuerpo = cliente.get("/api/v1/me").json()
    assert cuerpo["status"] == 401
    assert cuerpo["title"]
    assert cuerpo["type"].startswith("urn:mailauto:error:")
    assert cuerpo["instance"] == "/api/v1/me"
    assert cuerpo["trace_id"]


def test_un_error_no_expone_detalles_internos(cliente: TestClient) -> None:
    """Ni trazas, ni nombres de tabla, ni rutas de fichero del servidor."""
    texto = cliente.get("/api/v1/me").text.lower()
    for filtracion in (
        "traceback",
        "sqlalchemy",
        "site-packages",
        "c:\\",
        ".py",
        "scan_jobs",
        "mailbox_connections",
    ):
        assert filtracion not in texto


def test_una_respuesta_de_error_no_refleja_lo_enviado(cliente: TestClient) -> None:
    """
    Lo que el cliente envio puede ser un token o un dato personal.
    Reflejarlo en el error lo replicaria en los logs de quien llama.
    """
    respuesta = cliente.post(
        "/api/v1/mailboxes/authorize",
        json={"proveedor": "valor-secreto-del-cliente", "redirect_uri": "x"},
        headers={"Authorization": "Bearer token-invalido"},
    )
    assert respuesta.status_code in (401, 422)
    assert "valor-secreto-del-cliente" not in respuesta.text


def test_la_documentacion_no_se_publica_en_produccion() -> None:
    """`/docs` es un mapa completo de la superficie de ataque."""
    cliente = _cliente_de(_ajustes_de_produccion())
    assert cliente.get("/docs").status_code == 404
    assert cliente.get("/openapi.json").status_code == 404


def test_la_documentacion_si_se_publica_en_desarrollo(cliente: TestClient) -> None:
    assert cliente.get("/docs").status_code == 200


# ── CORS ─────────────────────────────────────────────────────────────


def test_no_se_refleja_un_origen_no_permitido(cliente: TestClient) -> None:
    """
    Reflejar el `Origin` recibido equivale a permitir cualquier dominio,
    y con `allow_credentials` convierte cada endpoint en un CSRF.
    """
    respuesta = cliente.get("/health/live", headers={"Origin": "https://atacante.ejemplo"})
    assert respuesta.headers.get("Access-Control-Allow-Origin") != ("https://atacante.ejemplo")


def test_la_configuracion_de_produccion_no_admite_comodin_en_cors() -> None:
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        _ajustes_de_produccion(cors_origins=["*"])
