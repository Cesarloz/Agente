# Permite ejecutar lectura de archivos, DNS y chunking sin bloquear el servidor asíncrono.
import asyncio
# Importa y exporta preguntas frecuentes con el formato CSV estándar.
import csv
# Crea flujos de texto en memoria para procesar CSV sin archivos temporales.
import io
# Comprueba si una dirección IP resuelta es pública antes de descargar contenido.
import ipaddress
# Serializa el perfil de embeddings de forma estable.
import json
# Deduce el tipo MIME de archivos entregables según su nombre.
import mimetypes
# Resuelve nombres DNS para validar destinos de importación por URL.
import socket
# Maneja nombres, extensiones y rutas de archivos de forma explícita.
from pathlib import Path
# Separa esquema, dominio, puerto y credenciales de una URL recibida.
from urllib.parse import urlsplit
# Genera nombres internos únicos para evitar sobrescribir archivos con el mismo nombre original.
from uuid import uuid4

# Descarga contenido HTTP con tiempo máximo, streaming y límites de tamaño.
import httpx
# Extrae texto de HTML y elimina elementos que aportarían ruido al RAG.
from bs4 import BeautifulSoup
# Distingue el fragmento de LangChain del registro Document almacenado en SQL.
from langchain_core.documents import Document as LCDocument
# Captura errores de validación del contenido de FAQ.
from pydantic import ValidationError
# Consulta únicamente IDs válidos de SQL, sin cargar textos ni objetos completos en cada pregunta.
from sqlalchemy import select

# Obtiene la configuración del servidor para almacenamiento y proveedores.
from app.config import get_settings
# Proporciona errores controlados con mensaje y estado HTTP.
from app.errors import AppError
# Accede a agentes, documentos, FAQ, archivos y trabajos persistidos.
from app.models import Agent, Document, FAQ, File, Job
# Reutiliza acceso SQL con filtros de agente y serialización de respuestas.
from app.repositories import Repository, serialize
# Valida campos, tipos y límites de las preguntas frecuentes.
from app.schemas import FAQInput
# Obtiene las credenciales del modelo desde el almacén cifrado del servidor.
from app.services.credentials import CredentialService


# Identifica el espacio vectorial mediante proveedor y modelo de embeddings, independientemente del LLM.
def profile(config):
    # Ordena las claves JSON para que configuraciones equivalentes produzcan la misma colección.
    return json.dumps({"provider": config["embedding_provider"], "model": config["embedding_model"]}, sort_keys=True)


