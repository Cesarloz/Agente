# Difiere la evaluación de anotaciones de tipos sin alterar el comportamiento del índice.
from __future__ import annotations

# Coordina operaciones asíncronas y ejecuta disco/FAISS fuera del bucle de eventos.
import asyncio
# Genera huellas estables para separar agentes y modelos en los nombres de colección.
import hashlib
# Serializa metadatos y vectores numéricos sin cargar objetos ejecutables.
import json
# Realiza reemplazos atómicos, sincronización de disco y limpieza de temporales.
import os
# Limpia los nombres públicos de colección de caracteres no admitidos.
import re
# Crea archivos temporales únicos en el mismo volumen que el índice final.
import tempfile
# Implementa pausas breves únicamente ante bloqueos transitorios de archivos de Windows.
import time
# Define el contrato que deben implementar los proveedores de almacenamiento vectorial.
from abc import ABC, abstractmethod
# Resuelve y valida rutas de los índices persistidos.
from pathlib import Path
# Protege la creación perezosa del cliente Chroma cuando _store se ejecuta desde varios hilos.
from threading import Lock
# Describe metadatos heterogéneos conservados en los resultados.
from typing import Any
# Permite compartir bloqueos sin retener indefinidamente los que ya no están en uso.
from weakref import WeakValueDictionary

# Representa cada fragmento de texto con su ID y sus metadatos.
from langchain_core.documents import Document
# Describe la interfaz de cálculo de embeddings, separada de la generación de respuestas.
from langchain_core.embeddings import Embeddings
# Integra la búsqueda con el contrato invoke/ainvoke de LangChain.
from langchain_core.retrievers import BaseRetriever

# Comparte un bloqueo por archivo entre instancias FAISS del mismo proceso.
_FAISS_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


# Construye un espacio vectorial separado por agente, tipo de contenido y modelo.
def collection_name(agent_id: str, kind: str, profile: str) -> str:
    """Aísla rutas, agentes con nombres similares y dimensiones de distintos modelos."""
    # Unifica alias singulares y plurales para que apunten a la misma colección.
    kinds = {"faq": "faq", "faqs": "faq", "document": "documents", "documents": "documents"}
    # Rechaza una selección incompleta antes de crear índices o rutas.
    if kind not in kinds or not agent_id or not profile:
        # Explica los identificadores requeridos al llamador.
        raise ValueError("Se requieren agente, tipo de conocimiento y perfil de embeddings.")
    # Crea un prefijo legible de longitud limitada que no contiene separadores de ruta.
    slug = re.sub(r"[^a-z0-9]", "_", str(agent_id).lower()).strip("_")[:24] or "agent"
    # Distingue IDs de agente que producirían el mismo prefijo limpio.
    agent_hash = hashlib.sha256(str(agent_id).encode()).hexdigest()[:12]
    # Evita mezclar vectores de distintos modelos o proveedores de embeddings.
    profile_hash = hashlib.sha256(profile.encode()).hexdigest()[:16]
    # Combina el nombre legible y las huellas para producir una colección estable.
    return f"agent_{slug}_{agent_hash}_{kinds[kind]}_{profile_hash}"


# Adapta Document al formato compartido por el servicio RAG.
def _document_result(document: Document) -> dict[str, Any]:
    # Expone ID, texto y metadatos; ambos alias de texto se mantienen por compatibilidad.
    return {"id": document.id, "content": document.page_content,
            "page_content": document.page_content, "metadata": document.metadata}


# Valida el lote antes de realizar cálculos de embeddings que pueden tener coste.
def _validate(documents: list[Document], ids: list[str]) -> None:
    # Detecta diferencias de longitud, IDs duplicados y cadenas de ID vacías.
    if len(documents) != len(ids) or len(ids) != len(set(ids)) or any(not item for item in ids):
        # Interrumpe el upsert antes de sobrescribir accidentalmente fragmentos del lote.
        raise ValueError("Los documentos requieren IDs únicos y de la misma longitud.")


