# Publicar la página de un agente

Cada agente publicado dispone de una página de conversación en
`/chat/ID_DEL_AGENTE`. La sirve FastAPI junto con los recursos y las operaciones
de chat de `/public/`. No necesitas crear una aplicación de Streamlit por agente.

Hay dos pasos distintos: instalar el proyecto para mantenerlo funcionando y
publicar un agente configurado desde el panel. En localhost, la página funciona
en tu computadora. Para compartir un enlace por Internet, el servidor también
necesita una dirección pública y HTTPS.

## Configurar y publicar desde el panel

1. Inicia el proyecto e ingresa con tu contraseña de superusuario. Las
   instrucciones para consultar la contraseña inicial están en el [README](../README.md).
2. En **Configuración**, guarda y prueba la API key de OpenAI o Google Gemini.
3. En **Agentes**, configura las instrucciones y el proveedor/modelo de
   conversación. Configura también embeddings si utilizarás una base de conocimiento.
4. En **General**, guarda el nombre y el mensaje de bienvenida. En **Apariencia**,
   selecciona **Subir logo** y pulsa **Guardar logo**. Se admiten PNG, JPEG y
   WebP de hasta 2 MB; puedes ver la imagen actual o usar **Eliminar logo**.
5. Prueba una conversación en **Playground**.
6. En **Publicación**, pulsa **Publicar página**. Debe estar activo, tener
   un modelo seleccionado y disponer de las credenciales necesarias. Si falta
   configuración, corrígela antes de volver a publicar.
7. Usa **Abrir página del agente** y prueba también el enlace en una ventana
   privada. Confirma que se ve el logo y que una pregunta recibe respuesta.
   Consulta después esa conversación desde el panel.

La página usa el nombre, la descripción, la bienvenida y el logo guardados para
ese agente. Los cambios de configuración se reflejan al volver a abrirla.
**Retirar publicación** impide abrir la página y seguir enviando mensajes por
su API pública; la configuración del agente permanece guardada.

## Dirección local y dirección pública

`PUBLIC_BASE_URL` se configura en `.env`. Es la dirección de FastAPI vista desde
el navegador del visitante, sin `/chat/ID_DEL_AGENTE` al final.

| Instalación | Valor de ejemplo |
|---|---|
| Docker local, puerto predeterminado | `http://localhost:8000` |
| Acceso por el túnel SSH de la guía de servidor | `http://localhost:18000` |
| Servidor con dominio y HTTPS | `https://chat.tu-dominio.com` |

Si cambias `API_PORT`, actualiza también la URL local o el puerto destino del
proxy. Si cambias solo `PUBLIC_BASE_URL`, recrea los servicios para que lean el
nuevo entorno; `docker compose restart` no actualiza variables de entorno:

```bash
docker compose up -d
```

Después vuelve a abrir **Publicación** y copia el enlace actualizado.
Un enlace que empiece por `localhost` solo es útil en la computadora que ejecuta
la API o el túnel SSH; no es una dirección compartible con otros visitantes.

El chat tiene límites de 30 mensajes por minuto por IP y agente, y 120 por
minuto por agente. Las cookies nuevas no reinician estos contadores. Si el
servidor usa un proxy, configura sus IP de confianza como se indica abajo
para distinguir correctamente a los visitantes.

## Servidor Ubuntu con HTTPS

Completa primero la [guía de instalación del servidor](guia-deploy-servidor.md).
El ejemplo siguiente usa Caddy instalado en el propio Ubuntu y conserva los
puertos de API y administración enlazados a `127.0.0.1` en Compose.

1. Elige un subdominio, por ejemplo `chat.tu-dominio.com`, y apunta su registro
   DNS A a la IP pública del VPS. Si publicas un registro AAAA, la dirección IPv6
   también debe llegar a ese servidor.
2. Permite **80/TCP y 443/TCP** en el firewall del proveedor y del servidor.
   Conserva SSH para administrar y los puertos 8000/8501 privados.