# Comparte selección del índice, caché por solicitud y recuperación para documentos y FAQ.
class KnowledgeService:
    # Inicializa el servicio con la sesión SQL de la solicitud o trabajo.
    def __init__(self, session):
        # Conserva la sesión que controla las consultas y transacciones del servicio.
        self.session = session
        # Crea una caché de adaptadores por perfil limitada a esta instancia, no compartida entre usuarios.
        self._embeddings = {}
        # Reutiliza el almacén y su conexión durante esta solicitud o trabajo de indexación.
        self._vectors = None

    # Selecciona dónde almacenar o consultar vectores según la configuración del servidor.
    def vectors(self):
        # Evita reconstruir el proveedor entre FAQ/documentos o entre lotes del mismo archivo.
        if self._vectors is not None:
            # La caché contiene el cliente del almacén, no respuestas ni conocimiento de otros usuarios.
            return self._vectors
        # Carga los adaptadores bajo demanda para mantener independientes Chroma y FAISS.
        from app.providers import ChromaVectorStoreProvider, FAISSVectorStoreProvider
        # Lee proveedor, rutas y dirección del almacén vectorial configurado.
        settings = get_settings()
        # El modo FAISS utiliza archivos locales por colección.
        if settings.vector_provider == "faiss":
            # Conserva el adaptador local bajo la carpeta de almacenamiento autorizada.
            self._vectors = FAISSVectorStoreProvider(str(settings.storage_dir / "faiss"))
        # Chroma comparte el cliente entre las colecciones de esta instancia del servicio.
        elif settings.vector_provider == "chroma":
            # Conserva el adaptador; su conexión remota se abrirá sólo al realizar una operación.
            self._vectors = ChromaVectorStoreProvider(settings.chroma_host, settings.chroma_port)
        # Rechaza nombres desconocidos en lugar de escoger silenciosamente otro almacén.
        else:
            # Informa de una selección de proveedor vectorial no válida.
            raise AppError("Vector provider no válido")
        # Devuelve la misma instancia mientras dure esta solicitud o trabajo.
        return self._vectors

    # Construye o reutiliza el modelo que transforma texto en vectores numéricos.
    async def embedding(self, index_profile):
        # Importa las implementaciones de embeddings de OpenAI y Gemini.
        from app.providers import GeminiEmbeddingProvider, OpenAIEmbeddingProvider
        # Importa la caché que comparte un embedding de consulta entre FAQ y documentos de la misma solicitud.
        from app.providers.cached_embeddings import RequestEmbeddings
        # Comprueba si este perfil ya tiene un adaptador en la instancia.
        if index_profile in self._embeddings:
            # Reutiliza adaptador y consultas calculadas para no repetir trabajo o gasto.
            return self._embeddings[index_profile]
        # Decodifica proveedor y nombre del modelo que generó la colección.
        settings = json.loads(index_profile)
        # Obtiene la clave correspondiente al proveedor sin incluirla en textos ni resultados.
        key = await CredentialService(self.session).get(f"provider:{settings['provider']}")
        # Selecciona la implementación correspondiente a la configuración validada del agente.
        factory = OpenAIEmbeddingProvider if settings["provider"] == "openai" else GeminiEmbeddingProvider
        # Construye el cliente de embeddings y lo envuelve con caché limitada a esta solicitud.
        result = RequestEmbeddings(factory().build(key, settings["model"]))
        # Conserva el adaptador bajo su perfil para no mezclar modelos ni dimensiones.
        self._embeddings[index_profile] = result
        # Devuelve el proveedor; el cálculo real ocurre al indexar textos o recuperar por pregunta.
        return result

    # Registra un trabajo durable para que el worker procese el conocimiento fuera de la petición HTTP.
    async def enqueue(self, kind, agent_id, id):
        # Guarda tipo, agente e ID; el método llamador confirma la transacción junto con el registro de origen.
        await Repository(self.session, Job).add(kind=kind, payload={"agent_id": agent_id, "id": id})

    # Recupera conocimiento indexado que sigue siendo válido en la base SQL.
    async def retrieve(self, agent_id, kind, query, config):
        # Selecciona la tabla de FAQ o documentos según la búsqueda solicitada.
        model = FAQ if kind == "faq" else Document
        # Usa el perfil actual para impedir búsquedas sobre vectores de otro modelo.
        p = profile(config)
        # Excluye FAQ desactivadas y documentos cuya eliminación ya fue solicitada.
        enabled = FAQ.active.is_(True) if kind == "faq" else Document.delete_requested.is_(False)
        # Lee sólo IDs del agente con estado INDEXED, perfil compatible y condición activa.
        valid = set(await self.session.scalars(select(model.id).where(
            model.agent_id == agent_id, model.status == "INDEXED", model.index_profile == p, enabled,
        )))
        # Sin fuentes válidas no hay nada que consultar en el almacén vectorial.
        if not valid:
            # Devuelve vacío sin leer credenciales ni gastar embeddings de consulta.
            return []
        # Pasa el filtro de fuentes antes de top-k/MMR y comparte el adaptador de embeddings del perfil.
        result = await self.vectors().retrieve(agent_id, kind, query, await self.embedding(p), p, k=config["retriever_k"], search_type=config["retriever_type"], source_ids=valid)
        # Mantiene una segunda comprobación de pertenencia de las fuentes devueltas por el proveedor.
        result = [r for r in result if r.get("metadata", {}).get("source_id") in valid]
        # Las FAQ usan además la prioridad administrativa dentro de los candidatos recuperados.
        if kind == "faq":
            # Ordena prioridad descendente conservando el orden de relevancia entre prioridades iguales.
            result.sort(key=lambda r: r.get("metadata", {}).get("priority", 0), reverse=True)
        # Devuelve los fragmentos que el runtime podrá introducir en el contexto del modelo.
        return result