# Valida y construye las opciones comunes de recuperación.
def _search_options(k: int, search_type: str) -> dict[str, Any]:
    # Admite similitud pura o MMR, que combina relevancia y diversidad.
    if search_type not in {"similarity", "mmr"}:
        # Informa un modo no implementado sin realizar una búsqueda externa.
        raise ValueError("Usa búsqueda similarity o mmr.")
    # Limita el número de resultados para evitar consultas desproporcionadas.
    if k < 1 or k > 100:
        # Devuelve el intervalo permitido al llamador.
        raise ValueError("k debe estar entre 1 y 100.")
    # MMR evalúa más candidatos locales, pero no multiplica las llamadas al modelo de embeddings.
    return {"k": k, **({"fetch_k": max(20, k * 4)} if search_type == "mmr" else {})}


# Permite intercambiar Chroma y FAISS sin cambiar la lógica de KnowledgeService.
class VectorStoreProvider(ABC):
    @abstractmethod
    # Exige insertar o reemplazar documentos usando IDs estables y un perfil de embeddings.
    async def upsert(self, agent_id: str, kind: str, documents: list[Document], ids: list[str], embedding: Embeddings, profile: str) -> None: ...

    @abstractmethod
    # Exige eliminar IDs; la operación no necesita calcular embeddings.
    async def delete(self, agent_id: str, kind: str, ids: list[str], embedding: Embeddings | None, profile: str) -> None: ...

    @abstractmethod
    # Exige recuperar hasta k resultados y permite filtrar las fuentes válidas antes del ranking.
    async def retrieve(self, agent_id: str, kind: str, query: str, embedding: Embeddings, profile: str, k: int = 4, search_type: str = "similarity", source_ids: set[str] | None = None) -> list[dict]: ...


