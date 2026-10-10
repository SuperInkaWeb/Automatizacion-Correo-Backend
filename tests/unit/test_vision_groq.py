"""
Adaptador de vision sobre Groq.

Se verifica lo que no cubre el pipeline con dobles: que la respuesta del
modelo se valide contra el esquema del dominio, que la confianza se
recorte al techo, que un documento ajeno no produzca campos y que un
JSON malformado se trate como fallo del motor (y no tumbe el pipeline ni
guarde basura). El cliente es un doble: ningun test llama a la API real.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from mailauto.modules.extraction.domain.entities import (
    CampoExtraido,
    Estrategia,
    RegistroTributario,
)
from mailauto.modules.extraction.domain.ports import (
    DocumentoAExtraer,
    PerfilDeExtraccion,
)
from mailauto.modules.extraction.infrastructure.strategies import vision_comun
from mailauto.modules.extraction.infrastructure.strategies.vision_groq import VisionIAGroq

# ── Dobles ───────────────────────────────────────────────────────────


@dataclass
class _Mensaje:
    content: str | None


@dataclass
class _Eleccion:
    message: _Mensaje


@dataclass
class _Respuesta:
    choices: list[_Eleccion]


class _Completions:
    def __init__(self, cliente: _ClienteFalso) -> None:
        self._cliente = cliente

    async def create(self, **kwargs: object) -> _Respuesta:
        self._cliente.kwargs = kwargs
        if self._cliente.excepcion is not None:
            raise self._cliente.excepcion
        return _Respuesta([_Eleccion(_Mensaje(self._cliente.contenido))])


class _Chat:
    def __init__(self, cliente: _ClienteFalso) -> None:
        self.completions = _Completions(cliente)


class _ClienteFalso:
    """Imita lo justo del cliente OpenAI que usa el adaptador."""

    def __init__(self, contenido: str | None = None, excepcion: Exception | None = None) -> None:
        self.contenido = contenido
        self.excepcion = excepcion
        self.kwargs: dict[str, object] | None = None
        self.chat = _Chat(self)


class _PerfilTrivial(PerfilDeExtraccion):
    """El adaptador recibe el perfil pero no lo usa; basta con uno inerte."""

    @property
    def nombre(self) -> str:
        return "trivial"

    def reconoce(self, texto: str) -> bool:
        return True

    def extraer_campos(self, texto: str) -> dict[str, CampoExtraido]:
        return {}

    def a_registro(
        self, campos: dict[str, CampoExtraido], documento: DocumentoAExtraer
    ) -> RegistroTributario:  # pragma: no cover - la vision no lo invoca
        raise NotImplementedError


def _documento() -> DocumentoAExtraer:
    # Imagen: evita el rasterizado de PDF, que no es lo que se prueba aqui.
    return DocumentoAExtraer(
        adjunto_id=uuid4(),
        tenant_id=uuid4(),
        trabajo_id=uuid4(),
        contenido=b"imagen-de-prueba",
        tipo_mime="image/png",
        nombre="constancia.png",
    )


def _json_constancia(ruc_confianza: float = 0.95) -> str:
    return (
        '{"es_constancia_sunat": true,'
        f' "ruc_contribuyente": {{"valor": "20123456789", "confianza": {ruc_confianza}}},'
        ' "periodo": {"valor": "202509", "confianza": 0.8},'
        ' "importe_pagado": {"valor": "326.00", "confianza": 0.7}}'
    )


# ── Tests ────────────────────────────────────────────────────────────


async def test_una_lectura_valida_produce_campos_del_dominio() -> None:
    estrategia = VisionIAGroq(_ClienteFalso(_json_constancia()))

    resultado = await estrategia.leer(_documento(), _PerfilTrivial())

    assert resultado.error is None
    assert resultado.estrategia is Estrategia.VISION_IA
    assert resultado.campos["ruc_contribuyente"].valor == "20123456789"
    assert resultado.campos["periodo"].valor == "202509"
    assert resultado.campos["importe_pagado"].valor == "326.00"


async def test_la_confianza_se_recorta_al_techo() -> None:
    # El modelo declara 0.95; el dominio no deja pasar nada por encima del
    # techo, porque un registro con confianza alta se salta la revision.
    estrategia = VisionIAGroq(_ClienteFalso(_json_constancia(ruc_confianza=0.99)))

    resultado = await estrategia.leer(_documento(), _PerfilTrivial())

    assert resultado.campos["ruc_contribuyente"].confianza == vision_comun.TECHO_DE_CONFIANZA


async def test_un_documento_ajeno_no_produce_campos() -> None:
    estrategia = VisionIAGroq(_ClienteFalso('{"es_constancia_sunat": false}'))

    resultado = await estrategia.leer(_documento(), _PerfilTrivial())

    assert resultado.error == "documento_no_reconocido"
    assert resultado.campos == {}


async def test_un_json_malformado_es_fallo_del_motor_no_excepcion() -> None:
    # Groq devuelve algo que no cumple el esquema: no debe propagar la
    # excepcion (tumbaria el documento), sino registrarse como fallo para
    # que el pipeline siga con los motores gratuitos.
    estrategia = VisionIAGroq(_ClienteFalso("esto no es json"))

    resultado = await estrategia.leer(_documento(), _PerfilTrivial())

    assert resultado.error is not None
    assert resultado.campos == {}


async def test_envia_modo_json_y_la_imagen_como_data_url() -> None:
    cliente = _ClienteFalso(_json_constancia())
    estrategia = VisionIAGroq(cliente, modelo="qwen/qwen3.8-27b")

    await estrategia.leer(_documento(), _PerfilTrivial())

    assert cliente.kwargs is not None
    assert cliente.kwargs["model"] == "qwen/qwen3.8-27b"
    assert cliente.kwargs["response_format"] == {"type": "json_object"}
    mensajes = cliente.kwargs["messages"]
    assert isinstance(mensajes, list)
    # El sistema lleva las instrucciones; la imagen va en el bloque de
    # usuario como data URL, nunca concatenada al prompt de sistema.
    assert mensajes[0]["role"] == "system"
    contenido_usuario = mensajes[1]["content"]
    assert contenido_usuario[0]["type"] == "image_url"
    assert contenido_usuario[0]["image_url"]["url"].startswith("data:image/png;base64,")