# Administra las FAQ y su proyección vectorial sin generar respuestas con un LLM.
class FAQService(KnowledgeService):
    # Lista las preguntas frecuentes del agente para administración.
    async def list(self, agent_id):
        # Comprueba que el agente existe antes de consultar sus FAQ.
        await Repository(self.session, Agent).get(agent_id)
        # Serializa los registros del agente para devolver campos aptos para la API.
        return [serialize(r) for r in await Repository(self.session, FAQ).list(FAQ.agent_id == agent_id)]

    # Crea o actualiza una FAQ y encola indexación sólo si el estado requiere una actualización.
    async def save(self, agent_id, values, id=None, commit=True):
        # Carga el agente para validar existencia y comparar el perfil de embeddings actual.
        agent = await Repository(self.session, Agent).get(agent_id)
        # Al editar, obtiene la FAQ de ese agente con bloqueo; al crear no existe una fila previa.
        row = await Repository(self.session, FAQ).get(id, agent_id, lock=True) if id else None
        # Convierte cambios parciales en una FAQ completa antes de modificar la base de datos.
        try:
            # Conserva los campos actuales cuando el llamador sólo cambia parte de la FAQ.
            base = {k: getattr(row, k) for k in FAQInput.model_fields} if row else {}
            # Combina cambios y valores previos y valida el resultado con el esquema FAQInput.
            data = FAQInput.model_validate({**base, **values})
        except ValidationError:
            # Devuelve un error 422 legible sin exponer detalles internos de la validación.
            raise AppError("FAQ inválida: pregunta y respuesta son obligatorias", 422) from None
        # Detecta un guardado idéntico ya proyectado con el perfil vigente para no encolar ni ocultar la FAQ.
        if row and data.model_dump() == base and row.status in {"INDEXED", "INACTIVE"} and row.index_profile == profile(agent.config):
            # Respeta si la operación individual es responsable de confirmar la transacción.
            if commit:
                # Libera la transacción y el bloqueo aunque no haya cambios que indexar.
                await self.session.commit()
            # Devuelve la FAQ existente preservando su estado INDEXED o INACTIVE.
            return serialize(row)
        # Si se asocia un archivo, comprueba que pertenece al mismo agente y puede entregarse.
        if data.file_id:
            # Carga el archivo por ID y agente para evitar referencias cruzadas.
            file = await Repository(self.session, File).get(data.file_id, agent_id)
            # Permite únicamente archivos registrados como entregables.
            if file.kind != "DELIVERABLE":
                # Rechaza adjuntos de conversación u otros tipos no aptos para una FAQ.
                raise AppError("La FAQ solo puede asociar archivos entregables")
        # Distingue una edición de la creación de un nuevo registro.
        if row:
            # Recorre los campos ya validados por el esquema de entrada.
            for key, value in data.model_dump().items():
                # Aplica cada valor al registro existente dentro de la transacción.
                setattr(row, key, value)
        else:
            # Crea una nueva FAQ vinculada al agente con todos sus campos validados.
            row = await Repository(self.session, FAQ).add(agent_id=agent_id, **data.model_dump())
        # Marca que la proyección requiere actualizarse y limpia un error previo.
        row.status, row.error = "PENDING", None
        # Encola el trabajo que sincronizará la FAQ con el índice vectorial.
        await self.enqueue("faq", agent_id, row.id)
        # Permite agrupar varias FAQ de una importación en una única transacción.
        if commit:
            # Confirma conjuntamente datos y trabajo de indexación cuando corresponde.
            await self.session.commit()
        # Devuelve el registro guardado sin esperar al cálculo externo de embeddings.
        return serialize(row)

    # Solicita eliminar una FAQ y la retira inmediatamente de las fuentes recuperables.
    async def delete(self, agent_id, id):
        # Carga y bloquea la FAQ comprobando a qué agente pertenece.
        row = await Repository(self.session, FAQ).get(id, agent_id, lock=True)
        # Se excluye de inmediato; el worker borrará sus vectores antes de eliminar la fila.
        # Desactiva la FAQ antes del borrado físico para que no reaparezca en el RAG.
        row.active, row.status = False, "DELETING"
        # Encola la eliminación de su vector y del registro SQL.
        await self.enqueue("delete_faq", agent_id, id)
        # Hace persistente la exclusión inmediata y el trabajo de borrado.
        await self.session.commit()
        # Informa que la eliminación se completará de forma asíncrona.
        return {"status": "DELETING"}

    # Proyecta una FAQ en el índice; el worker llama este método después de guardarla.
    async def index(self, agent_id, id):
        # Obtiene la versión vigente de la FAQ con bloqueo transaccional.
        row = await Repository(self.session, FAQ).get(id, agent_id, lock=True)
        # Una FAQ que ya se está eliminando no debe volver a indexarse.
        if row.status == "DELETING":
            # Descarta el trabajo obsoleto sin calcular embeddings.
            return
        # Carga la configuración actual del agente.
        agent = await Repository(self.session, Agent).get(agent_id)
        # Determina la colección que corresponde a su modelo de embeddings.
        p = profile(agent.config)
        # Detecta si existe una proyección creada con un perfil anterior.
        if row.index_profile and row.index_profile != p:
            # Elimina esa proyección sin generar embeddings para el modelo antiguo.
            await self.vectors().delete(agent_id, "faq", [row.id], None, row.index_profile)
        # Sólo las FAQ activas necesitan estar disponibles en el índice.
        if row.active:
            # Construye texto pregunta/respuesta y metadatos de prioridad, fuente, etiquetas y adjunto autorizado.
            doc = LCDocument(page_content=f"Pregunta: {row.question}\nRespuesta: {row.answer}", metadata={"source_id": row.id, "source": row.question, "answer": row.answer, "priority": row.priority, "file_id": row.file_id or "", "tags": ",".join(row.tags)})
            # Inserta o reemplaza el ID estable; el proveedor omite cálculos si el contenido persistido coincide.
            await self.vectors().upsert(agent_id, "faq", [doc], [row.id], await self.embedding(p), p)
            # Marca la FAQ como recuperable únicamente después de un upsert correcto.
            row.status = "INDEXED"
        else:
            # Al desactivar, comprueba si había un índice del cual retirar el vector.
            if row.index_profile:
                # Elimina el vector existente sin consultar un modelo de embeddings.
                await self.vectors().delete(agent_id, "faq", [row.id], None, row.index_profile)
            # Registra que la FAQ permanece almacenada pero no participa en recuperación.
            row.status = "INACTIVE"
        # Guarda el perfil sincronizado y limpia errores anteriores de indexación.
        row.index_profile, row.error = p, None
        # Confirma la proyección de estado después de que el almacén vectorial respondió correctamente.
        await self.session.commit()

    # Importa un CSV validando todo su contenido antes de guardar sus FAQ.
    async def import_csv(self, agent_id, raw):
        # Agrupa errores de formato, codificación y campos para devolver un mensaje consistente.
        try:
            # Lee UTF-8 con BOM opcional y usa los encabezados como nombres de campos.
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
            # Materializa las filas para comprobar el tamaño y validar el conjunto.
            rows = list(reader)
            # Impide importaciones vacías y lotes mayores de 5000 FAQ.
            if not rows or len(rows) > 5000:
                # Redirige ese fallo al error de CSV controlado.
                raise ValueError()
            # Acumula objetos validados antes de modificar la base de datos.
            validated = []
            # Recorre todas las filas de la importación.
            for r in rows:
                # DictReader usa clave None para celdas sobrantes y valores None para
                # celdas finales omitidas: rechaza extras y completa opcionales.
                # DictReader usa una clave None cuando sobran celdas frente al encabezado.
                if None in r:
                    # Rechaza filas con columnas extras en vez de ignorar contenido ambiguo.
                    raise ValueError()
                # Convierte celdas ausentes o vacías en cadenas para aplicar valores opcionales por defecto.
                r = {k: v or "" for k, v in r.items()}
                # La protección aplicada al exportar puede revertirse al importar el CSV propio.
                # Revierte el prefijo protector de fórmulas cuando proviene de una exportación propia.
                clean = {k: v[1:] if v and v.startswith("'") and len(v) > 1 and v[1] in "=+-@" else v for k, v in r.items()}
                # Valida pregunta/respuesta y convierte etiquetas, prioridad, activo y archivo a sus tipos de entrada.
                validated.append(FAQInput(question=clean["question"], answer=clean["answer"], tags=[t.strip() for t in clean.get("tags", "").split("|") if t.strip()], priority=int(clean.get("priority") or 0), active=(clean.get("active") or "true").lower() in {"true", "1", "sí", "si"}, file_id=clean.get("file_id") or None))
        except (ValueError, KeyError, UnicodeError, TypeError, csv.Error):
            # Explica el esquema requerido sin guardar parcialmente una importación inválida.
            raise AppError("CSV inválido. Usa columnas question, answer, tags, priority, file_id, active; tags separadas con |.") from None
        # Recorre las FAQ sólo después de que todas las filas han pasado validación.
        for r in validated:
            # Guarda cada FAQ y su trabajo sin confirmar aún la transacción global.
            await self.save(agent_id, r.model_dump(), commit=False)
        # Confirma todas las FAQ importadas y sus trabajos de forma conjunta.
        await self.session.commit()
        # Devuelve el número de registros importados.
        return {"imported": len(validated)}

    # Exporta las FAQ del agente en un formato compatible con la importación.
    async def export_csv(self, agent_id):
        # Reutiliza el listado y sus comprobaciones de pertenencia al agente.
        rows = await self.list(agent_id)
        # Crea un flujo de texto CSV con control explícito de saltos de línea.
        stream = io.StringIO(newline="")
        # Define el orden estable de columnas y los campos exportados.
        writer = csv.DictWriter(stream, fieldnames=["question", "answer", "tags", "priority", "file_id", "active"])
        # Escribe el encabezado que import_csv espera recibir.
        writer.writeheader()
        # Convierte cada registro al formato tabular.
        for row in rows:
            # Une etiquetas mediante el separador reversible de la importación.
            row["tags"] = "|".join(row["tags"])
            # Selecciona únicamente los campos públicos definidos en las columnas.
            values = {key: row[key] for key in writer.fieldnames}
            # Revisa cada celda de texto antes de escribirla en el CSV.
            for key, value in values.items():
                # Detecta prefijos que una hoja de cálculo podría interpretar como fórmulas o controles.
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    # Antecede una comilla simple para que el programa de hojas de cálculo trate la celda como texto.
                    values[key] = "'" + value
            # Escribe la fila escapando correctamente comas, comillas y saltos de línea.
            writer.writerow(values)
        # Devuelve bytes UTF-8 con BOM para facilitar la lectura de acentos en hojas de cálculo.
        return stream.getvalue().encode("utf-8-sig")


