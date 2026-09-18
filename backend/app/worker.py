"""Cola durable de PostgreSQL; ejecuta indexación RAG y entregas fuera del proceso web."""
import asyncio  # Coordina trabajos, señales de parada y renovación de arrendamientos.
from datetime import timedelta  # Calcula expiración de trabajos y espera entre reintentos.
import signal  # Solicita una parada ordenada ante SIGINT/SIGTERM.

from sqlalchemy import or_, select, text  # Consultas de cola y bloqueo de liderazgo de PostgreSQL.

from app.config import get_settings  # Límites de reintentos e intervalo de sondeo.
from app.db import Session  # Crea sesiones independientes para trabajo y heartbeat.
from app.errors import AppError  # Distingue errores de aplicación sin revelar excepciones internas.
from app.models import Document, FAQ, Integration, Job, Log, utcnow  # Estado durable de cola y fuentes.
from app.repositories import Repository  # Acceso y validación de pertenencia de las entidades.
from app.schemas import ChatInput  # Valida mensajes recibidos desde Telegram/WhatsApp.
from app.services.connections import IntegrationService  # Construye adaptadores de entrega de cada canal.
from app.services.conversations import ConversationService  # Ejecuta el grafo y persiste su respuesta.
from app.services.email import EmailService  # Entrega los correos previamente encolados.
from app.services.knowledge import DocumentService, FAQService, FileService  # Indexa fuentes y resuelve adjuntos.


async def claim():
    """Reserva el trabajo más antiguo elegible sin esperar por filas ya bloqueadas."""
    async with Session() as session:  # La reserva tiene una transacción corta, independiente del procesamiento.
        # Un PENDING con locked_until futuro sigue esperando su periodo de reintento.
        pending = (Job.status == "PENDING") & or_(Job.locked_until.is_(None), Job.locked_until <= utcnow())
        # Recupera también RUNNING vencidos; SKIP LOCKED evita que dos procesos reserven la misma fila.
        query = select(Job).where(or_(pending, (Job.status == "RUNNING") & (Job.locked_until < utcnow()))).order_by(Job.created_at).limit(1).with_for_update(skip_locked=True)
        job = await session.scalar(query)  # Obtiene y bloquea como máximo un trabajo.
        if not job:
            return None  # El bucle principal esperará sin hacer llamadas al modelo.
        job.status, job.locked_until = "RUNNING", utcnow() + timedelta(minutes=10)  # Reserva temporal renovable.
        job.attempts += 1  # Cuenta intentos de trabajo; los envíos reutilizan respuestas ya generadas.
        await session.commit()  # Publica la reserva antes de empezar trabajo externo.
        return job.id  # El procesamiento abrirá su propia sesión.


async def heartbeat(id, stop):
    """Mantiene reservado un trabajo largo para evitar que se ejecute de nuevo por expiración."""
    while not stop.is_set():  # Finaliza cuando process termina o falla.
        try:
            await asyncio.wait_for(stop.wait(), 30)  # Espera interruptible; no retrasa una parada ya solicitada.
        except TimeoutError:
            async with Session() as session:  # Renueva con otra conexión, sin compartir la sesión del trabajo.
                job = await session.get(Job, id)  # Comprueba el estado durable más reciente.
                if job and job.status == "RUNNING":
                    job.locked_until = utcnow() + timedelta(minutes=10)  # Extiende solo trabajos aún activos.
                    await session.commit()  # Hace visible la renovación al selector de trabajos.


