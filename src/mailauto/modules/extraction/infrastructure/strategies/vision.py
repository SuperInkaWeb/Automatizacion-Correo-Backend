"""
Adaptador de vision IA sobre Anthropic.

Proposito
    Leer los documentos que los tres motores anteriores no consiguieron
    descifrar: fotos torcidas, impresiones con poca tinta, sellos que
    tapan parte del texto. Es uno de los proveedores de vision
    intercambiables; el esquema, el prompt y el rasterizado son comunes
    (`vision_comun`).

Dependencias
    SDK oficial de Anthropic (`anthropic`), mas lo compartido en
    `vision_comun`.

Decisiones de diseño
    1. Es el ultimo escalon a proposito: cuesta dinero por documento,
       mientras que los tres anteriores son gratis. El orquestador solo
       llega hasta aqui cuando lo barato ya fallo.

    2. Salida forzada a un esquema con `messages.parse()`. No se pide
       "devuelve un JSON" y luego se intenta parsear el texto: el
       esquema lo impone la API y la respuesta llega ya validada. Eso
       elimina de golpe la clase entera de fallos por respuesta
       malformada.

    3. **El documento es entrada no confiable.** Lo envia un tercero
       cualquiera y puede llevar texto escrito para que el modelo lo
       obedezca. Tres barreras: la imagen va en un bloque de usuario
       delimitado y nunca concatenada al sistema; el modelo no tiene
       herramientas; y la salida pasa por los objetos de valor del
       dominio, que descartan un RUC o una fecha imposibles.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Final

from mailauto.modules.extraction.domain.entities import (
    CampoExtraido,
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

MODELO_POR_DEFECTO: Final = "claude-opus-5-5"

# Extraer campos de un formulario conocido es una tarea de clasificacion,
# no de razonamiento: con esfuerzo bajo acierta igual y cuesta una
# fraccion. Configurable para subirlo si la medicion dijera otra cosa.
ESFUERZO_POR_DEFECTO: Final = "low"


class VisionIA(EstrategiaDeExtraccion):
    """
    Lectura por modelo multimodal de Anthropic.

    Se construye con el cliente ya creado para poder sustituirlo por un
    doble en los tests: ningun test debe gastar dinero real.
    """

    def __init__(
        self,
        cliente: Any,  # noqa: ANN401 - cliente del SDK, inyectable para los tests
        *,
        modelo: str = MODELO_POR_DEFECTO,
        esfuerzo: str = ESFUERZO_POR_DEFECTO,
    ) -> None:
        self._cliente = cliente
        self._modelo = modelo
        self._esfuerzo = esfuerzo

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
            # Incluye errores de red, de cuota y de la propia API. El
            # pipeline sigue con lo que hayan dado los motores previos.
            logger.warning("vision_ia_fallo", error=type(exc).__name__)
            return ResultadoDeEstrategia(
                estrategia=self.nombre,
                duracion_ms=comun.ms(inicio),
                error=type(exc).__name__,
            )

        if not lectura.es_constancia_sunat:
            # El modelo dice que el documento no es lo que se busca. Se
            # registra como resultado, no como error: evita que el
            # adjunto vuelva a procesarse.
            return ResultadoDeEstrategia(
                estrategia=self.nombre,
                duracion_ms=comun.ms(inicio),
                error="documento_no_reconocido",
            )

        return ResultadoDeEstrategia(
            estrategia=self.nombre,
            campos=self._a_campos(lectura),
            duracion_ms=comun.ms(inicio),
        )

    # ── Interno ──────────────────────────────────────────────────────

    async def _consultar(self, imagen: bytes, tipo_mime: str) -> comun.Lectura:
        """
        Llama al modelo con la salida forzada al esquema.

        `messages.parse` devuelve la respuesta ya validada contra el
        modelo Pydantic, asi que no hay que parsear texto ni manejar el
        caso de un JSON malformado.
        """
        respuesta = await self._cliente.messages.parse(
            model=self._modelo,
            max_tokens=comun.MAXIMO_TOKENS,
            system=comun.INSTRUCCIONES,
            output_config={"effort": self._esfuerzo},
            output_format=comun.Lectura,
            timeout=comun.TIMEOUT_SEGUNDOS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": tipo_mime,
                                "data": base64.standard_b64encode(imagen).decode(),
                            },
                        },
                        {"type": "text", "text": comun.TEXTO_DE_USUARIO},
                    ],
                }
            ],
        )

        lectura: comun.Lectura = respuesta.parsed_output
        return lectura

    def _a_campos(self, lectura: comun.Lectura) -> dict[str, CampoExtraido]:
        return comun.campos_desde_lectura(lectura, self.nombre)


def crear_cliente(api_key: str | None) -> Any | None:  # noqa: ANN401 - cliente del SDK, sin tipo publico estable
    """
    Construye el cliente asincrono de Anthropic.

    Devuelve None si no hay credencial configurada, y entonces el
    composition root simplemente no registra esta estrategia: el
    pipeline funciona igual con los tres motores gratuitos.
    """
    if not api_key:
        return None
    from anthropic import AsyncAnthropic

    cliente: Any = AsyncAnthropic(api_key=api_key)
    return cliente
