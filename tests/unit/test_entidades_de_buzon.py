"""
Tests de las reglas de la conexion de buzon.

Fijan el contrato de `tiene_alcances_suficientes`, que decide si un
consentimiento OAuth basta para escanear. El punto delicado es que cada
proveedor devuelve los alcances concedidos con una forma distinta, y la
comprobacion solo debe mirar el de LECTURA de correo, nunca el de
identidad `email`.
"""

from __future__ import annotations

from mailauto.modules.mailbox.domain.entities import ConexionDeBuzon, Proveedor


def _conexion(proveedor: Proveedor, alcances: tuple[str, ...]) -> ConexionDeBuzon:
    return ConexionDeBuzon(proveedor=proveedor, alcances_concedidos=alcances)


def test_google_concede_lectura_aunque_email_llegue_como_url() -> None:
    """
    Regresion: Google devuelve `email` como `.../userinfo.email`, no como
    el literal `email`. El predicado no debe exigir ese literal, o la
    vinculacion de Gmail falla pese a haberse concedido la lectura.
    """
    conexion = _conexion(
        Proveedor.GOOGLE,
        (
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/gmail.readonly",
        ),
    )
    assert conexion.tiene_alcances_suficientes() is True


def test_google_sin_gmail_readonly_es_insuficiente() -> None:
    conexion = _conexion(
        Proveedor.GOOGLE,
        ("openid", "https://www.googleapis.com/auth/userinfo.email"),
    )
    assert conexion.tiene_alcances_suficientes() is False


def test_microsoft_con_mail_read_es_suficiente() -> None:
    conexion = _conexion(
        Proveedor.MICROSOFT,
        (
            "openid",
            "email",
            "offline_access",
            "https://graph.microsoft.com/Mail.Read",
        ),
    )
    assert conexion.tiene_alcances_suficientes() is True


def test_microsoft_sin_mail_read_es_insuficiente() -> None:
    conexion = _conexion(
        Proveedor.MICROSOFT,
        ("openid", "email", "offline_access"),
    )
    assert conexion.tiene_alcances_suficientes() is False