async def process(id):
    """Procesa un trabajo y conserva marcadores de entrega para no repetir generación o envíos."""
    async with Session() as session:  # Agrupa los cambios del trabajo y los servicios de aplicación.
        job = await Repository(session, Job).get(id)  # Relee la reserva durable.
        p = dict(job.payload)  # Copia JSON para reasignarlo después: SQLAlchemy debe detectar los cambios.
        try:
            if job.kind == "document":
                await DocumentService(session).index(p["agent_id"], p["id"])  # Fragmenta e indexa documentos para RAG.
            elif job.kind == "faq":
                await FAQService(session).index(p["agent_id"], p["id"])  # Crea la proyección vectorial de la FAQ.
            elif job.kind in {"delete_document", "delete_faq"}:
                await DocumentService(session).purge(p["agent_id"], p["id"], job.kind == "delete_faq")  # Elimina vectores.
            elif job.kind == "email":
                if p.get("send_in_progress"):
                    # El proceso anterior pudo enviar el correo antes de perder confirmación.
                    job.status, job.error = "NEEDS_REVIEW", "Un correo quedó sin confirmación. Revisa Enviados en Gmail antes de autorizar un reenvío."
                    await session.commit()  # Bloquea el reenvío automático ambiguo.
                    return  # La revisión se realiza antes de autorizar una entrega nueva.
                if not p.get("email_sent"):
                    await EmailService(session).deliver(job)  # El servicio conserva su marcador de envío.
            elif job.kind == "channel":
                if p.get("send_in_progress"):
                    # Sin confirmación no puede saberse si Telegram/WhatsApp ya aceptó el mensaje.
                    job.status, job.error = "NEEDS_REVIEW", "Un envío quedó sin confirmación. Revisa el canal antes de autorizar un reenvío."
                    await session.commit()  # Conserva la necesidad de revisión para impedir duplicados.
                    return  # No vuelve a generar respuesta ni a enviar a ciegas.
                integration = await Repository(session, Integration).get(p["integration_id"], p["agent_id"])  # Valida pertenencia.
                if not integration.enabled:
                    job.status = "CANCELLED"  # Respeta desactivaciones posteriores a la recepción del mensaje.
                    await session.commit()  # Guarda cancelación sin ejecutar el agente.
                    return  # Evita tokens y entregas para una integración deshabilitada.
                # Persiste respuesta y contador antes de entregar: reintentar envío no repite el agente.
                if "response_result" not in p:
                    # chat guarda response_result junto al historial y auditoría en su misma transacción.
                    result = await ConversationService(session).chat(p["agent_id"], ChatInput(message=p["text"], user_id=p["user_id"], channel=p["channel"]), result_job=job)
                    p["response_result"] = result  # Mantiene sincronizada la copia local del payload.
                    job.payload = p  # Conserva el resultado para las etapas de entrega siguientes.
                    await session.commit()  # Hace durable el payload antes de contactar al canal.
                adapter = await IntegrationService(session).adapter(integration)  # Resuelve credenciales en el servicio.
                recipient = p.get("recipient") or p["user_id"]  # Usa el destino explícito o la identidad del canal.
                if not p.get("text_sent"):
                    p["send_in_progress"] = True  # Deja evidencia de un envío potencialmente ambiguo.
                    job.payload = dict(p)  # La reasignación JSON asegura seguimiento del cambio.
                    await session.commit()  # Guarda el marcador antes de realizar la operación externa.
                    await adapter.send_text(recipient, p["response_result"]["response"])  # Entrega la respuesta guardada.
                    p["send_in_progress"] = False  # El canal confirmó que esta llamada terminó.
                    p["text_sent"] = True  # Los reintentos siguientes omitirán el texto.
                    job.payload = dict(p)  # Actualiza los marcadores de entrega.
                    await session.commit()  # Publica confirmación antes de pasar a adjuntos.
                sent_files = set(p.get("sent_files", []))  # IDs confirmados: evita repetir archivos entregados.
                for file in p["response_result"].get("attachments", []):
                    fid = file.get("id") or file.get("file_id")  # Acepta los identificadores del contrato de adjuntos.
                    if fid and fid not in sent_files:
                        row = await FileService(session).download(p["agent_id"], fid)  # Valida acceso y ruta del archivo.
                        p["send_in_progress"] = True  # Protege también cada envío de adjunto ante ambigüedad.
                        job.payload = dict(p)  # Prepara el marcador durable del archivo actual.
                        await session.commit()  # Guarda intención antes de contactar al canal.
                        await adapter.send_file(recipient, row.path, row.name, row.mime_type)  # Entrega el archivo autorizado.
                        p["send_in_progress"] = False  # El adaptador confirmó este envío.
                        sent_files.add(fid)  # Deduplica referencias al mismo archivo dentro del resultado.
                        p["sent_files"] = list(sent_files)  # Convierte el conjunto a JSON persistible.
                        job.payload = dict(p)  # Registra qué adjuntos ya llegaron al canal.
                        await session.commit()  # Confirma cada archivo antes de enviar el siguiente.
            else:
                raise AppError("Tipo de trabajo desconocido")  # Rechaza payloads con operación no soportada.
            job.status, job.error, job.locked_until = "DONE", None, None  # Marca éxito y libera la reserva temporal.
            await session.commit()  # Hace durable la finalización del trabajo.
        except Exception as error:
            await session.rollback()  # Descarta cambios sin confirmar; conserva marcadores previos ya confirmados.
            job = await Repository(session, Job).get(id)  # Relee el estado durable después del rollback.
            if isinstance(error, AppError) and error.status == 404 and job.kind not in {"channel", "email"}:
                job.status, job.error, job.locked_until = "DONE", None, None  # La fuente borrada vuelve obsoleta la proyección.
                await session.commit()  # Evita reindexar eternamente una fuente que ya no existe.
                return  # No consume otro intento de indexación o embeddings.
            # No copia excepciones de SDK que podrían contener credenciales, URLs o consultas privadas.
            message = error.message if isinstance(error, AppError) else "El trabajo falló. Revisa la configuración, credenciales y conectividad."
            job.error = message  # Conserva un diagnóstico público para administración.
            job.status = "PENDING" if job.attempts < get_settings().worker_max_attempts else "FAILED"  # Reintentos acotados.
            if job.kind in {"channel", "email"} and job.payload.get("send_in_progress"):
                job.status = "NEEDS_REVIEW"  # La ambigüedad de entrega impide un reintento automático.
                job.error = "Envío sin confirmación; revisa el canal antes de autorizar un reenvío."  # Acción administrativa.
            # Espera exponencial limitada a 60 segundos, solo para trabajos que sí se pueden repetir.
            job.locked_until = utcnow() + timedelta(seconds=min(60, 2 ** job.attempts)) if job.status == "PENDING" else None
            if job.kind in {"document", "faq"}:
                row = await session.get(Document if job.kind == "document" else FAQ, p["id"])  # Sincroniza el estado de la fuente.
                if row:
                    row.status, row.error = "ERROR", message  # Expone el fallo de indexación al administrador.
            await Repository(session, Log).add(event="job_error", details={"job_id": id, "kind": job.kind, "error": message})  # Auditoría.
            await session.commit()  # Guarda estado de reintento/revisión y diagnóstico de forma durable.


