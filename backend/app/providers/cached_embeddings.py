"""Reutiliza embeddings de consulta solo dentro de una petición y perfil de proveedor."""

import asyncio  # Integra esperas compartidas con el bucle asíncrono.
from concurrent.futures import Future  # Un resultado compartido sirve para consumidores sync y async.
from threading import Lock  # Protege el registro frente a consultas desde distintos hilos.

from langchain_core.embeddings import Embeddings  # Mantiene la interfaz esperada por Chroma y FAISS.


class RequestEmbeddings(Embeddings):  # Una instancia por petición impide compartir preguntas entre usuarios.
    def __init__(self, delegate: Embeddings):
        self.delegate = delegate  # Cliente real correspondiente al proveedor y modelo del índice.
        self.queries: dict[str, Future[list[float]]] = {}  # Memoriza resultados y errores de cada texto exacto.
        self.lock = Lock()  # El candado solo protege el mapa; nunca abarca la llamada de red.

    def _query(self, text: str) -> tuple[Future[list[float]], bool]:
        with self.lock:  # Decide de forma atómica quién calculará el embedding de esta pregunta.
            if text in self.queries:  # FAQ y documentos comparten incluso el trabajo todavía en curso.
                return self.queries[text], False  # Este consumidor esperará el resultado existente.
            pending: Future[list[float]] = Future()  # Reserva una única operación para este texto.
            self.queries[text] = pending  # Publica la reserva antes de liberar el candado.
            return pending, True  # El primer consumidor ejecuta la petición al proveedor.

    def embed_query(self, text: str) -> list[float]:
        pending, owner = self._query(text)  # Comparte el mismo resultado con consumidores async.
        if owner:  # Solo el propietario puede iniciar una llamada potencialmente facturable.
            try:
                pending.set_result(list(self.delegate.embed_query(text)))  # Copia el vector del SDK.
            except BaseException as error:  # Desbloquea a todos los consumidores también ante cancelación.
                pending.set_exception(error)  # Reutiliza el error; no reintenta a escondidas en esta petición.
        return list(pending.result())  # Devuelve una copia para que un consumidor no altere la caché.

    async def aembed_query(self, text: str) -> list[float]:
        pending, owner = self._query(text)  # Evita computar dos veces consultas simultáneas idénticas.
        if owner:  # Otras preguntas distintas pueden avanzar en paralelo.
            try:
                pending.set_result(list(await self.delegate.aembed_query(text)))  # Usa el cliente async nativo.
            except BaseException as error:  # Incluye CancelledError para no dejar esperas bloqueadas.
                pending.set_exception(error)  # Un consumidor posterior recibe el mismo fallo sin nueva llamada.
        if pending.done():  # Un acierto de caché o resultado propio no necesita programar otra espera.
            return list(pending.result())  # Propaga también errores/cancelaciones sin repetir la petición.
        # Cancelar un consumidor que espera no cancela el resultado que necesitan los demás.
        return list(await asyncio.shield(asyncio.wrap_future(pending)))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.delegate.embed_documents(texts)  # Mantiene el batching nativo de la indexación.

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.delegate.aembed_documents(texts)  # No mezcla vectores de documento con los de consulta.