# Implementa índices persistentes compartidos de Chroma y un modo local para las pruebas.
class ChromaVectorStoreProvider(VectorStoreProvider):
    # Recibe conexión remota o una carpeta local de persistencia.
    def __init__(self, host: str = "chroma", port: int = 8000, persist_dir: str | Path | None = None):
        # Guarda el host del servidor Chroma; no es el host del modelo LLM.
        self.host = host
        # Guarda el puerto de Chroma.
        self.port = port
        # Selecciona persistencia local cuando el llamador proporciona una carpeta.
        self.persist_dir = str(persist_dir) if persist_dir is not None else None
        # Aplaza la conexión: construir el proveedor con una búsqueda vacía no abre Chroma.
        self._client = None
        # Los accesos mediante asyncio.to_thread comparten este candado de inicialización.
        self._client_lock = Lock()

    # Reutiliza el transporte Chroma sin repetir autenticación y validación del tenant por colección/lote.
    def _get_client(self):
        # El candado sólo protege la creación; las operaciones del cliente no quedan serializadas.
        with self._client_lock:
            # Una inicialización fallida deja None y permite que un trabajo posterior vuelva a intentarlo.
            if self._client is None:
                # Importa el SDK únicamente cuando se necesita una operación vectorial real.
                import chromadb
                # Mantiene el mismo modo persistente usado por las pruebas y el almacenamiento local.
                if self.persist_dir:
                    # El SDK abre una sola conexión persistente para las colecciones de este proveedor.
                    self._client = chromadb.PersistentClient(path=self.persist_dir)
                # El modo remoto reutiliza también su pool HTTP entre FAQ, documentos y lotes.
                else:
                    # Evita reconstruir HttpClient y sus validaciones de identidad, tenant y base de datos.
                    self._client = chromadb.HttpClient(host=self.host, port=self.port)
            # Entrega el cliente inicializado a cada adaptador de colección.
            return self._client

    # Construye el adaptador LangChain de una colección específica.
    def _store(self, agent_id: str, kind: str, embedding: Embeddings | None, profile: str):
        # Importa Chroma bajo demanda para no cargarlo cuando se utiliza FAISS.
        from langchain_chroma import Chroma

        # Asocia el proveedor de embeddings y define similitud coseno para esa colección.
        return Chroma(
            collection_name=collection_name(agent_id, kind, profile),
            # Usa la API pública client para compartir transporte sin modificar internals de LangChain.
            embedding_function=embedding, collection_metadata={"hnsw:space": "cosine"}, client=self._get_client(),
        )

    # Actualiza únicamente registros nuevos o diferentes para ahorrar embeddings al reindexar.
    async def upsert(self, agent_id: str, kind: str, documents: list[Document], ids: list[str], embedding: Embeddings, profile: str) -> None:
        # Valida los IDs antes de acceder al índice o al proveedor de embeddings.
        _validate(documents, ids)
        # Evita abrir una colección para un lote sin documentos.
        if not ids:
            # Termina sin llamadas externas ni escrituras.
            return
        # Conserva tipos simples y serializa metadatos complejos; añade aislamiento de agente y tipo.
        clean = [Document(page_content=doc.page_content, metadata={
            key: value if isinstance(value, (str, int, float, bool)) else json.dumps(value, ensure_ascii=False, default=str)
            for key, value in {**doc.metadata, "agent_id": str(agent_id), "knowledge_kind": kind}.items()
            if value is not None
        }) for doc in documents]
        # Abre la colección en un hilo porque la inicialización puede realizar E/S síncrona.
        store = await asyncio.to_thread(self._store, agent_id, kind, embedding, profile)
        # Consulta texto y metadatos persistidos sin llamar al modelo de embeddings.
        # Limita a 128 documentos tanto la comparación como cada escritura en Chroma.
        for offset in range(0, len(clean), 128):
            # Selecciona documentos e IDs correspondientes al mismo lote.
            batch, batch_ids = clean[offset:offset + 128], ids[offset:offset + 128]
            # Lee el contenido existente por ID; esta lectura no calcula embeddings.
            existing = await asyncio.to_thread(store.get, ids=batch_ids, include=["documents", "metadatas"])
            # Relaciona cada ID persistido con su texto y metadatos sin depender del orden de retorno.
            previous = {identifier: (content, metadata or {}) for identifier, content, metadata in zip(
                existing["ids"], existing["documents"], existing["metadatas"], strict=True,
            )}
            # Detecta diferencias exactas para omitir reindexaciones repetidas e idénticas.
            changed = [(doc, identifier) for doc, identifier in zip(batch, batch_ids, strict=True)
                       if previous.get(identifier) != (doc.page_content, doc.metadata)]
            # Sólo solicita embeddings cuando hay documentos o metadatos que actualizar.
            if changed:
                # LangChain calcula embeddings y hace upsert estable; cambios sólo de metadatos también se reembeben aquí.
                await store.aadd_documents(documents=[doc for doc, _ in changed], ids=[identifier for _, identifier in changed])

    # Elimina fragmentos conocidos sin consultar el modelo de embeddings.
    async def delete(self, agent_id: str, kind: str, ids: list[str], embedding: Embeddings | None, profile: str) -> None:
        # Omitir un lote vacío evita conexión y escritura innecesarias.
        if ids:
            # Abre la colección del perfil que realmente contiene los IDs a eliminar.
            store = await asyncio.to_thread(self._store, agent_id, kind, embedding, profile)
            # Solicita un borrado idempotente por IDs.
            await store.adelete(ids=ids)

    # Recupera candidatos válidos y evita embeber consultas contra índices vacíos.
    async def retrieve(self, agent_id: str, kind: str, query: str, embedding: Embeddings, profile: str, k: int = 4, search_type: str = "similarity", source_ids: set[str] | None = None) -> list[dict]:
        # Aplica los límites de k y las opciones de similarity o MMR.
        options = _search_options(k, search_type)
        # Un conjunto explícitamente vacío significa que no existe ninguna fuente permitida.
        if source_ids is not None and not source_ids:
            # Devuelve vacío antes de abrir el almacén o embeber la pregunta.
            return []
        # Abre la colección correspondiente al agente y al modelo actuales.
        store = await asyncio.to_thread(self._store, agent_id, kind, embedding, profile)
        # Traduce IDs de fuentes SQL válidas al filtro de metadatos de Chroma.
        where = {"source_id": {"$in": sorted(source_ids)}} if source_ids is not None else None
        # Un índice vacío o sin fuentes activas no necesita embeber la consulta.
        # Comprueba la existencia de un único ID válido sin traer contenido ni embeddings.
        available = await asyncio.to_thread(store.get, where=where, limit=1, include=[])
        # Detecta ausencia de documentos en el perfil o en las fuentes seleccionadas.
        if not available["ids"]:
            # Evita pagar por el embedding de una pregunta que no puede recuperar resultados.
            return []
        # Un filtro omitido mantiene el comportamiento público de búsqueda sin restricción de fuentes.
        if where is not None:
            # Aplica el filtro antes de seleccionar top-k; los borrados o pendientes no ocupan sus posiciones.
            options["filter"] = where
        # Construye el retriever con el modo y el filtro ya validados.
        retriever = store.as_retriever(search_type=search_type, search_kwargs=options)
        # Embeberá la consulta una vez y devolverá el texto de los fragmentos recuperados.
        return [_document_result(doc) for doc in await retriever.ainvoke(query)]


