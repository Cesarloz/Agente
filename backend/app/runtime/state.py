"""Contrato de datos del grafo: el estado circula en memoria durante un único turno."""

from __future__ import annotations  # Evalúa las anotaciones al necesitarlas, evitando referencias prematuras.

from typing import Any, Literal, Protocol  # Tipos abiertos, valores restringidos e interfaz de los servicios.

from typing_extensions import TypedDict  # Describe las claves del diccionario que intercambian los nodos.


class AgentState(TypedDict, total=False):
    """Todas las claves son opcionales para permitir actualizaciones parciales por nodo."""

    conversation_id: str  # Identifica el historial persistido en PostgreSQL.
    agent_id: str  # Delimita la configuración, fuentes y herramientas autorizadas.
    user_id: str  # Identificador externo del usuario, si el llamador lo facilita.
    channel: str  # Origen del mensaje: API, web o una integración de mensajería.
    config: dict[str, Any]  # Copia de configuración del agente aplicada a este turno.
    messages: list[dict[str, Any]]  # Historial limitado y mensaje actual; no almacena credenciales.
    question: str  # Pregunta de este turno, utilizada por alcance, recuperación y generación.
    interaction_count: int  # Total de turnos aceptados que se volverá a persistir.
    max_interactions: int  # Límite que cierra el grafo antes de nuevas llamadas al modelo.
    scope_status: Literal["IN_SCOPE", "UNCERTAIN", "OUT_OF_SCOPE"]  # Resultado validado del alcance.
    retrieved_documents: list[dict[str, Any]]  # Fragmentos RAG, con contenido y metadatos de sus fuentes.
    retrieved_faqs: list[dict[str, Any]]  # FAQs exactas o recuperadas semánticamente.
    tool_calls: list[dict[str, Any]]  # Plan completo de herramientas, conservado para inspección.
    pending_tools: list[dict[str, Any]]  # Cola que se consume una vez por herramienta ejecutada.
    tool_results: list[dict[str, Any]]  # Resultados que serán evidencia para la respuesta final.
    attachments: list[dict[str, Any]]  # Archivos autorizados que el canal puede entregar al usuario.
    conversation_status: str  # ACTIVE, CLOSED o ERROR; PostgreSQL conserva el valor entre turnos.
    context: str  # Evidencia serializada y acotada; no sustituye al prompt de sistema.
    response: str  # Texto final del modelo, de una FAQ o de una política local.
    tokens: int  # Uso LLM reportado por los hooks; no representa tokens de embeddings.
    answered: bool  # Indica si hubo información suficiente para contestar la pregunta.
    trace: list[dict[str, Any]]  # Nodos, decisiones y tiempos; evita copiar prompts o secretos.
    error: dict[str, str] | None  # Error público saneado, sin el mensaje original del proveedor.
    accepted: bool  # Marca de un solo uso para incrementar una vez el contador del turno.
    route: str  # Nombre de la siguiente transición elegida por el nodo.


class RuntimeHooks(Protocol):
    """Interfaz de servicios: el grafo no implementa persistencia ni acceso a credenciales."""

    # Clasifica el alcance; la implementación puede resolverlo localmente o consultar el LLM.
    async def classify_scope(self, state: AgentState) -> str: ...

    # Busca una FAQ exacta primero y, si hace falta, consulta el índice vectorial de FAQs.
    async def retrieve_faqs(self, state: AgentState) -> list[dict]: ...

    # Recupera los fragmentos documentales relevantes usando embeddings de la pregunta.
    async def retrieve_documents(self, state: AgentState) -> list[dict]: ...

    # Devuelve un plan de herramientas permitidas; una lista vacía evita ejecutarlas.
    async def decide_tools(self, state: AgentState) -> list[dict]: ...

    # Ejecuta una llamada validada y devuelve evidencia y, cuando aplica, adjuntos.
    async def execute_tool(self, call: dict, state: AgentState) -> dict: ...

    # Envía instrucciones, historial, pregunta y evidencia al proveedor y reporta su uso.
    async def generate(self, state: AgentState) -> dict: ...
