# Guía para instalar Agente en un servidor

Esta guía instala una copia nueva de Agente en un servidor Ubuntu y permite administrarla desde tu computadora mediante una conexión privada. El servidor seguirá trabajando aunque apagues tu computadora. No hace cambios en la instalación de Windows.

**Es una instalación nueva:** copiar el código no transfiere los agentes, conversaciones, documentos ni credenciales guardadas en Docker Desktop. Si quieres conservar esos datos, necesitas una migración de los cuatro volúmenes y de la clave de cifrado. La diferencia se explica al final.

La guía sigue la configuración del proyecto revisada el 6 de septiembre de 2026. Los comandos de despliegue son instrucciones para ejecutar en un servidor nuevo; no se han ejecutado en un VPS real como parte de esta revisión. El panel requiere una contraseña de superusuario y cada agente puede publicar su propia página de chat.

## 1. Entender lo que vas a instalar

Un **VPS** es una computadora alquilada que permanece encendida en Internet. **Ubuntu** es su sistema operativo. **SSH** permite manejarla desde una terminal con una conexión cifrada. **Docker** ejecuta las partes de la aplicación en contenedores; **Compose** las coordina. Un **volumen** es el almacenamiento que conserva datos aunque se reemplacen los contenedores.

| Parte | Para qué sirve |
|---|---|
| `admin` | El panel web de administración, construido con Streamlit. |
| `api` | El backend FastAPI que administra y ejecuta los agentes. |
| `worker` | Procesa trabajos pendientes en segundo plano. |
| `postgres` | Guarda agentes, configuración, conversaciones y credenciales cifradas. |
| `chroma` | Guarda los índices utilizados para buscar en documentos. |
| `init` | Prepara almacenamiento y genera secretos cuando todavía no existen. |
| `migrate` | Prepara o actualiza las tablas de la base de datos. |

Puedes consultar los archivos originales: [README](../README.md), [Compose](../compose.yaml), [Dockerfile](../Dockerfile), [runtime](runtime.md) e [integraciones](integrations.md).

## 2. Contratar y preparar el servidor

Elige un VPS con **Ubuntu 24.04 LTS de 64 bits, arquitectura x86_64/amd64**. Como punto de partida para pruebas y pocos usuarios, propongo 2 CPU, 4 GB de RAM y 40 GB de disco. Es una estimación inicial, no una capacidad medida para tu carga: los documentos, índices y conversaciones aumentarán el consumo.

Solicita acceso SSH con un usuario que pueda utilizar `sudo`. Guarda la dirección IP del servidor y el nombre del usuario. Si el proveedor permite configurar una clave SSH al crear el VPS, utiliza esa opción y protege la clave privada en tu computadora.

En el firewall del proveedor, permite inicialmente el puerto **22/TCP**, preferiblemente solo desde tu dirección IP pública. Si cambia tu IP, tendrás que actualizar esa regla. No abras los puertos 8000, 8501, 5432 ni los de Chroma. Para esta fase no necesitas dominio ni certificado web: el acceso viajará dentro de SSH.

En los ejemplos, reemplaza `USUARIO` e `IP_DEL_SERVIDOR` por los datos reales. No escribas literalmente esos marcadores.

## 3. Entrar al servidor desde Windows

Abre PowerShell en tu computadora y ejecuta:

```powershell
ssh USUARIO@IP_DEL_SERVIDOR
```

La primera conexión pide confirmar la identidad del servidor. Comprueba la huella de su clave con la información o consola del proveedor antes de aceptarla. Introduce la contraseña o utiliza la clave SSH configurada. Al escribir una contraseña puede no verse ningún carácter: es normal.

**Cuando ya entraste por SSH, los comandos se ejecutan en Ubuntu.** Mantén esta ventana abierta. Para los pasos que digan Windows, abre otra ventana de PowerShell.