# Administra el almacenamiento local de archivos y adjuntos relacionados con el conocimiento.
class FileService:
    # Recibe la sesión SQL de la operación actual.
    def __init__(self, session):
        # Conserva la sesión para consultar pertenencia y guardar registros de archivos.
        self.session = session

    # Valida y guarda el binario en una carpeta del agente sin indexarlo todavía.
    async def store(self, agent_id, name, content, kind):
        # Rechaza un agente inexistente antes de crear archivos.
        await Repository(self.session, Agent).get(agent_id)
        # Lee directorio de almacenamiento y límite de tamaño de subida.
        settings = get_settings()
        # Exige contenido no vacío y un tamaño dentro del máximo del servidor.
        if not content or len(content) > settings.upload_max_bytes:
            # Devuelve un error de tamaño antes de escribir el archivo.
            raise AppError("Archivo vacío o mayor al límite permitido", 413)
        # Elimina directorios del nombre recibido y limita su longitud visible a 255 caracteres.
        safe_name = Path(name.replace("\\", "/")).name[:255]
        # Normaliza la extensión usada para elegir el parser de conocimiento.
        suffix = Path(safe_name).suffix.lower()
        # Limita archivos de conocimiento a los formatos que LoaderPipeline sabe extraer.
        if kind == "knowledge" and suffix not in {".pdf", ".docx", ".txt", ".md"}:
            # Informa del formato no admitido sin persistir contenido no procesable.
            raise AppError("Formato no admitido; usa PDF, DOCX, TXT o MD")
        # Separa almacenamiento por clase de archivo y agente.
        folder = settings.storage_dir.resolve() / kind / agent_id
        # Crea la carpeta sin bloquear el bucle asíncrono.
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        # Usa un UUID interno conservando la extensión para evitar colisiones de nombres.
        path = folder / f"{uuid4().hex}{suffix}"
        # Escribe el contenido en disco en un hilo de trabajo.
        await asyncio.to_thread(path.write_bytes, content)
        # Devuelve el nombre visible limpio y la ruta interna para el registro SQL.
        return safe_name, str(path)

    # Registra un entregable o archivo de conversación; documentos RAG usan DocumentService.upload.
    async def upload(self, agent_id, name, content, kind="DELIVERABLE", conversation_id=None):
        # Comprueba los tipos de archivo admitidos por esta operación.
        if kind not in {"DELIVERABLE", "CONVERSATION"}:
            # Informa de un tipo inválido antes de almacenar el binario.
            raise AppError("Tipo de archivo inválido")
        # Reutiliza comprobaciones de agente, tamaño y ruta del almacenamiento local.
        safe, path = await self.store(agent_id, name, content, kind.lower())
        # Guarda metadatos, asociación opcional a conversación y tipo MIME del archivo.
        row = await Repository(self.session, File).add(agent_id=agent_id, name=safe, path=path, kind=kind, conversation_id=conversation_id, mime_type=mimetypes.guess_type(safe)[0] or "application/octet-stream")
        # Confirma el registro del archivo para que pueda consultarse o descargarse.
        await self.session.commit()
        # Devuelve datos públicos ocultando la ruta del servidor.
        return serialize(row, exclude=("path",))

    # Lista entregables disponibles para asociarlos a FAQ del agente.
    async def list(self, agent_id):
        # Filtra por agente y tipo y excluye la ruta interna de cada archivo.
        return [serialize(r, exclude=("path",)) for r in await Repository(self.session, File).list(File.agent_id == agent_id, File.kind == "DELIVERABLE")]

    # Autoriza y prepara la descarga de un archivo concreto del agente.
    async def download(self, agent_id, id):
        # Carga el registro comprobando que pertenece al agente solicitado.
        row = await Repository(self.session, File).get(id, agent_id)
        # Verifica que el binario existe dentro de la carpeta de almacenamiento.
        self.checked_path(row.path)
        # Devuelve el registro al endpoint que enviará el archivo.
        return row

    @staticmethod
    # Valida rutas antes de leer o eliminar archivos del almacén local.
    def checked_path(path):
        # Resuelve enlaces y componentes relativos para comparar una ruta absoluta real.
        value = Path(path).resolve()
        # Exige que el archivo esté bajo storage_dir y sea un archivo existente.
        if not value.is_relative_to(get_settings().storage_dir.resolve()) or not value.is_file():
            # Devuelve 404 si el archivo falta o la ruta queda fuera del almacén autorizado.
            raise AppError("Archivo no disponible", 404)
        # Entrega la ruta validada al llamador.
        return value

    # Elimina un entregable y actualiza las FAQ que lo referencian.
    async def delete(self, agent_id, id):
        # Obtiene y bloquea el archivo comprobando su pertenencia al agente.
        row = await Repository(self.session, File).get(id, agent_id, lock=True)
        # Encuentra las FAQ del mismo agente cuyo adjunto es el archivo que se elimina.
        for faq in await Repository(self.session, FAQ).list(FAQ.agent_id == agent_id, FAQ.file_id == id):
            # Quita la referencia y marca que sus metadatos vectoriales deben actualizarse.
            faq.file_id, faq.status = None, "PENDING"
            # Encola la actualización de cada FAQ afectada.
            await KnowledgeService(self.session).enqueue("faq", agent_id, faq.id)
        # Valida la ruta antes de programar el borrado físico.
        path = self.checked_path(row.path)
        # Elimina el registro SQL del archivo dentro de la transacción.
        await self.session.delete(row)
        # Confirma la eliminación y las actualizaciones de FAQ.
        await self.session.commit()
        # Borra el binario en un hilo; tolera que otro proceso ya lo haya eliminado.
        await asyncio.to_thread(path.unlink, missing_ok=True)
        # Confirma que la eliminación terminó.
        return {"deleted": True}


