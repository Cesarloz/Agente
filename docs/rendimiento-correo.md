# Rendimiento, enlaces y Gmail

## Revisar la latencia

El flujo completo puede llamar al LLM para clasificar el alcance, decidir
herramientas y redactar la respuesta. Si no hay alcance definido o herramientas
habilitadas, se omiten esas llamadas. Consultar una base de datos también puede
añadir tiempo antes de la generación. La velocidad final depende del modelo,
su razonamiento, la longitud de salida, la red y el estado del proveedor.

En **Playground → Ejecución del agente** se muestran el tiempo total, la espera
inicial y el tiempo de cada etapa. En **Preguntas de usuarios** quedan el tiempo
total y la traza de etapas de cada consulta. Una espera inicial larga puede
indicar que otro mensaje de esa conversación mantiene el bloqueo de PostgreSQL.

Esta versión incorpora:

- Una sola vectorización de la misma pregunta para buscar FAQ y documentos.
  La caché dura una petición y se separa por perfil de embeddings.
- Respuestas directas para FAQ con pregunta idéntica, ignorando mayúsculas,
  espacios y puntuación exterior. Primero se aplica la política de alcance.
  FAQ con respuestas contradictorias o archivos asociados conservan el flujo
  completo. El administrador puede desactivar esta opción.
- Cero reintentos automáticos del LLM y 45 segundos por llamada como valores
  iniciales. Evitan acumular varias esperas ante errores del proveedor; no
  implican que todas las respuestas exitosas tarden menos de 45 segundos.
- Controles en **Agentes → Modelo → Velocidad y límites de respuesta** para
  timeout, reintentos, tokens de salida y razonamiento de OpenAI. Mantén
  razonamiento en `default` si el modelo no admite `reasoning_effort`.
  El límite total del runtime sigue siendo de 150 segundos.

Las pruebas automáticas comprueban llamadas evitadas y métricas con sustitutos
locales. Para comparar tu instalación, envía las mismas preguntas al mismo
modelo antes y después y revisa las trazas. No se ha medido aquí la latencia de
una cuenta real ni se garantiza una reducción porcentual.

## Enlaces en las respuestas

En **Agentes → Enlaces** escribe una entrada por línea:

```text
Agendar una cita | https://outlook.office.com/bookwithme/
```

Los enlaces se incluyen como referencias autorizadas para el LLM y como
accesos en la página pública. Las respuestas aceptan URLs HTTP(S) completas
y el formato `[Agendar una cita](https://outlook.office.com/bookwithme/)`.
Se presentan como enlaces clicables, sin interpretar HTML de los mensajes.
También puedes guardar una respuesta de FAQ con una liga.

La [dirección general de Book With Me](https://outlook.office.com/bookwithme/)
abre el servicio de Microsoft. Sustitúyela por el enlace compartible de tu
agenda para que los visitantes lleguen a tus horarios. Mostrar la liga no
crea ni confirma una reserva.

## Conectar Gmail y enviar una respuesta

1. Activa la verificación en dos pasos de tu cuenta y crea una
   [contraseña de aplicación de Google](https://support.google.com/accounts/answer/185833).
   Algunas cuentas de organización, protección avanzada o configuraciones de
   seguridad no ofrecen esta opción.
2. En **Configuración → Gmail**, introduce la dirección, el nombre del
   remitente y esa contraseña de aplicación; pulsa **Guardar Gmail**.
   Se guarda cifrada y el backend no la devuelve al panel.
3. Usa **Probar conexión Gmail**. Comprueba autenticación SMTP sin enviar
   ningún mensaje.
4. Abre **Conversaciones**, elige una conversación y despliega
   **Enviar respuesta por correo**. Marca **Preparar un correo para esta
   conversación**.
5. Introduce el destinatario y el asunto, revisa la respuesta y agrega la
   información adicional. Pulsa **Enviar correo por Gmail**.
6. Consulta **Actualizar estado del correo**. `DONE` indica que Gmail aceptó
   el mensaje para envío; no confirma que el destinatario lo haya leído o que
   su servidor ya lo haya entregado.

El envío utiliza [SMTP de Gmail con TLS](https://developers.google.com/workspace/gmail/imap/imap-smtp)
en `smtp.gmail.com:465`, con certificado verificado. El servidor debe permitir
conexiones salientes a ese puerto. La integración es SMTP mediante contraseña
de aplicación; no incluye un flujo OAuth «Iniciar sesión con Google» ni acceso
a la bandeja de entrada. Los límites de envío de Google siguen aplicándose.

El worker procesa los correos después de registrarlos en PostgreSQL. Un doble
clic con la misma solicitud no crea otro correo. Si una conexión se corta y
no es posible saber si Gmail aceptó el envío, queda en `NEEDS_REVIEW` y no se
reenvía automáticamente. Revisa Enviados y, si procede, autoriza el reintento
en **Agentes → Canales → Trabajos pendientes de atención**.

El administrador elige destinatario y contenido. La página pública y el LLM
no tienen permiso para iniciar envíos por sí mismos. No se envía ningún correo
al guardar la configuración. **Desconectar Gmail** elimina la credencial del
proyecto; revoca también la contraseña de aplicación en Google si ya no la usas.
