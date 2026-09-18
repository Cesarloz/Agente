"""Transporte NDJSON de chat con sesión propia, cola acotada y cancelación ordenada."""

import asyncio  # Coordina el productor, la espera de eventos y la desconexión del cliente.
import json  # Codifica un objeto independiente por línea para el cliente HTTP.
from collections.abc import AsyncIterator  # Describe la salida incremental del generador.
from contextlib import suppress  # Espera la cancelación del productor sin ocultar otros fallos.

from app.errors import AppError  # Solo estos mensajes están preparados para mostrarse públicamente.
from app.services.conversations import ConversationService  # Ejecuta y persiste el turno dentro de su sesión.


PING_SECONDS = 10  # Mantiene activa la conexión durante etapas sin nuevos fragmentos de texto.
STREAM_ERROR = "No fue posible completar la respuesta. Inténtalo de nuevo."  # No revela detalles del SDK.


def _line(event: dict) -> str:
    """Serializa antes de encolar para detectar salidas inválidas dentro del productor."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"  # NDJSON requiere el salto final.


async def chat_events(session_factory, agent_id, data, finalize) -> AsyncIterator[str]:
    """Emite snapshots provisionales y un resultado confirmado; finalize valida la salida pública.

    ConversationService confirma el turno antes de devolver el resultado y debe
    propagar CancelledError después de persistir la auditoría de una cancelación.
    El consumidor debe cerrar este generador al desconectarse (StreamingResponse
    cancela la iteración cuando detecta el cierre del cliente).
    """
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)  # Un cliente lento nunca acumula snapshots ilimitados.

    async def emit(snapshot: str) -> None:
        if not isinstance(snapshot, str):
            raise TypeError("El snapshot de respuesta debe ser texto")  # Se convertirá en error público saneado.
        message = _line({"type": "text", "text": snapshot})  # El texto es acumulado, no un delta para concatenar.
        if queue.full():
            queue.get_nowait()  # Sustituye una versión antigua todavía no consumida por la versión actual.
        queue.put_nowait(message)  # No cede control entre retirar y añadir: la sustitución es atómica en este loop.

    async def produce() -> None:
        try:
            async with session_factory() as session:  # La sesión vive hasta completar finalize, fuera del request inicial.
                result = await ConversationService(session).chat(agent_id, data, on_response=emit)  # Devuelve tras commit.
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError  # Evita finalizar si un servicio absorbió una cancelación pendiente.
                payload = await finalize(session, result)  # Aplica permisos y URLs públicas a los adjuntos confirmados.
                terminal = _line({"type": "done", "result": payload})  # Solo el resultado confirmado es definitivo.
            # Cierra la sesión antes de anunciar done para que el consumidor no interrumpa su limpieza al terminar.
        except asyncio.CancelledError:
            raise  # La desconexión no se convierte en un error que se intente enviar al cliente ausente.
        except Exception as error:
            terminal = _line({
                "type": "error",  # Un fallo de chat, finalize, sesión o serialización termina la transmisión.
                "message": error.message if isinstance(error, AppError) else STREAM_ERROR,  # Nunca expone str(error) desconocido.
                "status": error.status if isinstance(error, AppError) else 500,  # El HTTP ya iniciado informa estado en el evento.
            })
        await queue.put(terminal)  # Conserva el último snapshot; espera espacio para el único evento terminal.

    producer = asyncio.create_task(produce(), name="chat-stream-producer")  # El generador es dueño de esta tarea.
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)  # Espera sin bloquear el loop.
            except TimeoutError:
                yield _line({"type": "ping"})  # No implica avance del modelo ni consumo adicional de tokens.
                continue  # Vuelve a esperar snapshots o el evento definitivo.
            yield message  # NDJSON permite al cliente procesar la línea antes de terminar el turno.
            if json.loads(message)["type"] in {"done", "error"}:
                break  # El protocolo emite exactamente un terminal y no envía más texto después.
    finally:
        if not producer.done():
            producer.cancel()  # Desconectar o cerrar el generador interrumpe también modelo y sesión del productor.
        with suppress(asyncio.CancelledError):
            await producer  # Espera la auditoría de cancelación y __aexit__; no deja tareas huérfanas.
