"""Vista incremental de Answer.response; la validación final sigue siendo del SDK."""

from collections.abc import Awaitable, Callable

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessageChunk
from langchain_core.messages.ai import add_usage
from langchain_core.utils.json import parse_partial_json


class AnswerStream(AsyncCallbackHandler):
    """Extrae texto visible sin publicar el JSON, herramientas ni razonamiento.

    Se registra solamente en la llamada final Answer. Los callbacks llegan antes
    que la salida de with_structured_output(include_raw=True), cuyo fallback
    acumula los fragmentos antes de devolver raw/parsed en los SDK instalados.
    """

    run_inline = True  # Mantiene el orden de snapshots y respeta el control de flujo del consumidor.
    raise_error = True  # Un fallo del consumidor se propaga; nunca causa una segunda llamada al modelo.

    def __init__(self, on_response: Callable[[str], Awaitable[None]]):
        self.on_response = on_response  # Recibe el campo response completo acumulado, nunca un token crudo.
        self.response = ""  # Último prefijo publicado; evita duplicados o retrocesos por parseo parcial.
        self._text = ""  # JSON textual acumulado únicamente de bloques visibles.
        self._arguments: dict[object, str] = {}  # Argumentos de llamadas estructuradas, separados por índice.
        self.usage_metadata = None  # Conserva consumo conocido aunque el stream falle antes de su salida final.
        self.response_metadata = {}  # Interfaz compatible con el lector de uso del runtime.
        self._preview_limit = 131_072  # Acota el trabajo del parser; la respuesta final conserva su tamaño real.

    async def publish(self, response):
        """Emite un prefijo nuevo únicamente si es texto válido y crece sin reemplazos."""
        if not isinstance(response, str):
            return  # Un objeto/lista dentro de response no se convierte en texto público.
        if response and 0xD800 <= ord(response[-1]) <= 0xDBFF:
            response = response[:-1]  # Espera el segundo escape de un par Unicode dividido entre fragmentos.
        if not response or response == self.response or not response.startswith(self.response):
            return  # Ignora fragmentos vacíos, repetidos o revisiones todavía no consolidadas.
        try:
            response.encode("utf-8")  # Nunca entrega un surrogate aislado que rompa JSON/SSE del transporte.
        except UnicodeEncodeError:
            return
        await self.on_response(response)  # El transporte recibe solamente texto destinado al usuario.
        self.response = response  # Avanza solo después de entregar el snapshot.

    async def _parse(self, text):
        if not text.lstrip().startswith("{") or len(text) > self._preview_limit:
            return  # Sin un objeto JSON reconocible, espera el resultado validado al final.
        try:
            parsed = parse_partial_json(text)  # Decodifica comillas, barras y saltos incompletos sin regex.
        except (ValueError, TypeError, RecursionError):
            return  # Un fragmento incompleto/malformado no es motivo para repetir la petición.
        if isinstance(parsed, dict):
            await self.publish(parsed.get("response"))  # Nunca publica answered, scope_status ni calls.

    async def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        message = getattr(chunk, "message", chunk)  # LangChain entrega ChatGenerationChunk en ambos SDK.
        if not isinstance(message, AIMessageChunk):
            return  # El token suelto no distingue texto público de bloques de razonamiento.
        if message.usage_metadata:
            self.usage_metadata = add_usage(self.usage_metadata, message.usage_metadata)  # Deltas normalizados.
        # content_blocks normaliza OpenAI/Gemini; thinking/reasoning/signatures no son bloques text.
        visible = "".join(block.get("text", "") for block in message.content_blocks
                          if block.get("type") == "text" and isinstance(block.get("text"), str))
        if visible and len(self._text) <= self._preview_limit:
            self._text += visible  # No usa token: puede contener JSON, razonamiento o estar vacío con tools.
            await self._parse(self._text)
        for position, call in enumerate(message.tool_call_chunks):
            # El índice permanece estable aunque el proveedor envíe el ID solamente en el primer fragmento.
            key = call.get("index") if call.get("index") is not None else position
            arguments = call.get("args")
            if not isinstance(arguments, str):
                continue
            previous = self._arguments.get(key, "")
            if len(previous) > self._preview_limit:
                continue  # Detiene solo la vista parcial de argumentos excesivos, no la generación.
            self._arguments[key] = previous + arguments
            await self._parse(self._arguments[key])


async def collect_answer_stream(runnable, messages, observer):
    """Consume una sola llamada y conserva la salida include_raw para validación/uso."""
    output = {}
    async for part in runnable.astream(messages, config={"callbacks": [observer]}):
        for key, value in part.items():
            if key == "raw" and key in output and isinstance(output[key], AIMessageChunk):
                output[key] = output[key] + value  # Compatible también con wrappers que emiten raw por partes.
            else:
                output[key] = value  # parsed y parsing_error conservan su última versión, sin concatenarse.
    return output  # No llama a ainvoke como fallback: evita una segunda solicitud facturable.
