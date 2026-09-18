"""Configura bases de datos y canales; aquí no se envían prompts al modelo."""

import json  # Serializa secretos de integración antes de entregarlos al almacén cifrado.

from sqlalchemy import select  # Construye consultas SQL parametrizadas a la base de la plataforma.
from sqlalchemy.engine import make_url  # Analiza el DSN sin abrir una conexión externa.

from app.config import get_settings  # Accede a la versión configurada de la API de WhatsApp.
from app.errors import AppError  # Expone errores controlados sin filtrar credenciales.
from app.models import Agent, DatabaseConnection, Integration  # Entidades persistentes administradas aquí.
from app.repositories import Repository, serialize  # Acceso con aislamiento por agente y salida serializable.
from app.services.credentials import CredentialService  # Lee/escribe DSN y claves mediante cifrado.


class DatabaseService:  # Gestiona conexiones consultables por las herramientas del agente.
    def __init__(self, session):
        self.session = session  # Conserva la transacción de la petición actual.
        self.credentials = CredentialService(session)  # Comparte esa sesión con el almacén de credenciales.

    async def list(self, agent_id):
        # Filtra por agente y excluye el identificador interno del secreto de la respuesta pública.
        return [{**serialize(r, exclude=("credential_name",)), "configured": True} for r in await Repository(self.session, DatabaseConnection).list(DatabaseConnection.agent_id == agent_id)]

    async def save(self, agent_id, data, id=None):
        await Repository(self.session, Agent).get(agent_id)  # Comprueba que el agente exista.
        row = await Repository(self.session, DatabaseConnection).get(id, agent_id) if id else None  # Aísla edición.
        if not row and not data.dsn:  # Una conexión nueva necesita un DSN; una edición puede conservarlo.
            raise AppError("Es necesario especificar DSN")
        if data.dsn:  # Valida un secreto nuevo antes de almacenarlo.
            try:
                url = make_url(data.dsn.get_secret_value())  # Extrae el DSN solo en memoria para analizarlo.
                if url.get_backend_name() != data.dialect:  # Evita motor y driver incoherentes.
                    raise ValueError()  # Reutiliza el mensaje controlado del bloque siguiente.
            except Exception:  # Un error del parser podría contener el DSN; no lo expone.
                raise AppError("El DSN no coincide con el motor seleccionado") from None
        if not row:  # Crea la ficha de conexión sin guardar el secreto en sus columnas.
            row = await Repository(self.session, DatabaseConnection).add(agent_id=agent_id, name=data.name, dialect=data.dialect, credential_name="pending", allowed_tables=data.allowed_tables, max_rows=data.max_rows)
            row.credential_name = f"database:{row.id}"  # Vincula el secreto mediante un identificador único.
        # Actualiza nombre, motor, tablas autorizadas y límite de filas para las herramientas SQL.
        row.name, row.dialect, row.allowed_tables, row.max_rows = data.name, data.dialect, data.allowed_tables, data.max_rows
        if data.dsn:  # Un DSN omitido conserva la credencial anterior.
            await self.credentials.set(row.credential_name, data.dsn.get_secret_value())  # Almacena cifrado.
        await self.session.commit()  # Confirma ficha y secreto de forma conjunta.
        return {**serialize(row, exclude=("credential_name",)), "configured": True}  # Responde sin secretos.

    async def delete(self, agent_id, id):
        from app.models import Credential  # Carga la entidad necesaria para borrar también su secreto.
        row = await Repository(self.session, DatabaseConnection).get(id, agent_id)  # Comprueba pertenencia.
        credential = await self.session.scalar(select(Credential).where(Credential.name == row.credential_name))
        if credential:  # La eliminación también funciona si el secreto ya no existe.
            await self.session.delete(credential)  # Elimina el valor cifrado asociado.
        await self.session.delete(row)  # Elimina la conexión administrada.
        await self.session.commit()  # Confirma ambas eliminaciones en una transacción.
        return {"deleted": True}  # Informa al cliente de la operación completada.

    async def test(self, agent_id, id):
        from app.tools import DatabaseConnector  # Usa el mismo conector que las herramientas SQL del runtime.
        row = await Repository(self.session, DatabaseConnection).get(id, agent_id)  # Valida el acceso al recurso.
        try:
            return await DatabaseConnector().test(await self.credentials.get(row.credential_name))  # Prueba real SQL.
        except Exception:  # No devuelve detalles del driver que pudieran revelar el DSN.
            raise AppError("No se pudo conectar. Revisa DSN, permisos de lectura y conectividad.", 502) from None