# Realiza búsqueda FAISS local usando vectores persistidos y un embedding de la consulta.
class _FAISSRetriever(BaseRetriever):
    """Retriever de LangChain con índice FAISS reconstruido y normalización coseno."""

    # Contiene únicamente los registros de la colección y fuentes autorizadas.
    records: dict[str, dict[str, Any]]
    # Recibe el modelo de embeddings o su caché limitada a la solicitud.
    embedding: Embeddings
    # Define cuatro resultados como valor predeterminado.
    k: int = 4
    # Usa similitud coseno salvo que se solicite diversidad mediante MMR.
    search_type: str = "similarity"

    # Ordena vectores localmente; no genera texto ni realiza llamadas a un LLM.
    def _search(self, query_vector: list[float]) -> list[Document]:
        # Carga la dependencia opcional FAISS sólo al buscar con este proveedor.
        import faiss
        # Carga operaciones matriciales y normalización de vectores.
        import numpy as np

        # Fija el orden que relaciona cada fila de la matriz con su ID persistido.
        identifiers = list(self.records)
        # Convierte los vectores guardados a una matriz float32 compatible con FAISS.
        vectors = np.asarray([self.records[item]["vector"] for item in identifiers], dtype=np.float32)
        # Construye una matriz de una fila con el embedding de la consulta.
        query = np.asarray([query_vector], dtype=np.float32)
        # Comprueba la dimensión y evita comparar vectores de espacios incompatibles.
        if vectors.ndim != 2 or query.ndim != 2 or vectors.shape[1] != query.shape[1]:
            # Informa que el perfil de embeddings no corresponde a los vectores guardados.
            raise ValueError("El perfil de embeddings tiene dimensiones incompatibles.")
        # Rechaza NaN e infinito antes de pasarlos a las operaciones numéricas.
        if not np.isfinite(vectors).all() or not np.isfinite(query).all():
            # Señala datos vectoriales inválidos que deben corregirse o reindexarse.
            raise ValueError("Los embeddings deben contener valores finitos.")
        # Normaliza documentos para convertir producto interno en similitud coseno, evitando división por cero.
        vectors = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        # Normaliza de la misma forma el vector de la consulta.
        query = query / np.maximum(np.linalg.norm(query, axis=1, keepdims=True), 1e-12)
        # Crea un índice exacto de producto interno en memoria; no recalcula embeddings persistidos.
        index = faiss.IndexFlatIP(vectors.shape[1])
        # Añade los vectores numéricos previamente normalizados al índice temporal.
        index.add(vectors)
        # Restringe candidatos al tamaño real de la colección y amplía la selección inicial para MMR.
        count = min(len(identifiers), max(20, self.k * 4) if self.search_type == "mmr" else self.k)
        # Obtiene posiciones de los vecinos más similares a la consulta.
        _, indices = index.search(query, count)
        # Convierte posiciones numéricas de la primera consulta a una lista de candidatos.
        candidates = indices[0].tolist()
        # Si se pidió MMR, reduce redundancia entre los resultados recuperados.
        if self.search_type == "mmr":
            # Extrae la matriz de los candidatos inicialmente más relevantes.
            candidate_vectors = vectors[candidates]
            # Calcula la relevancia de cada candidato respecto a la consulta mediante producto matricial.
            relevance = (candidate_vectors @ query.T).reshape(-1)
            # Selecciona primero el candidato de máxima relevancia.
            selected = [int(np.argmax(relevance))]
            # Completa como máximo k resultados distintos, o todos si hay menos candidatos.
            while len(selected) < min(self.k, len(candidates)):
                # Mide la mayor similitud de cada candidato con alguno de los ya seleccionados.
                redundancy = np.max(candidate_vectors @ candidate_vectors[selected].T, axis=1)
                # Equilibra relevancia y penalización de redundancia con pesos de 0.5.
                scores = 0.5 * relevance - 0.5 * redundancy
                # Impide seleccionar de nuevo candidatos ya elegidos.
                scores[selected] = -np.inf
                # Añade el siguiente candidato con mejor equilibrio entre relevancia y diversidad.
                selected.append(int(np.argmax(scores)))
            # Traduce los índices internos de MMR a posiciones del índice original.
            candidates = [candidates[item] for item in selected]
        # Reconstruye documentos con su ID, contenido y metadatos conservados.
        return [Document(id=identifiers[item], page_content=self.records[identifiers[item]]["content"],
                         metadata=self.records[identifiers[item]]["metadata"]) for item in candidates]

    # Implementa el contrato síncrono del retriever.
    def _get_relevant_documents(self, query: str) -> list[Document]:
        # Calcula o reutiliza el embedding de la consulta y busca localmente.
        return self._search(self.embedding.embed_query(query))

    # Implementa el contrato asíncrono del retriever.
    async def _aget_relevant_documents(self, query: str) -> list[Document]:
        # Solicita un único embedding; RequestEmbeddings puede compartirlo con la búsqueda de FAQ.
        vector = await self.embedding.aembed_query(query)
        # Ejecuta el cálculo FAISS en un hilo para no bloquear otras solicitudes.
        return await asyncio.to_thread(self._search, vector)


