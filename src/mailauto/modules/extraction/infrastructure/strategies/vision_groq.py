"""
Adaptador de vision IA sobre Groq.

Proposito
    Mismo trabajo que el adaptador de Anthropic (leer los documentos que
    los motores gratuitos no descifran), pero contra Groq, que sirve
    modelos abiertos multimodales con un plan gratuito generoso. El
    esquema, el prompt y el rasterizado son los compartidos en
    `vision_comun`: este modulo solo aporta la llamada a la API de Groq.

Dependencias
    SDK de OpenAI (`openai`): Groq expone una API compatible, asi que se
    reutiliza el cliente estandar apuntando a su `base_url`. Mas lo
    compartido en `vision_comun`.

Decisiones de diseño
    1. Salida en modo JSON (`response_format`), no texto libre. El modelo
       devuelve un objeto JSON que se valida contra el esquema Pydantic
       del dominio. Si no valida, es un fallo del motor y el pipeline
       sigue con lo que dieron los gratuitos: nunca se guarda basura.

    2. El esquema viaja EN el prompt generado desde el propio modelo
       Pydantic, no escrito a mano: un campo nuevo en `vision_comun` se
       propaga solo, sin riesgo de que las dos listas se desincronicen.

    3. Mismas barreras anti-inyeccion que el otro proveedor: la imagen va
       en un bloque de usuario, el modelo no tiene herramientas y la
       salida pasa por los objetos de valor del dominio.

    4. `temperature=0`: transcribir no admite creatividad. La misma
       imagen debe dar siempre la misma lectura.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Final

from mailauto.modules.extraction.domain.entities import (
    Estrategia,
    ResultadoDeEstrategia,
)
from mailauto.modules.extraction.domain.ports import (
    DocumentoAExtraer,
    EstrategiaDeExtraccion,
    PerfilDeExtraccion,
)
from mailauto.modules.extraction.infrastructure.strategies import vision_comun as comun
from mailauto.shared.observability.logging import obtener_logger

logger = obtener_logger(__name__)

# Unico modelo de vision vigente en Groq (los Llama-3.2-vision y los
# qwen3.6 ya se apagaron). Configurable por si Groq lo renombra.
MODELO_POR_DEFECTO: Final = "qwen/qwen3.8-27b"
BASE_URL_POR_DEFECTO: Final = "https://api.groq.com/openai/v1"

# El esquema que debe devolver el modelo, generado desde el propio modelo
# Pydantic para que no se desincronice con el dominio.
_ESQUEMA_JSON: Final = json.dumps(comun.Lectura.model_json_schema(), ensure_ascii=False)

_INSTRUCCIONES_JSON: Final = (
    f"{comun.INSTRUCCIONES}\n\n"
    "Responde UNICAMENTE con un objeto JSON valido que cumpla este "
    f"esquema JSON Schema:\n{_ESQUEMA_JSON}\n"
    "No incluyas texto fuera del JSON."
)


class VisionIAGroq(EstrategiaDeExtraccion):
    """
    Lectura por modelo multimodal de Groq.

    Se construye con el cliente ya creado para poder sustituirlo por un
    doble en los tests: ningun test debe llamar a la API real.
    """

    def __init__(
        self,
        cliente: Any,  # noqa: ANN401 - cliente del SDK, inyectable para los tests
        *,
        modelo: str = MODELO_POR_DEFECTO,
    ) -> None:
        self._cliente = cliente
        self._modelo = modelo

    @property
    def nombre(self) -> Estrategia:
        return Estrategia.VISION_IA

    @property
    def costo_relativo(self) -> int:
        return 100

    def admite(self, documento: DocumentoAExtraer) -> bool:
        return documento.es_pdf or documento.es_imagen

    async def leer(
        self, documento: DocumentoAExtraer, perfil: PerfilDeExtraccion
    ) -> ResultadoDeEstrategia:
        inicio = time.perf_counter()
        try:
            imagen, tipo = await comun.preparar_imagen(documento)
            lectura = await self._consultar(imagen, tipo)
        except Exception as exc:  # noqa: BLE001 - frontera del motor
            # Incluye errores de red, de cuota, de la API y de validacion
            # del JSON. El pipeline sigue con lo que dieron los motores
            # previos: nunca se guarda una lectura a medias.
            logger.warning("vision_groq_fallo", error=type(exc).__name__)
            return ResultadoDeEstrategia(
                estrategia=self.nombre,
                duracion_ms=comun.ms(inicio),
                error=type(exc).__name__,
            )

        if not lectura.es_constancia_sunat:
            return ResultadoDeEstrategia(
                estrategia=self.nombre,
                duracion_ms=comun.ms(inicio),
                error="documento_no_reconocido",
            )

        return ResultadoDeEstrategia(
            estrategia=self.nombre,
            campos=comun.campos_desde_lectura(lectura, self.nombre),
            duracion_ms=comun.ms(inicio),
        )

    # ── Interno ──────────────────────────────────────────────────────

    async def _consultar(self, imagen: bytes, tipo_mime: str) -> comun.Lectura:
        """Llama a Groq en modo JSON y valida la respuesta contra el esquema."""
        datos = base64.standard_b64encode(imagen).decode()
        respuesta = await self._cliente.chat.completions.create(
            model=self._modelo,
            max_tokens=comun.MAXIMO_TOKENS,
            temperature=0,
            timeout=comun.TIMEOUT_SEGUNDOS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _INSTRUCCIONES_JSON},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{tipo_mime};base64,{datos}"},
                        },
                        {"type": "text", "text": comun.TEXTO_DE_USUARIO},
                    ],
                },
            ],
        )

        contenido = respuesta.choices[0].message.content
        if not contenido:
            raise ValueError("Groq devolvio una respuesta vacia")
        # `model_validate_json` lanza si el JSON no cumple el esquema; el
        # fallo lo captura `leer` y el documento va a revision humana.
        return comun.Lectura.model_validate_json(contenido)


def crear_cliente(
    api_key: str | None, *, base_url: str = BASE_URL_POR_DEFECTO
) -> Any | None:  # noqa: ANN401 - cliente del SDK, sin tipo publico estable
    """
    Construye el cliente asincrono compatible con OpenAI apuntando a Groq.

    Devuelve None si no hay credencial, y entonces el composition root no
    registra la estrategia: el pipeline funciona con los motores gratuitos.
    """
    if not api_key:
        return None
    from openai import AsyncOpenAI

    cliente: Any = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return cliente
