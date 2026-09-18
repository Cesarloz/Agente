"""Clientes que convierten preguntas y fragmentos RAG en vectores numéricos."""

from __future__ import annotations  # Mantiene anotaciones de tipos sin evaluación temprana.

from abc import ABC, abstractmethod  # Contrato obligatorio para implementaciones intercambiables.

from langchain_core.embeddings import Embeddings  # Interfaz sync/async que utilizan los índices.


class EmbeddingProvider(ABC):  # Define cómo crear un adaptador sin persistir la clave en texto plano.
    @abstractmethod  # Las implementaciones deben proporcionar build.
    def build(self, api_key: str, model: str) -> Embeddings:
        """Construye el cliente; embed_query/embed_documents efectúan las peticiones."""


class OpenAIEmbeddingProvider(EmbeddingProvider):  # Vectorización mediante el endpoint de OpenAI.
    def build(self, api_key: str, model: str) -> Embeddings:
        from langchain_openai import OpenAIEmbeddings  # Carga el SDK solo al seleccionar OpenAI.

        if not api_key or not model:  # No inicia trabajo facturable sin configuración completa.
            raise ValueError("Configura la credencial y el modelo de embeddings.")
        # El worker ya reintenta indexaciones fallidas; evita multiplicar esos intentos dentro del SDK.
        return OpenAIEmbeddings(api_key=api_key, model=model, request_timeout=45, max_retries=0)


class GeminiEmbeddingProvider(EmbeddingProvider):  # Vectorización mediante el endpoint de Gemini.
    def build(self, api_key: str, model: str) -> Embeddings:
        from google.genai.types import HttpOptions, HttpRetryOptions  # Opciones de transporte del SDK.
        from langchain_google_genai import GoogleGenerativeAIEmbeddings  # Conserva su batching y task_type.

        if not api_key or not model:  # Evita peticiones inválidas y errores tardíos de configuración.
            raise ValueError("Configura la credencial y el modelo de embeddings.")

        class BoundedGeminiEmbeddings(GoogleGenerativeAIEmbeddings):
            """Aplica límites en la petición real del adaptador instalado (4.4).

            Su campo request_options se declara pero no se usa al construir la
            petición; _build_config es el punto común de consulta/documentos sync/async.
            """

            def _build_config(self, **kwargs):  # Conserva tipos de recuperación y dimensiones del padre.
                config = super()._build_config(**kwargs)  # Incluye RETRIEVAL_QUERY o RETRIEVAL_DOCUMENT.
                config.http_options = HttpOptions(
                    timeout=45_000,  # Google GenAI espera milisegundos, equivalentes a 45 segundos.
                    retry_options=HttpRetryOptions(attempts=1),  # Un intento total; ningún reintento oculto.
                )
                return config  # Las cuatro rutas de embeddings pasan esta configuración al SDK.

        return BoundedGeminiEmbeddings(google_api_key=api_key, model=model)  # Construye sin vectorizar aún.