class IntegrationService:  # Gestiona Telegram/WhatsApp, sus metadatos públicos y secretos cifrados.
    def __init__(self, session):
        self.session = session  # Conserva la sesión de base de datos de esta petición.
        self.credentials = CredentialService(session)  # Todas las claves pasan por el servicio de cifrado.

    async def list(self, agent_id):
        # El listado contiene configuración del canal, pero nunca sus tokens ni referencias a credenciales.
        return [serialize(r, exclude=("credential_name",)) for r in await Repository(self.session, Integration).list(Integration.agent_id == agent_id)]

    async def save(self, agent_id, data):
        await Repository(self.session, Agent).get(agent_id)  # Comprueba el agente antes de crear integraciones.
        permitted = {"telegram": {"webhook_url"}, "whatsapp": {"phone_number_id", "api_version", "webhook_url"}}
        secret_keys = {"telegram": {"bot_token", "webhook_secret"}, "whatsapp": {"access_token", "app_secret", "verify_token"}}
        # Los metadatos permitidos se separan de las claves que requieren cifrado.
        if set(data.config) - permitted[data.channel] or set(data.secrets) - secret_keys[data.channel]:
            raise AppError("Campo de canal no permitido; usa secrets para las credenciales")
        row = await self.session.scalar(select(Integration).where(Integration.agent_id == agent_id, Integration.channel == data.channel))  # Una ficha por agente/canal.
        if not row:  # Crea la ficha si aún no existe una integración para ese canal.
            row = await Repository(self.session, Integration).add(agent_id=agent_id, channel=data.channel, credential_name=f"integration:{agent_id}:{data.channel}")
        try:
            secrets = await self.credentials.get_json(row.credential_name)  # Recupera las claves previas en memoria.
        except AppError as error:  # Distingue una credencial pendiente de otros fallos.
            if error.status != 409:  # No silencia errores distintos de falta de configuración.
                raise
            secrets = {}  # Primera configuración sin secretos previos.
        secrets.update({k: v.get_secret_value() for k, v in data.secrets.items() if v.get_secret_value()})  # Omitidos conservan valores.
        if data.enabled and not all(secrets.get(k) for k in secret_keys[data.channel]):  # Activación exige claves completas.
            raise AppError("Completa todas las credenciales del canal antes de activarlo")
        if data.enabled and data.channel == "whatsapp" and not data.config.get("phone_number_id"):
            raise AppError("Es necesario phone_number_id")  # WhatsApp necesita el identificador del emisor.
        row.enabled, row.config = data.enabled, data.config  # Actualiza estado y metadatos públicos.
        if secrets:  # Guarda las claves combinadas mediante el mismo cifrado de credenciales.
            await self.credentials.set(row.credential_name, json.dumps(secrets))
        await self.session.commit()  # Confirma integración y credenciales juntas.
        return serialize(row, exclude=("credential_name",))  # Devuelve configuración sin secretos.

    async def adapter(self, row):
        from app.channels import TelegramChannel, WhatsAppChannel  # Carga los clientes de mensajería.
        secrets = await self.credentials.get_json(row.credential_name)  # Descifra solo al necesitar el adaptador.
        if row.channel == "telegram":  # Telegram se autentica mediante su token de bot.
            return TelegramChannel(secrets["bot_token"])  # Construir no envía mensajes.
        # WhatsApp necesita token, número emisor y versión de API de configuración o del servidor.
        return WhatsAppChannel(secrets["access_token"], row.config["phone_number_id"], row.config.get("api_version", get_settings().graph_api_version))

    async def test(self, agent_id, id):
        row = await Repository(self.session, Integration).get(id, agent_id)  # Evita probar canales de otro agente.
        try:
            return await (await self.adapter(row)).test()  # Verifica la credencial con la API real del canal.
        except Exception:  # Sustituye errores externos por un mensaje sin secretos.
            raise AppError("No se pudo verificar el canal. Revisa configuración y credenciales.", 502) from None