async def run():
    """Ejecuta un único worker de proyecciones y atiende los trabajos en orden."""
    stop = asyncio.Event()  # Señal compartida de parada ordenada del bucle principal.
    loop = asyncio.get_running_loop()  # Obtiene el event loop que procesa cola y heartbeat.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)  # Solicita terminar después de procesar el trabajo activo.
        except NotImplementedError:
            pass  # Algunos event loops de Windows no admiten manejadores de señales.
    # Un único worker conserva el orden de reemplazo/borrado de índices vectoriales.
    async with Session() as leader:  # Mantiene viva la sesión propietaria del bloqueo de liderazgo.
        if leader.bind.dialect.name == "postgresql":
            acquired = await leader.scalar(text("SELECT pg_try_advisory_lock(726495031)"))  # Impide un segundo worker.
            if not acquired:
                raise RuntimeError("Ya hay un worker activo")  # No modifica las mismas proyecciones en paralelo.
        while not stop.is_set():  # Deja de reservar trabajos cuando se solicita parada.
            id = await claim()  # Reserva el siguiente trabajo elegible.
            if id:
                heartbeat_stop = asyncio.Event()  # Permite cancelar solo la renovación de este trabajo.
                pulse = asyncio.create_task(heartbeat(id, heartbeat_stop))  # Renueva en paralelo sin repetir el trabajo.
                try:
                    await process(id)  # Ejecuta indexación, correo o conversación/entrega.
                finally:
                    heartbeat_stop.set()  # Detiene renovaciones incluso si el procesamiento falla.
                    await pulse  # Espera cierre limpio de la tarea auxiliar.
            else:
                try:
                    await asyncio.wait_for(stop.wait(), get_settings().worker_poll_seconds)  # Sondeo con espera interruptible.
                except TimeoutError:
                    pass  # Vuelve a consultar la cola al terminar el intervalo configurado.


if __name__ == "__main__":
    asyncio.run(run())  # Permite iniciar el proceso independiente mediante python -m app.worker.