Si PowerShell indica que `ssh` no existe, instala el Cliente OpenSSH desde las características opcionales de Windows. Microsoft explica el procedimiento en [OpenSSH para Windows](https://learn.microsoft.com/es-es/windows-server/administration/openssh/openssh_install_firstuse).

## 4. Instalar Docker en Ubuntu

Ejecuta este bloque en la ventana conectada al servidor. Está pensado para un Ubuntu 24.04 nuevo, sin otra instalación de Docker. El bloque que empieza con `sudo tee` termina en `EOF`; copia también esa última línea.

```bash
sudo apt update
sudo apt install -y ca-certificates curl python3

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker version
sudo docker compose version
```

`hello-world` debe mostrar un mensaje de éxito. Los dos últimos comandos deben mostrar versiones. Se utiliza `sudo` para administrar Docker; no hace falta modificar los grupos del usuario.

Estas instrucciones usan el [repositorio oficial de Docker para Ubuntu](https://docs.docker.com/engine/install/ubuntu/). Instala la versión estable vigente y mantenla actualizada. Docker documenta una limitación de versiones anteriores a 28: otros equipos del mismo segmento de red podían alcanzar algunos puertos publicados en localhost. Esta guía no supone ni demuestra que tu computadora actual sea accesible de ese modo. Véase [publicación de puertos en Docker](https://docs.docker.com/engine/network/port-publishing/).

## 5. Empaquetar y subir el proyecto desde Windows

Abre otra ventana de PowerShell en tu computadora. Ejecuta:

```powershell
Set-Location "$env:USERPROFILE\Documents\Agente"

tar.exe -czf agente-deploy.tar.gz --exclude=__pycache__ --exclude=*.pyc --exclude=*.egg-info --exclude=.pytest_cache Dockerfile compose.yaml pyproject.toml requirements.lock alembic.ini .dockerignore .env.example .streamlit/config.toml README.md backend admin migrations scripts docs deploy

scp .\agente-deploy.tar.gz USUARIO@IP_DEL_SERVIDOR:~/
```

La lista incluye los archivos para construir la aplicación, `.streamlit/config.toml`, la documentación y la plantilla de proxy HTTPS `deploy/Caddyfile.example`. No incluye un eventual `.streamlit/secrets.toml`, `.venv`, `.env`, Git, bases de datos, documentos subidos ni secretos de Docker. El archivo `.env.example` es una plantilla sin tus credenciales reales.

No comprimas indiscriminadamente toda la carpeta del proyecto: podrías copiar datos privados y un entorno Python de Windows que no sirve en Ubuntu. No necesitas instalar Python ni las dependencias del proyecto manualmente en el servidor; Docker construirá el entorno.

## 6. Crear la configuración inicial en Ubuntu

Vuelve a la ventana conectada al servidor:

```bash
mkdir -p ~/agente
chmod 700 ~/agente
tar -xzf ~/agente-deploy.tar.gz -C ~/agente
cd ~/agente
```

Ejecuta una sola vez el bloque siguiente. Genera una contraseña aleatoria para PostgreSQL y crea `.env` con permisos privados. **No imprime la contraseña ni sobrescribe un `.env` existente.** Si aparece `FileExistsError`, el archivo ya existe: conserva su contenido y no repitas la configuración inicial para actualizar la aplicación.

```bash
python3 - <<'PY'
from pathlib import Path
import os
import secrets

os.umask(0o077)
config = Path('.env.example').read_text()
config = config.replace(
    'POSTGRES_PASSWORD=agente_local',
    'POSTGRES_PASSWORD=' + secrets.token_hex(32),
)
config = config.replace(
    'PUBLIC_BASE_URL=http://localhost:8000',
    'PUBLIC_BASE_URL=http://localhost:18000',
)
with open('.env', 'x') as file:
    file.write(config)
print('Configuración inicial creada.')
PY
```

La contraseña PostgreSQL debe establecerse antes del primer arranque. Después, cambiar únicamente esa contraseña en `.env` no cambia la de una base ya inicializada y puede romper la conexión.

Las API keys de OpenAI o Gemini se introducirán en el panel. No las añadas al código. El servicio `init` generará la contraseña inicial de superusuario y, por separado, el token administrativo y la clave maestra de cifrado. Se conservarán en un volumen; no deben regenerarse en una actualización. El comportamiento está en [init_secrets.py](../scripts/init_secrets.py).

## 7. Iniciar la aplicación y comprobarla

En Ubuntu, dentro de `~/agente`:

```bash
sudo docker compose config --quiet
sudo docker compose up --build -d
sudo docker compose ps -a
curl --fail http://127.0.0.1:8000/health
```

La primera construcción descarga imágenes y dependencias; puede tardar varios minutos. `config --quiet` valida la configuración sin mostrar contraseñas interpoladas.

Al terminar, `api`, `worker`, `admin`, `postgres` y `chroma` deben estar funcionando. `init` y `migrate` deben terminar con código **0**: es normal que aparezcan detenidos, porque son tareas de una sola ejecución. La API debe estar saludable y el último comando debe responder sin error HTTP.

Si algo falla, consulta:

```bash
sudo docker compose logs --tail=100 api worker admin migrate init
```

Conserva el mensaje de error para diagnosticarlo. Revisa cualquier registro antes de compartirlo y no pegues `.env`, tokens ni archivos de secretos. No borres los volúmenes para intentar arreglar un error de arranque.

Consulta la contraseña inicial del superusuario en esa terminal privada:

```bash
sudo docker compose run --rm --no-deps --user root init cat /run/agente/superuser_password.initial
```

El comando imprime únicamente la contraseña inicial en tu terminal; no la copies
en registros de soporte. Inicia sesión en el panel con ella y utiliza **Cambiar
contraseña** para guardar la que vayas a usar. El archivo inicial no se actualiza
y deja de ser válido tras el cambio. El cambio de contraseña cierra las sesiones
abiertas. El token administrativo para scripts sigue siendo independiente.

## 8. Abrir el panel desde tu computadora

En una ventana de PowerShell de Windows, ejecuta:

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18501:127.0.0.1:8501 -L 127.0.0.1:18000:127.0.0.1:8000 USUARIO@IP_DEL_SERVIDOR
```

Deja esa ventana abierta. Es normal que no muestre nada después de conectarse: está manteniendo el túnel. Abre en el navegador:

- Panel: [http://localhost:18501](http://localhost:18501).
- Estado de la API: [http://localhost:18000/health](http://localhost:18000/health).
- Documentación técnica: [http://localhost:18000/docs](http://localhost:18000/docs).

Aunque la dirección diga `localhost`, estarás viendo el servidor a través de SSH. Se usan los puertos locales 18501 y 18000 para evitar conflictos con tu instalación habitual de Windows. En el servidor se mantienen 8501 y 8000, limitados a localhost por Compose.

Cerrar la ventana del túnel cierra tu acceso desde esa computadora. **La aplicación sigue encendida en el servidor.** Para volver a entrar, repite el comando del túnel. No necesitas pulsar el botón Deploy de Streamlit: ese botón no despliega por sí solo todos los servicios de este proyecto.

La opción utilizada está documentada en [reenvío local de puertos de OpenSSH](https://man.openbsd.org/ssh#L).

## 9. Configurar el proveedor y probar el agente

En el panel, sigue estos pasos:

1. Ingresa con tu contraseña de superusuario. Abre **Configuración**, selecciona OpenAI o Google Gemini, introduce la API key y pulsa **Guardar API key**.
2. Pulsa **Probar conexión** y **Consultar modelos disponibles**. La cuenta del proveedor debe tener acceso y cuota suficientes para los modelos que elijas.
3. Abre **Agentes**, crea un agente y configura sus instrucciones.
4. En su sección **Modelo**, asigna proveedor, modelo de conversación y modelo de embeddings.
5. En **Apariencia**, sube un logo PNG, JPEG o WebP de hasta 2 MB y pulsa **Guardar logo**. En **General**, guarda el nombre y el mensaje de bienvenida.
6. Abre **Playground**, selecciona el agente y envía una pregunta.
7. Comprueba que aparezca en **Conversaciones** y **Preguntas de usuarios**.
8. Si usarás documentos, sube uno pequeño, espera a que termine de procesarse y pregunta algo cuya respuesta esté dentro de él.
9. En **Publicación**, pulsa **Publicar página**. Corrige cualquier requisito que marque la interfaz y utiliza **Abrir página del agente**.

Con la URL configurada en el paso 6 y el túnel abierto, el enlace será
`http://localhost:18000/chat/ID_DEL_AGENTE`. Abre esa página en una ventana privada
del navegador: debes ver el logo y poder conversar sin iniciar sesión de
superusuario. **Retirar publicación** cierra ese acceso. El enlace localhost
funciona desde tu computadora; para compartirlo con otras personas sigue
[Publicar la página de un agente](publicacion.md).

El backend cifra las credenciales antes de guardarlas en PostgreSQL. La protección de esos datos también depende de proteger el servidor y la clave maestra. El coste del VPS y el consumo del proveedor de modelos son conceptos separados.

## 10. Crear un respaldo completo

Haz un respaldo antes de actualizar y establece una frecuencia acorde con los datos que puedes permitirte perder. Esta guía no crea una tarea automática de respaldo.

Tu instalación utiliza cuatro volúmenes: `agente_postgres`, `agente_chroma`, `agente_storage` y `agente_secrets`. `agente_storage` incluye logos y la base de acceso del superusuario; `agente_secrets` contiene la clave de cifrado, el token administrativo y los secretos iniciales de acceso. **PostgreSQL y la clave original deben conservarse juntos para recuperar las credenciales cifradas.** Guardar solo la carpeta del código no es un respaldo de la aplicación.

El procedimiento siguiente hace un respaldo en frío: detiene temporalmente todos los servicios, copia los cuatro volúmenes y vuelve a arrancar. Hazlo cuando nadie esté utilizando el agente. También guarda `.env`, Compose, información de imágenes y el código desplegado. No copia archivos internos de PostgreSQL mientras está escribiendo.

En Ubuntu, copia el bloque completo. Los paréntesis hacen que un error termine solamente este bloque, no tu sesión SSH. El manejador final intenta volver a arrancar incluso si una copia falla y explica si hace falta intervención.

```bash
(
set -euo pipefail
cd "$HOME/agente"
sudo -v
sudo docker pull ubuntu:24.04
sudo docker volume inspect agente_postgres agente_chroma agente_storage agente_secrets >/dev/null

umask 077
backup_dir="$HOME/respaldo-agente-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
resume_needed=0
backup_complete=0

finish_backup() {
    result=$?
    trap - EXIT
    if [ "$resume_needed" -eq 1 ]; then
        printf 'Intentando volver a iniciar Agente...\n'
        if ! sudo docker compose up -d; then
            printf 'No se pudo completar el arranque. Ejecuta: cd ~/agente y sudo docker compose up -d\n' >&2
            printf 'Después revisa: sudo docker compose logs --tail=100 api worker admin migrate init\n' >&2
            result=1
        fi
    fi
    if [ "$backup_complete" -eq 1 ]; then
        printf 'Respaldo creado y archivos comprobados: %s\n' "$backup_dir"
    else
        printf 'Respaldo incompleto; no lo uses para restaurar: %s\n' "$backup_dir" >&2
        result=1
    fi
    exit "$result"
}
trap finish_backup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

resume_needed=1
sudo docker compose stop

for volume in agente_postgres agente_chroma agente_storage agente_secrets; do
    archive="$backup_dir/$volume.tar.gz"
    sudo docker run --rm --network none \
        --mount "type=volume,source=$volume,target=/source,readonly" \
        ubuntu:24.04 tar -czf - -C /source . > "$archive.part"
    test -s "$archive.part"
    tar -tzf "$archive.part" >/dev/null
    mv "$archive.part" "$archive"
done

cp -- .env compose.yaml "$backup_dir/"
if sudo test -f /etc/caddy/Caddyfile; then
    sudo cat /etc/caddy/Caddyfile > "$backup_dir/Caddyfile"
fi
sudo docker compose images > "$backup_dir/images.txt"
sudo docker image inspect agente-platform:local postgres:17-alpine chromadb/chroma:1.5.9 > "$backup_dir/image-details.json"
tar -czf "$backup_dir/codigo.tar.gz" \
    --exclude=__pycache__ --exclude='*.pyc' --exclude='*.egg-info' \
    Dockerfile compose.yaml pyproject.toml requirements.lock alembic.ini \
    .dockerignore .env.example .streamlit/config.toml README.md backend admin migrations scripts docs deploy
tar -tzf "$backup_dir/codigo.tar.gz" >/dev/null

(
    cd "$backup_dir"
    sha256sum agente_postgres.tar.gz agente_chroma.tar.gz agente_storage.tar.gz agente_secrets.tar.gz codigo.tar.gz .env compose.yaml images.txt image-details.json > SHA256SUMS
    if [ -f Caddyfile ]; then
        sha256sum Caddyfile >> SHA256SUMS
    fi
    sha256sum --check SHA256SUMS
)
backup_complete=1
)
```

Comprueba después que el servicio regresó:

```bash
cd ~/agente
sudo docker compose ps -a
curl --fail http://127.0.0.1:8000/health
```

La revisión de archivos detecta copias incompletas y permite comprobar cambios en los archivos, pero **no sustituye un ensayo de restauración**. Si se apaga el servidor, se corta la conexión o se fuerza la terminación del proceso, ningún manejador garantiza recuperar el servicio: vuelve a conectar, ejecuta `sudo docker compose up -d` desde `~/agente` y considera incompleto ese respaldo hasta revisarlo.

El método usa los nombres de volumen originales de `name: agente` en Compose. Si en el futuro cambias el nombre del proyecto, debes revisar los nombres antes de respaldar; el comando de inspección falla si falta alguno de los esperados. Si existe un Caddyfile en `/etc/caddy`, guarda también su contenido; cualquier otro archivo que hayas añadido para el proxy requiere incluirse en tu procedimiento. Referencia: [respaldo de volúmenes de Docker](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes).

## 11. Guardar una copia cifrada fuera del servidor

Un respaldo en el mismo VPS se pierde si pierdes ese servidor. Además, estos archivos contienen datos y la clave necesaria para descifrar credenciales. No los subas sin cifrar a un espacio compartido.

En Ubuntu instala la herramienta de cifrado:

```bash
sudo apt install -y gnupg
```

Sustituye `AAAAMMDD-HHMMSS` por la fecha que imprimió el respaldo. El siguiente bloque pide una contraseña de cifrado de forma interactiva; no la escribas en el comando. Guárdala en un gestor de contraseñas, separada del archivo del respaldo. Si la pierdes, no podrás abrirlo.

```bash
(
set -euo pipefail
umask 077
backup_dir="$HOME/respaldo-agente-AAAAMMDD-HHMMSS"
test -d "$backup_dir"
archive_name="$(basename "$backup_dir").tar.gz.gpg"
test ! -e "$HOME/$archive_name"
export GPG_TTY="$(tty)"
tar -czf - -C "$HOME" "$(basename "$backup_dir")" |
    gpg --symmetric --cipher-algo AES256 --output "$HOME/$archive_name"
test -s "$HOME/$archive_name"
printf 'Copia cifrada: %s\n' "$HOME/$archive_name"
)
```

En PowerShell de Windows, elige una carpeta privada y descarga el archivo cifrado, sustituyendo también la fecha:

```powershell
scp USUARIO@IP_DEL_SERVIDOR:~/respaldo-agente-AAAAMMDD-HHMMSS.tar.gz.gpg .
```

Conserva otra copia cifrada en un lugar independiente y controla quién puede acceder a ella. Conserva también una copia protegida separada de la clave maestra de cifrado, mediante tu procedimiento de custodia de secretos; no la pegues en conversaciones ni en archivos de instrucciones. El respaldo completo ya incluye su original dentro de `agente_secrets.tar.gz`.

Programa un ensayo de recuperación en otro servidor aislado: utiliza las mismas versiones de imágenes y del código, verifica los hashes y restaura los cuatro volúmenes antes de iniciar. La copia física de PostgreSQL exige compatibilidad de versión y plataforma; no aproveches una restauración para cambiar de versión mayor. Comprueba que puedas ver agentes, abrir documentos y usar una credencial restaurada. No conectes los mismos webhooks de producción al servidor del ensayo.

Referencias: [comandos de cifrado de GnuPG](https://www.gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html) y [respaldo del sistema de archivos de PostgreSQL](https://www.postgresql.org/docs/17/backup-file.html).

## 12. Operación diaria y actualizaciones

Para ver el estado o los últimos mensajes, en Ubuntu:

```bash
cd ~/agente
sudo docker compose ps -a
sudo docker compose logs --tail=100 api worker
df -h
free -h
```

`df -h` muestra el espacio libre y `free -h` la memoria. Vigila el crecimiento de documentos, base de datos, índices y registros. También conviene comprobar periódicamente que los respaldos se pueden restaurar.

Para reiniciar las partes de la aplicación:

```bash
sudo docker compose restart api worker admin
```

Para apagar conservando los datos:

```bash
sudo docker compose down
```

Para volver a arrancar:

```bash
sudo docker compose up -d
```

**No añadas `-v` ni `--volumes` a `down`.** Esas opciones eliminan los volúmenes. Tampoco uses comandos de limpieza de volúmenes como método para resolver problemas. Véase [docker compose down](https://docs.docker.com/reference/cli/docker/compose/down/).

Para actualizar el código, reserva una ventana de mantenimiento y sigue este orden:

1. Revisa los cambios y sus migraciones. Confirma que no estás mezclando una actualización de la aplicación con una actualización mayor de PostgreSQL.
2. Genera y descarga un respaldo completo de la versión actual usando los pasos anteriores. Conserva el código e imágenes de esa versión para una posible recuperación.
3. Crea un paquete nuevo en Windows con la misma lista selectiva del paso 5 y súbelo al servidor.
4. Prepara una carpeta nueva con ese paquete para evitar que queden archivos eliminados en la versión nueva. Copia a ella el `.env` existente; no ejecutes otra vez la generación de contraseña.
5. Mantén `name: agente` y los mismos cuatro volúmenes en Compose. No cambies el nombre del proyecto con `-p` ni `COMPOSE_PROJECT_NAME`.
6. Detén la versión anterior, cambia la carpeta de código y arranca con construcción y migraciones. Verifica salud, acceso de superusuario, Playground y el enlace del chat publicado antes de dar por terminada la actualización.

Este bloque de Ubuntu concreta los pasos 4 a 6. Se usa únicamente después de tener el respaldo y subir el paquete nuevo:

```bash
(
set -euo pipefail
umask 077
cd "$HOME/agente"
test -f .env
test -f "$HOME/agente-deploy.tar.gz"

release_dir="$(mktemp -d "$HOME/agente-nuevo.XXXXXX")"
tar -xzf "$HOME/agente-deploy.tar.gz" -C "$release_dir"
test ! -e "$release_dir/.env"
cp -- .env "$release_dir/.env"
chmod 600 "$release_dir/.env"
sudo docker compose -f "$release_dir/compose.yaml" --project-directory "$release_dir" config --quiet

previous_dir="$HOME/agente-anterior-$(date +%Y%m%d-%H%M%S)"
test ! -e "$previous_dir"
sudo docker compose down
cd "$HOME"
mv "$HOME/agente" "$previous_dir"
mv "$release_dir" "$HOME/agente"
cd "$HOME/agente"
sudo docker compose up --build -d
sudo docker compose ps -a
curl --fail http://127.0.0.1:8000/health
printf 'Código anterior conservado en: %s\n' "$previous_dir"
)
```

Compose ejecuta `init` conservando los secretos existentes y `migrate` antes de iniciar la API. Si el bloque falla después de detener, el servicio puede quedar apagado: revisa cuál carpeta contiene el código, consulta los errores y vuelve a iniciar desde la carpeta correcta. No repitas movimientos sin comprobar las rutas. Si una migración ya cambió la base de datos, volver a copiar el código anterior puede no ser suficiente: la recuperación debe coordinar código y respaldo de datos.

Los comandos de actualización no reemplazan `.env` con la plantilla, no borran volúmenes ni regeneran la clave maestra. En una instalación anterior sin acceso de superusuario, `init` añade los archivos nuevos y podrás consultar la contraseña inicial con el comando del paso 7. Los cambios de contraseña, logos y publicaciones existentes se conservan en sus volúmenes. Las carpetas de código anterior contienen una copia privada de `.env`; consérvalas protegidas y gestiona su retención junto con los respaldos.

## 13. Publicar la página del agente en Internet

Con esta guía tienes el sistema funcionando en un servidor y puedes administrarlo en privado. Para compartir el chat con visitantes, sigue [Publicar la página de un agente](publicacion.md): configura DNS, `PUBLIC_BASE_URL` y HTTPS con la plantilla [deploy/Caddyfile.example](../deploy/Caddyfile.example).

El proxy publica únicamente `/chat/*` y `/public/*`, que sirven la página, los logos y la conversación. La API comprueba que el agente esté publicado; retirar la publicación desactiva su acceso público. El panel y las rutas administrativas permanecen privados. Comprueba la página desde otra conexión a Internet, incluyendo una pregunta real al agente y que `/api/agents` y `/docs` respondan 404 en ese dominio.

La plantilla no expone webhooks. Para Telegram o WhatsApp, configura además sus rutas, registro, tokens y firmas conforme a [integraciones del proyecto](integrations.md). Mantén el panel administrativo por SSH y entra con la contraseña de superusuario. Docker advierte que los puertos publicados pueden eludir reglas de UFW: conserva los enlaces a localhost y revisa el firewall del proveedor. Fuente: [limitaciones de firewall con Docker](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations).

Todos los servicios y datos estarán en una sola máquina: una falla del VPS interrumpe la aplicación. Los respaldos permiten recuperar datos, pero no proporcionan continuidad automática del servicio.

## 14. Si quieres trasladar los datos de Windows

La copia selectiva del paso 5 contiene el programa, no sus datos. Una migración requiere detener la instalación de origen durante un respaldo coherente, transportar los cuatro volúmenes de forma cifrada y restaurarlos en un destino compatible con las mismas versiones. Debes conservar la clave original de `agente_secrets`.

No empieces restaurando solo PostgreSQL sobre una instalación que generó una clave nueva: esa clave nueva no descifra las credenciales anteriores. El destino y su procedimiento de restauración deben prepararse antes de ponerlo en servicio. Mantén una copia intacta del origen hasta comprobar agentes, conversaciones, documentos y credenciales en el destino.

## Comprobación final

- El panel pide la contraseña de superusuario al abrirlo por el túnel y la API responde a la comprobación de salud.
- Una pregunta de prueba funciona con el proveedor y modelo elegidos.
- API y panel siguen publicados solamente en localhost del servidor.
- La instalación tiene una contraseña PostgreSQL nueva, definida antes de inicializar la base.
- Los cuatro volúmenes, el código y la configuración tienen respaldo verificado.
- Hay una copia cifrada fuera del VPS y puedes recuperar su contraseña.
- Se ha ensayado la restauración antes de depender de la instalación para datos importantes.
- El chat publicado muestra el logo, permite conversar y deja de estar accesible al retirar la publicación.
- Si se comparte por Internet, el enlace usa el dominio HTTPS real y el proxy mantiene privados el panel y las rutas administrativas.
- Los canales públicos, si se necesitan, tienen su propia configuración HTTPS y autenticación.
