# Instalar Student Assistant en un PC

Cómo pasar de un PC Ubuntu recién instalado a tener el backend en marcha, con tus apuntes (el
*vault*) traídos de GitHub. Si el vault ya existe en otro PC, este mismo procedimiento lo clona y
lo deja todo como estaba.

## Qué necesitas

- Ubuntu con `git`, `python3` (3.12) y [`uv`](https://docs.astral.sh/uv/).
- Una cuenta de GitHub. Lo más cómodo es tener [`gh`](https://cli.github.com/) con la sesión
  iniciada (`gh auth login`); si no, un token en `GH_TOKEN` (o `GITHUB_TOKEN`) con permiso de
  escritura en el repositorio del vault.
- Una clave de la API de Anthropic (`sk-ant-...`), de <https://console.anthropic.com/>.
- Solo si quieres transcribir la voz en el PC (Whisper) en lugar de en el móvil o el navegador:
  una GPU NVIDIA con su controlador instalado. No hace falta instalar CUDA en el sistema: el
  extra `whisper` trae cuBLAS y cuDNN como paquetes de Python.

## 1. Descargar e instalar

```sh
git clone https://github.com/titanarq/studentassistant.git
cd studentassistant/backend
uv sync
cd ../web
npm ci && npm run build
cd ../backend
```

`npm run build` (necesita Node 22) deja la web en `backend/src/studentassistant/server/static/`,
que es lo que sirve el backend; sin él, `http://localhost:8765/` solo muestra un aviso de "web sin
construir".

A partir de aquí, `uv run studentassistant ...` (o activa el entorno con
`source .venv/bin/activate` y escribe solo `studentassistant ...`).

## 2. `studentassistant setup`

```sh
uv run studentassistant setup
```

Te pregunta, en este orden:

1. **Vault**: si quieres *crear* uno nuevo (un repositorio privado nuevo en GitHub) o *clonar* el
   que ya tienes, el repositorio (`propietario/nombre`) y la carpeta local
   (por defecto `~/StudentAssistant/vault`). Queda anotado en
   `~/.config/studentassistant/config.toml`.
2. **Clave de la API de Anthropic**: no se ve mientras la escribes. Se guarda en
   `~/.config/studentassistant/secrets.env`, un archivo que solo puede leer tu usuario
   (permisos `600`). Nunca va al vault ni a `config.toml`. Pulsa Enter para dejarlo para luego.
   Si ya tienes `ANTHROPIC_API_KEY` en el entorno, no se pregunta ni se guarda nada.
3. **Voz**: con la configuración por defecto (modo `client`) transcribe el móvil o el navegador y
   no hay nada que descargar. Si has elegido Whisper en el PC (ver más abajo), descarga el modelo
   aquí; la primera vez tarda un rato.
4. **Servicio**: instala `~/.config/systemd/user/studentassistant.service`, que arranca
   `studentassistant serve` al iniciar sesión y lo reinicia si falla, y lo pone en marcha.

Sin preguntas (por ejemplo, desde un script):

```sh
printf '%s\n' "$MI_CLAVE" | uv run studentassistant setup \
  --vault-repo ana/vault --path ~/StudentAssistant/vault --clone --api-key-stdin
```

`--no-service` lo hace todo menos el servicio. Volver a lanzar `setup` con las mismas respuestas
no cambia nada, así que es seguro repetirlo.

Para que el servicio siga en marcha aunque cierres la sesión (o arranque con el PC sin iniciar
sesión), una sola vez:

```sh
sudo loginctl enable-linger "$USER"
```

## 3. `studentassistant doctor`

```sh
uv run studentassistant doctor              # sin gastar nada
uv run studentassistant doctor --api-call   # además prueba la clave con la API (llamada gratuita)
```

Una línea por comprobación, `[ok]`, `[aviso]` o `[FALLO]`; si algo falla, termina con código 1.

| comprobación | qué mira | si falla |
|---|---|---|
| Configuración | que `config.toml` se pueda leer | corrige el valor que indica |
| Dependencias de Python | que estén instaladas | `uv sync` en `backend/` (con Whisper, `uv sync --extra whisper`) |
| Voz (STT) | modo y proveedor configurados | revisa `[stt]` en `config.toml` |
| faster-whisper, CUDA, Modelo de Whisper | solo con `faster-whisper`: instalado, GPU visible y cuBLAS/cuDNN funcionando en ella, modelo descargado | ver "Whisper en el PC" |
| Clave de la API de Anthropic | que haya clave (y, con `--api-call`, que Anthropic la acepte) | `setup --api-key-stdin`; `chmod 600` si lo pide |
| Vault | que la carpeta sea un vault | `setup` |
| Remoto del vault | que `origin` sea el repositorio de `vault.repo` | `setup` |
| Subida al vault | `git push --dry-run`: que puedas subir cambios | `gh auth login` o un token con escritura |
| Puerto | que `server.port` (8765) esté libre o lo use el propio backend | cambia `server.port` |
| Servicio | que `studentassistant.service` esté activo | `setup`, o mira `journalctl --user -u studentassistant` |

## Whisper en el PC (opcional)

Por defecto la voz la transcribe el cliente (Google, en el móvil o en el navegador). Para
transcribirla en el PC con faster-whisper:

```sh
cd studentassistant/backend
uv sync --extra whisper
```

y en `~/.config/studentassistant/config.toml`:

```toml
[stt]
mode = "server"
provider = "faster-whisper"

[stt.options.faster-whisper]
model = "large-v3-turbo"   # el modelo que se descarga
device = "auto"            # auto: la GPU si hay, si no la CPU (lento); cuda; cpu
# download_root = "/ruta/a/los/modelos"   # por defecto, la caché de Hugging Face
```

El extra instala también las bibliotecas de CUDA 12 que usa faster-whisper en la GPU
(`nvidia-cublas-cu12` y `nvidia-cudnn-cu12`, cuDNN 9) dentro del entorno de `backend/`; el backend
las carga desde ahí, sin `LD_LIBRARY_PATH` ni paquetes del sistema. Basta con el controlador de
NVIDIA (`nvidia-smi` debe ver la GPU).

Después, `uv run studentassistant setup` descarga el modelo y `doctor` comprueba la GPU (crea un
contexto de cuBLAS y otro de cuDNN en ella) y el modelo. Si la línea `CUDA` dice que no se
encuentra `libcublas.so.12` o `libcudnn.so.9`, vuelve a lanzar `uv sync --extra whisper`; con
`device = "auto"` es un aviso y Whisper usará la CPU (lento), con `device = "cuda"` es un fallo. (Ojo: un `uv sync` sin `--extra whisper` desinstala faster-whisper; con Whisper,
usa siempre `uv sync --extra whisper`.)

## El día a día

```sh
systemctl --user status studentassistant           # ¿está en marcha?
systemctl --user restart studentassistant          # tras cambiar config.toml o actualizar
journalctl --user -u studentassistant -f           # registro en directo
uv run studentassistant pair                       # emparejar el móvil (código QR)
```

Actualizar: `git pull && uv sync` en `backend/` (`uv sync --extra whisper` si usas Whisper en el
PC), `npm ci && npm run build` en `web/` y `systemctl --user restart studentassistant`.

Quitar el servicio:

```sh
systemctl --user disable --now studentassistant
rm ~/.config/systemd/user/studentassistant.service
systemctl --user daemon-reload
```
