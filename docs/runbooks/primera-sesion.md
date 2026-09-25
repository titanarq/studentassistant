# Primera sesión real, de principio a fin

Guía para la primera prueba de verdad en este PC (el portátil `Titan`): unos 20 minutos con el
cuaderno o el libro delante de la cámara del portátil, hablando, y después revisar los apuntes en
la web. Complementa a `install.md` (instalación y `doctor` en detalle).

> **Pendiente:** los comandos de voz ("mira aquí", "ya está, prepárame el tema") llegan con
> #47; mientras tanto se usan los botones de `/capture`.

Marcado con **(tú)**: pasos interactivos que tienes que hacer a mano (claves, GitHub, permisos
del navegador, hablar). Lo demás son comandos que se pueden copiar tal cual.

## 0. Antes de empezar

- Navegador: **Google Chrome**. La transcripción usa la Web Speech API, que en Chrome envía el
  audio a Google (hace falta Internet); Firefox no la tiene.
- Abre siempre las páginas como `http://localhost:8765/...` en el propio portátil. La cámara y el
  micrófono solo funcionan en un contexto seguro, y `localhost` lo es; la IP de la red
  (`http://192.168.x.x:8765`) **no**, así que desde ahí el navegador los bloquea.
- Desde `localhost` no hace falta emparejar nada: el backend confía en el propio PC
  (`server.trust_localhost = true`, por defecto).
- Ten a mano la clave de la API de Anthropic (`sk-ant-...`) y la sesión de `gh` iniciada
  (`gh auth status`).

## 1. Instalar y construir la web

```sh
cd ~/projects/studentassistant
git pull
cd backend && uv sync && cd ..
cd web && npm ci && npm run build && cd ..
```

`npm run build` deja la web en `backend/src/studentassistant/server/static/`, que es lo que
sirve el backend. Sin ese paso, `http://localhost:8765/` solo muestra un aviso de "web sin
construir". Repite `npm run build` cada vez que actualices (`git pull`).

## 2. `studentassistant setup` (tú)

```sh
cd ~/projects/studentassistant/backend
uv run studentassistant setup
```

1. **Vault**: elige *crear* uno nuevo. Nombre del repositorio, por ejemplo
   `MatillaM/studentassistant-vault`; `setup` lo crea **privado** en GitHub. Carpeta local: la
   propuesta (`~/StudentAssistant/vault`). **Nunca** uses como vault un repositorio público.
2. **Clave de Anthropic**: pégala cuando la pida (no se ve al escribir). Queda en
   `~/.config/studentassistant/secrets.env` con permisos `600`.
3. **Voz**: con el modo por defecto (`client`) no hay nada que descargar.
4. **Servicio**: acepta; instala y arranca `studentassistant.service` (systemd de usuario).

Límite de gasto recomendado para la prueba: añade a `~/.config/studentassistant/config.toml`

```toml
[llm]
max_usd_per_session = 5
max_usd_per_day = 10
```

y reinicia: `systemctl --user restart studentassistant`. Al llegar al límite el observador se
pausa y el editor pide confirmación antes de gastar más.

Comprueba en GitHub (**tú**) que el repositorio del vault aparece como **Private**.

## 3. `studentassistant doctor`

```sh
uv run studentassistant doctor --api-call
```

Todo debe salir `[ok]` (con Whisper desactivado no aparecen sus líneas). En particular: la clave
(la llamada de prueba es gratuita), "Subida al vault" (el `git push --dry-run`), el puerto 8765 y
el servicio. Si algo falla, la tabla de `install.md` dice qué hacer.

## 4. Servidor en marcha

```sh
systemctl --user status studentassistant
curl -s http://localhost:8765/api/health       # {"status":"ok","protocol_version":"1.3",...}
journalctl --user -u studentassistant -f       # déjalo abierto en una terminal durante la sesión
```

Sin servicio, en una terminal aparte: `uv run studentassistant serve` (Ctrl+C para pararlo).

