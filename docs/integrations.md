# Conexiones SQL y canales

## SQL de solo lectura

El runtime accede exclusivamente a `DatabaseQueryTool.execute(dsn, sql, allowed_tables, max_rows)`. La herramienta invoca `DatabaseConnector.query`, que valida siempre el AST con `SQLGuard` antes de crear la conexión. El agente nunca recibe un engine, credenciales o un ejecutor SQL sin restricciones.

`SQLGuard` acepta una sola consulta SELECT (incluye CTE de lectura y operaciones de conjuntos), resuelve referencias de CTE por ámbito, exige una lista de tablas y aplica un límite SQL literal de hasta 1000 filas. Rechaza escrituras dentro de cualquier nodo, tablas de sistema, funciones fuera de una lista explícita, funciones con esquema, CTE recursivas, bloqueo de filas, parámetros y cargas de archivos. Los permisos de `orders` y `public.orders` son distintos: configurar los identificadores usados por la consulta.

El límite de filas no limita el trabajo de un JOIN o agregado; por eso también existen timeouts del motor/driver. Las consultas se ejecutan mediante `asyncio.to_thread` y cierran conexión y engine al terminar.

| Motor | DSN | Protección de ejecución |
|---|---|---|
| PostgreSQL | `postgresql+psycopg://lector:CLAVE@host:5432/business` | Transacción READ ONLY, statement_timeout y lock_timeout |
| MySQL | `mysql+pymysql://lector:CLAVE@host:3306/business` | Transacción READ ONLY, MAX_EXECUTION_TIME y timeouts del driver; LOCAL INFILE deshabilitado |
| SQL Server | `mssql+pymssql://lector:CLAVE@host:1433/business` | AST, límite de filas, timeout de consultas y bloqueos; requiere usuario limitado a SELECT |
| SQLite | `sqlite:////data/business.sqlite3` | Archivo local existente, URI `mode=ro`, `PRAGMA query_only`, progress handler con plazo |

Los cuatro motores deben usar cuentas con SELECT sobre las tablas/vistas autorizadas, sin permisos de escritura, DDL, administración, EXECUTE de rutinas ni acceso a archivos o redes. En SQL Server es un requisito de seguridad: `ApplicationIntent=ReadOnly` no concede ni impone permisos de lectura y no reemplaza esos permisos. `test()` comprueba conectividad; no certifica la configuración completa de privilegios y devuelve `read_only_enforced=false` para SQL Server. Las vistas autorizadas son responsabilidad del administrador: revisar sus fuentes y políticas de acceso. No autorizar tablas con secretos ni la base operacional interna de la plataforma.

SQLite solo abre archivos ya existentes que el administrador haya montado en el backend. No admite `:memory:` ni crea un archivo si la ruta es errónea. Los errores públicos omiten SQL, rutas, DSN y mensajes del driver. Las credenciales pertenecen al servicio cifrado del backend, nunca al estado permanente de Streamlit.

## Telegram

Crear un bot y guardar su token en Configuración. Configurar un webhook HTTPS de FastAPI mediante `setWebhook`, usando un `secret_token` independiente. El servidor debe verificar `X-Telegram-Bot-Api-Secret-Token` antes de leer o encolar el evento. `verify_telegram` compara el secreto en tiempo constante. [Telegram: setWebhook](https://core.telegram.org/bots/api#setwebhook).

El adaptador usa `getMe` para probar la cuenta, `sendMessage` para texto y `sendDocument` con multipart real para archivos locales. Los mensajes extensos se dividen conservando todo su contenido; los archivos tienen un tope de 50 MiB. Los logs de HTTPX ocultan el token incluido en la URL de Telegram. [Telegram: métodos y envío de archivos](https://core.telegram.org/bots/api).

El extractor devuelve `user_id` del remitente y `recipient` del chat. Usar `recipient` para responder, especialmente en grupos. `external_message_id` se basa en `update_id`; ignorar ediciones, mensajes de bots y eventos sin texto. No se configura ni registra automáticamente un webhook en una cuenta externa durante las pruebas locales.

## WhatsApp Cloud API

Configurar token de acceso de sistema, Phone Number ID, App Secret, verify token y una versión de Graph API vigente habilitada para la aplicación (parámetro explícito; el adaptador no fija una versión obsoleta). El endpoint HTTPS de FastAPI atiende el challenge y verifica `X-Hub-Signature-256` sobre los bytes originales con HMAC SHA-256 y App Secret antes de parsear JSON. El verify token del challenge y el App Secret cumplen funciones diferentes. [Meta: configuración de webhooks](https://developers.facebook.com/docs/graph-api/webhooks/getting-started).

El extractor recorre todos los mensajes de cada lote, ignora confirmaciones de entrega y devuelve `phone_number_id`; contrastarlo con la integración para impedir cruces entre teléfonos que comparten App Secret. El identificador `from` se trata como cadena opaca para enviar respuestas. Los nuevos formatos de identidad deben validarse al actualizar la versión de API.

El adaptador envía texto por `/{phone-number-id}/messages`. Para archivos primero sube bytes multipart a `/{phone-number-id}/media`, recibe el ID y lo utiliza en un mensaje document/image/audio/video. Soporta los MIME habituales publicados por Meta y comprueba sus límites; el canal valida además los codecs de audio/video. Las respuestas libres requieren una sesión habilitada por WhatsApp; los mensajes de plantilla y campañas están fuera de este adaptador. [Colección oficial Meta: media](https://www.postman.com/meta/whatsapp-business-platform/folder/13382743-ecb27be5-4d27-4763-bbee-6a8002c04bf3), [Colección oficial Meta: Cloud API](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api).

## Persistencia, idempotencia y pruebas

Los adaptadores no realizan persistencia. FastAPI debe autenticar el evento, filtrar la integración, guardar una clave única `(integration_id, external_message_id)` y procesar mediante su worker persistente. Los IDs retornados por send_text/send_file permiten registrar entregas. No reintentar ciegamente un envío cuyo resultado sea ambiguo: el proveedor puede haberlo entregado antes de un timeout.

Solo los archivos autorizados para el agente y conversación deben llegar a `send_file`; el repositorio de archivos comprueba pertenencia y obtiene la ruta local. Los adaptadores no aceptan URLs de descarga. `tests/test_channels.py` usa HTTPX MockTransport y archivos temporales, sin llamadas ni mensajes reales. `tests/test_sql_guard.py` incluye ataques SQL y ejecución sobre SQLite real en modo lectura.

Fuentes consultadas el 5 de septiembre de 2026. La documentación directa de Meta devolvió HTTP 429 durante la consulta; los formatos de media y mensajes se contrastaron con la colección oficial de Meta en Postman.