# Gestiona carga, extracción, fragmentación, indexación y eliminación del conocimiento documental.
class DocumentService(KnowledgeService):
    # Lista el estado de los documentos para la administración del agente.
    async def list(self, agent_id):
        # Devuelve metadatos públicos sin rutas locales ni la lista interna de IDs de fragmento.
        return [serialize(r, exclude=("path", "chunk_ids")) for r in await Repository(self.session, Document).list(Document.agent_id == agent_id)]

    # Recibe el binario de conocimiento y encola su procesamiento posterior.
    async def upload(self, agent_id, name, content, source_url=None):
        # Valida y guarda el archivo bajo la carpeta knowledge del agente.
        safe, path = await FileService(self.session).store(agent_id, name, content, "knowledge")
        # Registra nombre, ruta interna y URL pública de origen si la carga procede de una web.
        row = await Repository(self.session, Document).add(agent_id=agent_id, name=safe, path=path, source_url=source_url)
        # Encola la indexación sin calcular embeddings durante la subida HTTP.
        await self.enqueue("document", agent_id, row.id)
        # Confirma conjuntamente el documento recibido y el trabajo durable.
        await self.session.commit()
        # Devuelve el estado inicial y datos públicos del documento.
        return serialize(row, exclude=("path", "chunk_ids"))

    # Importa contenido de una URL HTTPS previamente autorizada en la configuración del servidor.
    async def upload_url(self, agent_id, url):
        # Separa componentes de URL para validarlos antes de acceder a la red.
        parsed = urlsplit(url)
        # Normaliza la lista explícita de dominios permitidos, omitiendo entradas vacías.
        hosts = {h.strip().lower() for h in get_settings().url_allowed_hosts.split(",") if h.strip()}
        # Exige dominio exacto permitido, HTTPS, puerto 443 y ausencia de credenciales en la URL.
        if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
            # Rechaza destinos fuera de la configuración aprobada.
            raise AppError("URL no autorizada. Agrega el dominio HTTPS exacto a URL_ALLOWED_HOSTS en el servidor.")
        # Controla errores de red y resolución DNS durante la descarga.
        try:
            # Resuelve DNS fuera del bucle asíncrono para comprobar las direcciones devueltas.
            addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM)
            # Rechaza cualquier dirección no pública entre las respuestas DNS.
            if any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
                # Impide importar directamente contenido desde direcciones locales o privadas.
                raise AppError("La URL debe resolver a una dirección pública")
            # Fija 30 segundos, desactiva redirecciones y evita heredar proxies del entorno.
            async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
                # Descarga en streaming para aplicar el límite antes de cargar toda la respuesta.
                async with client.stream("GET", url) as response:
                    # Propaga estados de error HTTP hacia el manejo de descarga fallida.
                    response.raise_for_status()
                    # Exige respuesta 200 directa y no acepta destinos alternativos por redirección.
                    if response.status_code != 200:
                        # Informa que la URL debe responder con contenido directamente.
                        raise AppError("La URL debe responder directamente sin redirecciones")
                    # Inicializa el acumulador de bytes y el contador de tamaño descargado.
                    chunks, size = [], 0
                    # Procesa los bloques que HTTPX entrega progresivamente.
                    async for chunk in response.aiter_bytes():
                        # Cuenta los bytes efectivamente recibidos antes de añadir otro bloque.
                        size += len(chunk)
                        # Interrumpe la descarga si supera el límite permitido de subida.
                        if size > get_settings().upload_max_bytes:
                            # Devuelve el error de tamaño de la importación web.
                            raise AppError("La URL excede el tamaño permitido", 413)
                        # Conserva el bloque después de verificar que cabe en el límite.
                        chunks.append(chunk)
                    # Une los bloques aceptados en el binario que se almacenará.
                    content = b"".join(chunks)
                    # Lee el tipo de contenido para decidir si debe limpiar HTML.
                    content_type = response.headers.get("content-type", "")
            # Usa el nombre de la ruta o un nombre de texto predeterminado.
            name = Path(parsed.path).name or "pagina.txt"
            # Las páginas HTML requieren extracción de texto antes de pasar al loader.
            if "html" in content_type:
                # Analiza HTML como datos sin ejecutar JavaScript ni estilos.
                soup = BeautifulSoup(content, "html.parser")
                # Selecciona scripts, estilos y secciones repetitivas de navegación o pie de página.
                for tag in soup(["script", "style", "nav", "footer"]):
                    # Retira esos elementos para reducir ruido y texto innecesario en los embeddings.
                    tag.decompose()
                # Extrae texto limpio UTF-8 y usa un nombre TXT derivado del dominio.
                content, name = soup.get_text("\n", strip=True).encode(), f"{parsed.hostname}.txt"
            # Reutiliza la carga documental y conserva la URL original para citar la fuente.
            return await self.upload(agent_id, name, content, url)
        except (httpx.HTTPError, OSError):
            # Devuelve un error controlado de descarga sin exponer detalles internos de red.
            raise AppError("No se pudo descargar la URL", 502) from None

    # Solicita reconstruir la proyección de un documento desde su archivo de origen.
    async def reindex(self, agent_id, id):
        # Carga el documento del agente con bloqueo durante el cambio de estado.
        row = await Repository(self.session, Document).get(id, agent_id, lock=True)
        # Una eliminación solicitada tiene prioridad sobre reconstruir el índice.
        if row.delete_requested:
            # Devuelve conflicto en lugar de volver a encolar un documento que será eliminado.
            raise AppError("El documento está en proceso de eliminación", 409)
        # Marca el trabajo pendiente y limpia errores de una ejecución anterior.
        row.status, row.error = "PENDING", None
        # Encola una nueva proyección que reutilizará vectores idénticos cuando sea posible.
        await self.enqueue("document", agent_id, id)
        # Confirma estado y trabajo en la misma transacción.
        await self.session.commit()
        # Informa que la reconstrucción se ejecutará de forma asíncrona.
        return {"status": "PENDING"}

    # Ejecuta en el worker el flujo archivo → texto → fragmentos → embeddings → índice.
    async def index(self, agent_id, id):
        # Carga la extracción/división sólo cuando hay un documento que procesar.
        from app.providers import LoaderPipeline
        # Obtiene el documento y valida que pertenece al agente del trabajo.
        row = await Repository(self.session, Document).get(id, agent_id, lock=True)
        # No inicia trabajo de embeddings si ya se solicitó eliminar el documento.
        if row.delete_requested:
            # Termina el trabajo obsoleto sin procesar el archivo.
            return
        # Carga la configuración del agente vigente al inicio de esta ejecución.
        agent = await Repository(self.session, Agent).get(agent_id)
        # Conserva parámetros de chunking y perfil de embeddings de esta proyección.
        config, p = agent.config, profile(agent.config)
        # Publica la fase de extracción; el documento queda fuera de recuperación hasta completarse.
        row.status = "PROCESSING"
        # Confirma el progreso y libera el bloqueo antes del trabajo de disco prolongado.
        await self.session.commit()
        # Valida que la ruta siga dentro del almacenamiento y que el archivo exista.
        path = FileService.checked_path(row.path)
        # Extrae y normaliza texto; aún no hay llamadas a modelos ni gasto de embeddings.
        docs = await LoaderPipeline().load(str(path), path.suffix)
        # Publica que comienza la división del texto en fragmentos.
        row.status = "CHUNKING"
        # Hace visible la nueva fase de progreso en la administración.
        await self.session.commit()
        # Divide en un hilo usando tamaño y solapamiento configurados, ambos medidos en caracteres.
        chunks = await asyncio.to_thread(LoaderPipeline().split, docs, config["chunk_size"], config["chunk_overlap"])
        # Detecta archivos vacíos de texto tras normalizar o PDF escaneados sin extracción posible.
        if not chunks:
            # Evita enviar fragmentos vacíos al modelo y explica la necesidad de OCR previo.
            raise AppError("El documento no contiene texto extraíble. Los PDF escaneados requieren OCR previo.")
        # Limita la cantidad total de fragmentos antes de incurrir en coste de embeddings.
        if len(chunks) > 20000:
            # Rechaza documentos que producirían más de 20000 fragmentos.
            raise AppError("El documento excede 20 000 fragmentos")
        # Publica la fase de sincronización vectorial.
        row.status = "EMBEDDING"
        # Confirma la fase antes de la comunicación con el proveedor de embeddings.
        await self.session.commit()
        # Asigna IDs estables por documento y posición para comparar o reemplazar al reindexar.
        ids = [f"{id}:{i}" for i in range(len(chunks))]
        # Prepara los metadatos de cada fragmento antes de persistirlos.
        for chunk in chunks:
            # Conserva metadatos escalares, fija ID de origen y sustituye la ruta interna por fuente pública.
            chunk.metadata = {**{k: v for k, v in chunk.metadata.items() if isinstance(v, (str, int, float, bool))}, "source_id": id, "source": row.source_url or row.name}
        # Si hubo una proyección previa, determina qué vectores ya no deben existir.
        if row.index_profile and row.chunk_ids:
            # Conserva IDs vigentes del mismo modelo para reutilizar embeddings idénticos.
            # Al cambiar modelo borra todos los anteriores; en el mismo perfil sólo retira IDs sobrantes.
            obsolete = row.chunk_ids if row.index_profile != p else sorted(set(row.chunk_ids) - set(ids))
            # Evita llamadas de borrado cuando todos los IDs anteriores siguen vigentes.
            if obsolete:
                # Elimina únicamente esos IDs del perfil anterior sin calcular embeddings.
                await self.vectors().delete(agent_id, "documents", obsolete, None, row.index_profile)
        # Obtiene el adaptador del modelo de embeddings para todos los lotes del documento.
        embedding = await self.embedding(p)
        # Conserva los IDs previstos para limpiar lotes parciales si el proveedor falla.
        # Persiste todos los IDs previstos antes de escribir para poder limpiar una indexación parcial fallida.
        row.chunk_ids, row.index_profile, row.chunk_count = ids, p, 0
        # Confirma la información de limpieza antes del primer lote externo.
        await self.session.commit()
        # Procesa 100 fragmentos por lote para acotar memoria y tamaño de cada operación.
        for start in range(0, len(chunks), 100):
            # Sincroniza cada lote; los proveedores reutilizan registros idénticos ya persistidos.
            await self.vectors().upsert(agent_id, "documents", chunks[start:start+100], ids[start:start+100], embedding, p)
        # Recarga el estado después del trabajo externo: puede haberse solicitado la eliminación.
        # Recarga el documento con bloqueo por si se pidió eliminarlo durante el trabajo externo.
        await self.session.refresh(row, with_for_update=True)
        # Consulta la intención de borrado más reciente confirmada en SQL.
        deleting = row.delete_requested
        # Respeta el borrado concurrente; si no existe, publica el documento como recuperable.
        row.status = "DELETING" if deleting else "INDEXED"
        # Guarda número de fragmentos, IDs, perfil realmente usado y ausencia de error.
        row.chunk_count, row.chunk_ids, row.index_profile, row.error = len(chunks), ids, p, None
        # Confirma la proyección completada para que SQL autorice su recuperación.
        await self.session.commit()

    # Solicita retirar un documento del RAG y eliminarlo mediante el worker.
    async def delete(self, agent_id, id):
        # Obtiene y bloquea el registro del agente para cambiar su estado.
        row = await Repository(self.session, Document).get(id, agent_id, lock=True)
        # Lo excluye inmediatamente de recuperación aunque sus vectores aún existan.
        row.status, row.delete_requested = "DELETING", True
        # Encola el borrado de todos sus fragmentos y de su archivo.
        await self.enqueue("delete_document", agent_id, id)
        # Confirma la exclusión y el trabajo de eliminación juntos.
        await self.session.commit()
        # Informa que el borrado físico terminará de forma asíncrona.
        return {"status": "DELETING"}

    # Elimina definitivamente una FAQ o documento y limpia incluso IDs de indexaciones parciales.
    async def purge(self, agent_id, id, is_faq=False):
        # Escoge la tabla de origen correspondiente al tipo de trabajo de borrado.
        model = FAQ if is_faq else Document
        # Obtiene el registro por ID y agente con bloqueo para mantener la operación ordenada.
        row = await Repository(self.session, model).get(id, agent_id, lock=True)
        # Usa un ID de vector para FAQ o toda la lista de fragmentos del documento.
        ids = [row.id] if is_faq else row.chunk_ids
        # Sólo intenta borrar vectores cuando conoce su perfil y al menos un ID.
        if row.index_profile and ids:
            # Elimina los vectores del perfil donde se crearon sin necesitar embeddings nuevos.
            await self.vectors().delete(agent_id, "faq" if is_faq else "documents", ids, None, row.index_profile)
        # Las FAQ no tienen archivo propio; los documentos requieren validar su ruta antes de eliminarla.
        path = None if is_faq else FileService.checked_path(row.path)
        # Elimina el registro SQL una vez retirados los vectores asociados.
        await self.session.delete(row)
        # Confirma que el origen ya no existe en la base de datos.
        await self.session.commit()
        # Sólo los documentos tienen un archivo físico que limpiar.
        if path:
            # Borra el archivo de origen sin bloquear el servidor y tolerando un borrado previo.
            await asyncio.to_thread(path.unlink, missing_ok=True)