3. Instala el paquete estable de Caddy siguiendo las
   [instrucciones oficiales para Ubuntu](https://caddyserver.com/docs/install#debian-ubuntu-raspbian).
   El paquete registra el servicio `caddy` en systemd.
4. En el `.env` existente de `~/agente`, cambia solo esta variable y conserva
   las demás, especialmente la contraseña de PostgreSQL:

   ```dotenv
   PUBLIC_BASE_URL=https://chat.tu-dominio.com
   ```

5. Actualiza los contenedores desde `~/agente`:

   ```bash
   sudo docker compose config --quiet
   sudo docker compose up -d
   ```

   Si Caddy está instalado en el host y la API dentro de Docker, consulta la
   IP del gateway que comunica ambos:

   ```bash
   sudo docker inspect --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' "$(sudo docker compose ps -q api)"
   ```

   En `.env`, establece `FORWARDED_ALLOW_IPS=127.0.0.1,IP_GATEWAY` sustituyendo
   `IP_GATEWAY` por esa IP exacta. Ejecuta `sudo docker compose up -d api`.
   Esto permite usar la IP del visitante enviada por Caddy para el límite de
   mensajes. No uses `*` y conserva el enlace de puertos a localhost. Si cambia
   la red Docker, revisa esta IP. Sin esta configuración varios visitantes
   pueden compartir el contador de la IP del proxy.

6. Abre `/etc/caddy/Caddyfile` con `sudoedit` y configura el bloque siguiente.
   Sustituye el dominio por el tuyo. En un servidor que ya aloje otros sitios,
   agrega el bloque conservando los existentes. También puedes consultar la
   plantilla [deploy/Caddyfile.example](../deploy/Caddyfile.example).

   ```caddyfile
   chat.tu-dominio.com {
       @agent_public path /chat/* /public/*
       handle @agent_public {
           reverse_proxy 127.0.0.1:8000
       }
       handle {
           respond "No encontrado" 404
       }
   }
   ```

7. Valida y recarga Caddy:

   ```bash
   sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   sudo systemctl reload caddy
   sudo systemctl status caddy --no-pager
   ```

Los bloques `handle` publican solo las rutas previstas y conservan sus prefijos.
No sustituyas `handle` por `handle_path`: este último elimina el prefijo antes
de enviarlo a FastAPI. [Referencia de Caddy](https://caddyserver.com/docs/caddyfile/directives/handle).

Con un dominio que resuelve al servidor y los puertos accesibles, Caddy obtiene
y renueva el certificado y redirige HTTP a HTTPS.
[Requisitos de HTTPS automático](https://caddyserver.com/docs/automatic-https).

El ejemplo devuelve 404 para `/api/*`, `/docs`, `/openapi.json`, `/health` y la
raíz del dominio. Mantén el panel disponible por el túnel SSH de la guía; la
contraseña de superusuario se utiliza allí. Si necesitas webhooks de Telegram
o WhatsApp, configura por separado sus rutas y validación según
[integraciones](integrations.md).

## Comprobar una publicación

Desde otra conexión a Internet, abre el enlace exacto que entrega el panel,
por ejemplo `https://chat.tu-dominio.com/chat/ID_DEL_AGENTE`. Verifica que carga
el logo y que puedes enviar una pregunta y recibir respuesta. Una prueba de
chat utiliza la cuota de tu proveedor configurado.

También puedes comprobar la entrega de la página y el aislamiento del panel:

```bash
curl --fail --output /dev/null https://chat.tu-dominio.com/chat/ID_DEL_AGENTE
curl --output /dev/null --silent --write-out '%{http_code}\n' https://chat.tu-dominio.com/api/agents
curl --output /dev/null --silent --write-out '%{http_code}\n' https://chat.tu-dominio.com/docs
```

Sustituye dominio e ID por los reales. El primer comando debe terminar sin
error y los otros dos deben imprimir `404` con este proxy. La página por sí
sola no comprueba credenciales ni cuota del proveedor: envía además una
pregunta desde el navegador. Retira temporalmente la publicación si quieres
comprobar que ese enlace deja de estar disponible y vuelve a publicarla al terminar.

Si aparece 502, comprueba `sudo docker compose ps` y la API local con
`curl --fail http://127.0.0.1:8000/health`. Si el enlace apunta a localhost,
revisa `PUBLIC_BASE_URL` y recrea los contenedores. Si falta el logo o el chat
no puede enviar mensajes, confirma que el proxy permite `/public/*`.

## Conservar los datos al actualizar

La publicación se guarda con el agente en PostgreSQL; los logos y el estado
de acceso del superusuario forman parte del volumen `storage`. El volumen
`secrets` conserva los secretos iniciales. Respalda los cuatro volúmenes del
proyecto, `.env`, el código y la configuración de Caddy antes de una actualización.
La guía del servidor incluye el procedimiento completo.

Actualiza manteniendo `.env`, `name: agente` y los nombres de volúmenes.
Ejecuta `docker compose up --build -d` y verifica el enlace ya publicado después
de las migraciones. No uses `docker compose down -v`: borraría los datos.
El Caddyfile desplegado en `/etc/caddy` se administra fuera de Docker; conserva
una copia de su contenido y de los cambios propios al preparar una nueva versión.

Estos archivos preparan la publicación; no registran un dominio ni ejecutan
un despliegue remoto automáticamente.