Emparejar solo hace falta para otro dispositivo (el móvil con la app Android), y se hace **desde
el PC**: `uv run studentassistant pair` (QR en la terminal) o `http://localhost:8765/pair`. El
código vale 5 minutos y un solo uso. Para esta prueba en el portátil no hace falta.

## 5. La sesión (tú, unos 20 minutos)

1. Abre `http://localhost:8765/capture` en Chrome y **permite cámara y micrófono**.
2. Elige o crea la asignatura y el tema (una sesión = un tema). Si el tema tiene una sesión sin
   terminar, puedes pulsar **Continuar la sesión abierta**; si no, **Empezar una sesión nueva**.
3. Habla con normalidad sobre lo que enseñas. La transcripción en directo aparece en gris
   (provisional) y pasa a negro (definitiva).
4. Para cada página: ponla delante de la cámara, bien iluminada y entera, y pulsa **Capturar**
   (hace una ráfaga de 3 fotos; verás un destello y la miniatura pasará de "Subiendo…" a
   "Guardada").
5. **Libro** / **Apuntes** indican de qué es la página siguiente; **Importante** marca el momento.
6. Al acabar, pulsa **Terminar**.

Cuando #47 esté fusionado podrás decir "mira aquí" (captura), "ahora el libro" y "ya está,
prepárame el tema" en lugar de pulsar los botones.

## 6. "Prepárame el tema" y revisión (tú)

1. Abre `http://localhost:8765/` (escritorio de estudio) y entra en el tema, o directamente
   `http://localhost:8765/subjects/<asignatura>/topics/<tema>`.
2. Pulsa **Prepárame el tema**. Opus escribe los apuntes; tarda un poco (minutos, no segundos).
3. `.../notes`: los apuntes con sus fuentes al lado; cada nota a pie lleva a la página o al
   fragmento de la transcripción del que sale.
4. En el chat del editor pide cambios ("demasiado resumido", "pon un ejemplo", "no inventes",
   "¿por qué pusiste esto?"). Cada cambio muestra su diff y se puede deshacer.
5. `.../pending`: las dudas que dejó el observador; respóndelas o descártalas.
6. `.../versions`: historial de los apuntes.

## 7. Coste

```sh
curl -s http://localhost:8765/api/cost        # sesión y día, límites, si el observador está pausado
uv run studentassistant cost                  # por tema, desde el ledger del vault
uv run studentassistant cost --topic <asignatura>/<tema>
```

Si `unpriced_*_calls` no es 0, hay llamadas a un modelo sin precio configurado: el total se queda
corto.

## Qué vigilar

- [ ] **Transcripción en directo**: aparece en `/capture` con poco retraso, en español, y no se
      corta durante minutos (si se para, mira `journalctl`; Chrome a veces pierde el micrófono).
- [ ] **Páginas guardadas**: cada ráfaga queda "guardada" y aparece en
      `~/StudentAssistant/vault/subjects/<asignatura>/topics/<tema>/sources/notes/`
      (`page-NNN.jpg`, `.page.jpg` recortada y, tras unos segundos, `page-NNN.md` con su
      transcripción).
- [ ] **Observador**: en `journalctl` no hay `observer call ... failed`; aparecen dudas
      pendientes (contador en `/capture`, lista en `.../pending`) cuando algo no se entiende.
- [ ] **Calidad de los apuntes**: dicen lo que querías escribir, sin inventar; cada párrafo cita su
      fuente; lo que no se entendía está como duda, no rellenado.
- [ ] **Coste**: `/api/cost` antes y después de "Prepárame el tema"; anota el total de la sesión.
- [ ] **Vault en GitHub**: `curl -s http://localhost:8765/api/vault/status` sin
      `last_push_failure`, y los commits en el repositorio privado.

Anota lo que falle o sorprenda (hora aproximada, qué dijiste o capturaste): son el material para
las siguientes tareas.
