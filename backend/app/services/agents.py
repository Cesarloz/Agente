from pydantic import ValidationError  # Captura fallos de validación de la configuración antes de persistir cambios.

from app.config import get_settings  # Obtiene la URL pública base usada al mostrar un agente publicado.
from app.errors import AppError  # Convierte errores internos en mensajes controlados de la API.
from app.models import Agent, Document, FAQ, Job, Prompt  # Entidades de agente, índices de conocimiento, trabajos y versiones de prompt.
from app.repositories import Repository, serialize  # Acceso a registros y serialización de resultados para la API.
from app.schemas import AgentConfig  # Contrato que valida proveedor, modelos, límites y demás opciones del agente.


def agent_view(agent):  # Construye la vista pública de administración de un agente.
    return {  # Devuelve un diccionario serializable sin alterar la entidad persistida.
        **agent.config, "id": agent.id, "name": agent.name, "created_at": agent.created_at,  # Combina configuración con ID, nombre y fecha de creación almacenados.
        "logo_url": f"/api/agents/{agent.id}/logo" if agent.logo_filename else None,  # Publica una ruta de logo únicamente si hay un archivo configurado.
        "published": agent.published,  # Indica si el chat público está habilitado.
        "public_url": f"{get_settings().public_base_url.rstrip('/')}/chat/{agent.id}" if agent.published else None,  # Construye el enlace del chat únicamente para agentes publicados.
    }


class AgentService:  # Gestiona configuración, reindexación necesaria y versiones del prompt de sistema.
    def __init__(self, session):  # Recibe la sesión SQL de la petición administrativa.
        self.session = session  # Conserva la transacción compartida para confirmar cambios relacionados juntos.
        self.repo = Repository(session, Agent)  # Prepara un repositorio específico para entidades Agent.

    async def list(self):  # Lista agentes disponibles para la administración.
        return [agent_view(a) for a in await self.repo.list()]  # Transforma cada registro a la misma representación de API.

    async def get(self, id):  # Obtiene la configuración de un agente por su identificador.
        return agent_view(await self.repo.get(id))  # Valida existencia mediante el repositorio y devuelve su vista.

    async def create(self, data: AgentConfig):  # Crea un agente a partir de configuración ya validada por AgentConfig.
        agent = await self.repo.add(name=data.name, config=data.model_dump())  # Persiste nombre y configuración, incluidos proveedor, modelo y system_prompt activo.
        await Repository(self.session, Prompt).add(agent_id=agent.id, body=data.system_prompt)  # Registra también la versión inicial del prompt para consultar/restaurar posteriormente.
        await self.session.commit()  # Confirma agente y primera versión juntos.
        return agent_view(agent)  # Devuelve la configuración creada; aquí no se llama a un modelo.

    async def update(self, id, values):  # Aplica una actualización parcial y programa índices nuevos cuando corresponde.
        agent = await self.repo.get(id, lock=True)  # Bloquea el agente durante la transacción para evitar actualizaciones concurrentes inconsistentes.
        try:  # Valida el resultado completo de combinar valores antiguos y nuevos.
            config = AgentConfig.model_validate({**agent.config, **values})  # Los campos enviados reemplazan a los existentes; los omitidos se conservan.
        except ValidationError:  # Intercepta errores de tipos, valores o límites de la configuración.
            raise AppError("Configuración inválida; revisa campos y límites", 422) from None  # Devuelve un error 422 sin volcar objetos internos.
        old = agent.config  # Conserva la configuración anterior para detectar cambios que invaliden el índice.
        agent.name, agent.config = config.name, config.model_dump()  # Actualiza nombre y el diccionario validado del agente.
        # Cambiar proveedor/modelo de embeddings o fragmentación exige volver a vectorizar.
        reindex = any(old.get(k) != agent.config[k] for k in ("embedding_provider", "embedding_model", "chunk_size", "chunk_overlap"))
        if reindex:  # Cambios ajenos al índice no programan nuevas llamadas de embeddings.
            for model, kind in ((Document, "document"), (FAQ, "faq")):  # Revisa tanto documentos como FAQs que dependen del perfil de recuperación.
                for row in await Repository(self.session, model).list(model.agent_id == id, limit=100000):  # Recorre registros del mismo agente para preparar su reindexación.
                    row.status = "PENDING"  # Marca el registro pendiente para evitar usar el índice como si ya estuviera actualizado.
                    await Repository(self.session, Job).add(kind=kind, payload={"agent_id": id, "id": row.id})  # Encola trabajo duradero; el worker vectorizará, no esta petición administrativa.
        await self.session.commit()  # Confirma configuración y trabajos de reindexación en una sola transacción.
        return agent_view(agent)  # Devuelve la vista actualizada del agente.

    async def prompts(self, id):  # Consulta el historial de versiones de instrucciones del sistema.
        await self.repo.get(id)  # Comprueba que exista el agente antes de listar sus prompts.
        return [serialize(p) for p in await Repository(self.session, Prompt).list(Prompt.agent_id == id)]  # Devuelve las versiones guardadas, filtradas por pertenencia al agente.

    async def save_prompt(self, id, body):  # Guarda una nueva versión y la convierte en el prompt activo.
        agent = await self.repo.get(id, lock=True)  # Bloquea la configuración durante la actualización del prompt.
        prompt = await Repository(self.session, Prompt).add(agent_id=id, body=body)  # Inserta un registro nuevo; no sobrescribe las versiones históricas.
        agent.config = {**agent.config, "system_prompt": body}  # Actualiza system_prompt, que ApplicationRuntimeHooks.generate enviará como instrucciones.
        await self.session.commit()  # Confirma a la vez la versión histórica y la configuración activa.
        return serialize(prompt)  # Devuelve la versión recién creada para la interfaz administrativa.

    async def restore_prompt(self, id, pid):  # Restaura una versión anterior conservando trazabilidad del cambio.
        prompt = await Repository(self.session, Prompt).get(pid, id)  # Verifica que la versión solicitada pertenezca al agente indicado.
        return await self.save_prompt(id, prompt.body)  # Crea una versión nueva con aquel contenido y la activa sin borrar el historial.