# Ofrece persistencia FAISS local pensada para un solo proceso y volúmenes moderados.
class FAISSVectorStoreProvider(VectorStoreProvider):
    """Almacén local opcional para un proceso; persiste vectores y documentos en JSON.

    Reconstruye el índice numérico en cada búsqueda sin cargar archivos pickle.
    Su coste de disco y CPU aumenta con la colección; Chroma es la opción compartida.
    """

    # Recibe el directorio permitido para los archivos JSON de las colecciones.
    def __init__(self, persist_dir: str | Path):
        # Resuelve la ruta base una sola vez para comprobar después sus archivos hijos.
        self.persist_dir = Path(persist_dir).resolve()

    # Obtiene el bloqueo compartido del archivo para coordinar proveedores del mismo proceso.
    def _lock(self, name: str) -> asyncio.Lock:
        # Los servicios crean proveedores por solicitud; el bloqueo pertenece al archivo
        # y se comparte entre instancias del mismo proceso para preservar las escrituras.
        # Reutiliza el bloqueo existente o crea uno cuando nadie está operando sobre esa colección.
        return _FAISS_LOCKS.setdefault(str(self._path(name)), asyncio.Lock())

    # Construye y valida la ruta final de una colección.
    def _path(self, name: str) -> Path:
        # Resuelve el nombre del índice debajo del directorio autorizado.
        target = (self.persist_dir / f"{name}.json").resolve()
        # Comprueba que el archivo no salga de la carpeta de índices.
        if target.parent != self.persist_dir:
            # Rechaza una ruta de colección insegura.
            raise ValueError("Ruta de almacenamiento inválida.")
        # Devuelve la ruta absoluta validada.
        return target

    # Carga los registros numéricos persistidos sin deserializar pickle ni ejecutar código.
    def _read(self, name: str) -> dict:
        # Valida la ruta antes de leer el archivo.
        path = self._path(name)
        # Decodifica JSON UTF-8 o devuelve una colección nueva si aún no existe.
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # Sustituye el archivo completo de forma atómica para evitar índices truncados.
    def _write(self, name: str, records: dict) -> None:
        # Crea la carpeta de persistencia cuando se utiliza por primera vez.
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        # Reserva un archivo temporal único en el mismo directorio para permitir reemplazo atómico.
        fd, temporary = tempfile.mkstemp(prefix="vectors-", suffix=".tmp", dir=self.persist_dir)
        # Garantiza que el temporal se limpie incluso si una escritura o reemplazo falla.
        try:
            # Abre el descriptor reservado como texto UTF-8 y garantiza su cierre.
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                # Serializa texto, metadatos y vectores rechazando NaN e infinito.
                json.dump(records, stream, ensure_ascii=False, allow_nan=False)
                # Vacía el búfer de Python antes de sincronizar el descriptor con el disco.
                stream.flush()
                # Solicita persistencia del contenido antes de reemplazar el índice vigente.
                os.fsync(stream.fileno())
            # Permite cinco intentos únicamente del reemplazo local, sin repetir llamadas a embeddings.
            for attempt in range(5):
                # Aísla los bloqueos transitorios que puede producir Windows al renombrar.
                try:
                    # Publica el nuevo JSON completo sin exponer una escritura parcial.
                    os.replace(temporary, self._path(name))
                    # Finaliza los intentos en cuanto el reemplazo atómico se completa.
                    break
                except PermissionError as exc:
                    # Antivirus e indexadores de Windows pueden retener brevemente el JSON
                    # recién cerrado. Se mantiene el reemplazo atómico y se reintenta
                    # sólo este bloqueo transitorio; no se repiten embeddings.
                    # Reintenta sólo errores transitorios conocidos de Windows mientras queden intentos.
                    if os.name != "nt" or exc.winerror not in {5, 32} or attempt == 4:
                        # Propaga cualquier otro error, o el agotamiento de reintentos, al worker.
                        raise
                    # Espera con retroceso exponencial breve; _write se ejecuta fuera del bucle asíncrono.
                    time.sleep(0.05 * (2 ** attempt))
        finally:
            # Comprueba si quedó un temporal después de un fallo.
            if os.path.exists(temporary):
                # Elimina exclusivamente el temporal creado por esta escritura.
                os.unlink(temporary)

    # Reutiliza embeddings por ID y texto dentro del mismo perfil persistido.
    async def upsert(self, agent_id: str, kind: str, documents: list[Document], ids: list[str], embedding: Embeddings, profile: str) -> None:
        # Rechaza lotes inconsistentes antes de consumir embeddings.
        _validate(documents, ids)
        # No abre ni escribe índices cuando no hay IDs.
        if not ids:
            # Termina sin actividad del proveedor.
            return
        # Selecciona la colección aislada por agente, tipo y perfil del modelo.
        name = collection_name(agent_id, kind, profile)
        # Serializa lectura, comparación y escritura para evitar actualizaciones perdidas o cálculos duplicados.
        async with self._lock(name):
            # Lee la versión vigente del JSON fuera del bucle asíncrono.
            records = await asyncio.to_thread(self._read, name)
            # Selecciona sólo los IDs nuevos o cuyo texto cambió; los metadatos no alteran el embedding.
            changed = [(doc, identifier) for doc, identifier in zip(documents, ids, strict=True)
                       if records.get(identifier, {}).get("content") != doc.page_content]
            # Envía únicamente esos textos al modelo y omite la llamada cuando todos coinciden.
            vectors = await embedding.aembed_documents([doc.page_content for doc, _ in changed]) if changed else []
            # Comprueba que el proveedor devolvió exactamente un vector por texto solicitado.
            if len(vectors) != len(changed):
                # Interrumpe el guardado de un lote incompleto para evitar asociaciones incorrectas.
                raise ValueError("El proveedor devolvió un número de vectores incorrecto.")
            # Relaciona cada nuevo vector con el ID del texto que lo produjo.
            replacements = {identifier: vector for (_, identifier), vector in zip(changed, vectors, strict=True)}
            # Actualiza todos los documentos del lote, incluso si sólo cambió su metadata.
            for doc, identifier in zip(documents, ids, strict=True):
                # Escoge el vector nuevo o reutiliza el persistido cuando el texto es idéntico.
                vector = replacements[identifier] if identifier in replacements else records[identifier]["vector"]
                # Guarda contenido, metadatos de aislamiento y vector bajo un ID estable.
                records[identifier] = {"content": doc.page_content, "metadata": {
                    **doc.metadata, "agent_id": str(agent_id), "knowledge_kind": kind,
                }, "vector": vector}
            # Publica el JSON completo mientras se mantiene el bloqueo de la colección.
            await asyncio.to_thread(self._write, name, records)

    # Elimina vectores locales por ID sin necesitar credenciales del modelo.
    async def delete(self, agent_id: str, kind: str, ids: list[str], embedding: Embeddings | None, profile: str) -> None:
        # Detecta borrados vacíos que no requieren disco.
        if not ids:
            # Finaliza inmediatamente si no hay IDs a borrar.
            return
        # Identifica el archivo correspondiente al perfil donde se crearon los vectores.
        name = collection_name(agent_id, kind, profile)
        # Evita que el borrado compita con un upsert de otra instancia.
        async with self._lock(name):
            # Carga los registros vigentes antes de aplicar el borrado.
            records = await asyncio.to_thread(self._read, name)
            # Recorre los IDs solicitados para retirar sus fragmentos.
            for identifier in ids:
                # Quita el ID si existe; repetir el borrado no genera un error.
                records.pop(identifier, None)
            # Persiste atómicamente los registros restantes.
            await asyncio.to_thread(self._write, name, records)

    # Recupera top-k tras excluir las fuentes no válidas, usando un único embedding de consulta.
    async def retrieve(self, agent_id: str, kind: str, query: str, embedding: Embeddings, profile: str, k: int = 4, search_type: str = "similarity", source_ids: set[str] | None = None) -> list[dict]:
        # Valida modo y límites incluso si la colección termina estando vacía.
        _search_options(k, search_type)
        # Un conjunto vacío de fuentes indica que no debe consultarse ningún documento.
        if source_ids is not None and not source_ids:
            # Evita leer el índice y llamar al proveedor de embeddings en ese caso.
            return []
        # Selecciona el perfil de embeddings y el agente de esta búsqueda.
        name = collection_name(agent_id, kind, profile)
        # Obtiene una instantánea consistente frente a escrituras locales concurrentes.
        async with self._lock(name):
            # Lee el archivo en un hilo y libera el bloqueo antes del cálculo numérico.
            records = await asyncio.to_thread(self._read, name)
        # Distingue búsqueda pública sin filtro de una selección explícita de fuentes válidas.
        if source_ids is not None:
            # Excluye pendientes, inactivos o eliminados antes de que compitan por top-k o MMR.
            records = {identifier: record for identifier, record in records.items()
                       if record["metadata"].get("source_id") in source_ids}
        # Detecta colecciones inexistentes o vacías después del filtrado.
        if not records:
            # No gasta un embedding de consulta si no hay resultados posibles.
            return []
        # Construye un retriever con sólo los registros válidos y las opciones elegidas.
        retriever = _FAISSRetriever(records=records, embedding=embedding, k=k, search_type=search_type)
        # Realiza la búsqueda y adapta documentos al formato de salida del servicio RAG.
        return [_document_result(doc) for doc in await retriever.ainvoke(query)]
