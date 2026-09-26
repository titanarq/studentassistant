# Instalar Student Assistant en un PC

Cómo pasar de un PC Ubuntu recién instalado a tener el backend en marcha, con tus apuntes (el
*vault*) traídos de GitHub. Si el vault ya existe en otro PC, este mismo procedimiento lo clona y
lo deja todo como estaba.

## Qué necesitas

- Ubuntu con `git`, `python3` (3.12) y [`uv`](https://docs.astral.sh/uv/).
- Una cuenta de GitHub. Lo más cómodo es tener [`gh`](https://cli.github.com/) con la sesión
  iniciada (`gh auth login`); si no, un token en `GH_TOKEN` (o `GITHUB_TOKEN`) con permiso de
  escritura en el repositorio del vault.
- Para hablar con Claude, una de las dos:
  - [Claude Code](https://docs.claude.com/en/docs/claude-code) instalado (`claude` en el `PATH`)
    y con la sesión iniciada con tu suscripción (`claude auth login`). Es lo que se usa si no hay
    clave de la API en el PC.
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
   Si ya tienes `ANTHROPIC_API_KEY` en el entorno, no se pregunta ni se guarda nada. Sin clave,
   Claude se usa a través de Claude Code con tu suscripción (`[llm] backend = "auto"`, el valor
   por defecto); con `[llm] backend = "claude-code"` en `config.toml` ni siquiera se pregunta.
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
uv run studentassistant doctor --fix        # además repara cómo sube el servicio el vault a GitHub
```

Una línea por comprobación, `[ok]`, `[aviso]` o `[FALLO]`; si algo falla, termina con código 1.

| comprobación | qué mira | si falla |
|---|---|---|
| Configuración | que `config.toml` se pueda leer | corrige el valor que indica |
| Dependencias de Python | que estén instaladas | `uv sync` en `backend/` (con Whisper, `uv sync --extra whisper`) |
| Voz (STT) | modo y proveedor configurados | revisa `[stt]` en `config.toml` |
| faster-whisper, CUDA, Modelo de Whisper | solo con `faster-whisper`: instalado, GPU visible y cuBLAS/cuDNN funcionando en ella, modelo descargado | ver "Whisper en el PC" |
| Clave de la API de Anthropic | con el backend `api`: que haya clave (y, con `--api-call`, que Anthropic la acepte) | `setup --api-key-stdin`; `chmod 600` si lo pide |
| Claude Code | con el backend `claude-code` (o `auto` sin clave): que `claude` esté en el `PATH` y con la sesión iniciada (`claude auth status`, sin gastar nada) | instala Claude Code; `claude auth login` |
| Vault | que la carpeta sea un vault | `setup` |
| Remoto del vault | que `origin` sea el repositorio de `vault.repo` | `setup` |
| Subida al vault | `git push --dry-run`: que puedas subir cambios | `gh auth login` o un token con escritura |
| Acceso del servicio a GitHub | `git ls-remote origin` como lo hace el servicio: sin terminal, sin ventana de contraseña, sin el `PATH` de tu shell | `doctor --fix` (con `gh auth login` hecho) |
| Credenciales del vault | solo con `--fix`: guarda en `.git/config` del vault que git use `gh auth git-credential` con la ruta completa de `gh` | `gh auth login` |
| Puerto | que `server.port` (8765) esté libre o lo use el propio backend | cambia `server.port` |
| Servicio | que `studentassistant.service` esté activo | `setup`, o mira `journalctl --user -u studentassistant` |
| Marp CLI (diapositivas) | que `generators.marp_command` (`marp`) esté en el `PATH` y responda a `--version`; si no, solo es un aviso: las diapositivas se generan pero sin PDF ni PPTX | `npm install -g @marp-team/marp-cli` (necesita Node.js y Chrome o Chromium) |

### El servicio no puede subir el vault

Si `journalctl --user -u studentassistant` muestra `vault push failed (auth)` y la página de
captura dice que no se pudo conectar a GitHub, el servicio no sabe autenticarse: `setup` usa la
sesión de `gh` de tu terminal, pero el servicio no tiene tu `PATH` (por ejemplo, `gh` de Homebrew
en `/home/linuxbrew/.linuxbrew/bin`). Desde hace #308, `setup` guarda en el `.git/config` del
propio vault (no en tu configuración global) esta línea, que no contiene ningún token:

```ini
[credential "https://github.com"]
	helper =
	helper = !'/ruta/completa/a/gh' auth git-credential
```

Para un vault preparado antes, o si mueves `gh` de sitio:

```sh
uv run studentassistant doctor --fix
GIT_TERMINAL_PROMPT=0 git -C ~/StudentAssistant/vault ls-remote origin   # debe listar ramas
systemctl --user restart studentassistant
```

Con un token (`GH_TOKEN`) en lugar de `gh` no se guarda nada: el servicio necesitaría el token
en su entorno, así que lo recomendable es instalar `gh` y `gh auth login`.

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
