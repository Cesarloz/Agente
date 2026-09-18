"""Exporta adaptadores tecnológicos; los servicios dependen de estos contratos."""

from .embeddings import EmbeddingProvider, GeminiEmbeddingProvider, OpenAIEmbeddingProvider  # Texto a vector.
from .llm import GeminiLLMProvider, LLMProvider, OpenAILLMProvider, list_models  # Chat y catálogo de modelos.
from .loaders import LoaderPipeline  # Extrae texto y lo divide antes de indexar.
from .vectors import ChromaVectorStoreProvider, FAISSVectorStoreProvider, VectorStoreProvider  # Índices RAG.

__all__ = [  # Explicita la API pública que importan los servicios de la aplicación.
    "EmbeddingProvider", "GeminiEmbeddingProvider", "OpenAIEmbeddingProvider",  # Fábricas de embeddings.
    "LLMProvider", "GeminiLLMProvider", "OpenAILLMProvider", "list_models",  # Fábricas de chat y catálogo.
    "LoaderPipeline", "VectorStoreProvider", "ChromaVectorStoreProvider", "FAISSVectorStoreProvider",  # RAG.
]
