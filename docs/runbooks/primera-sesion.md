# Primera sesión real, de principio a fin

Guía para la primera prueba de verdad en este PC (el portátil `Titan`): unos 20 minutos con el
cuaderno o el libro delante de la cámara del portátil, hablando, y después revisar los apuntes en
la web. Complementa a `install.md` (instalación y `doctor` en detalle).

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

1. Abre `http://localhost:8765/` en Chrome: es la **Mesa de estudio**.
2. Si todavía no tienes la asignatura, escríbela en **Nueva asignatura** y pulsa **Crear**.
3. En su recuadro pulsa **Nuevo tema**, escribe el nombre y pulsa **Crear** (una sesión = un tema).
   Se abre el espacio de estudio del tema (**Construir**). Cada tema de la mesa tiene dos botones,
   **Construir** y **Estudiar**; para seguir con uno que ya existe pulsa **Construir**.
4. En la pestaña **Captura** pulsa **Empezar una sesión nueva** (o **Continuar la sesión abierta**
   si el tema tiene una sin terminar) y **permite cámara y micrófono**.
5. Habla con normalidad sobre lo que enseñas. La transcripción en directo aparece en gris
   (provisional) y pasa a negro (definitiva).
6. Para cada página: ponla delante de la cámara, bien iluminada y entera, y pulsa **Capturar**
   (hace una ráfaga de 3 fotos; verás un destello y la miniatura pasará de "Subiendo…" a
   "Guardada"). Las páginas guardadas aparecen en la pestaña **Recursos**; cambiar de pestaña no
   para la captura.
7. **Libro** / **Apuntes** indican de qué es la página siguiente; **Importante** marca el momento.
8. **Terminar** (en la app Android, **Terminar captura**) cierra la sesión de captura y nada
   más: ni la web ni la app preparan los apuntes al terminar. Los apuntes del tema se piden en el
   chat de «Construir» («prepárame el tema», paso 6). Si capturas desde la página suelta
   `/capture` o desde la app, al terminar verás «Sesión terminada» con **Abrir en Construir**,
   que lleva al espacio de estudio del tema.

Decir "mira aquí", "mira esto", "captura" o "haz foto" también dispara una captura, igual que
"siguiente" o "pasamos página" pasa a la página siguiente. El resto de frases de la gramática
("ahora el libro", "vuelvo a mis apuntes", "ahora el pdf", "esto es importante" / "importante",
"pausa", "reanuda", "busca en internet ...") por ahora solo quedan grabadas como eventos
`voice.command` de la sesión, sin disparar ninguna acción por sí solas: para cambiar de fuente o
marcar un momento importante sigue haciendo falta pulsar **Libro** / **Apuntes** / **Importante**.

Lo que le pides al asistente hablando (o escribiendo en el chat del espacio de estudio) sí lo
entiende y lo lleva a cabo: "ya está, prepárame el tema", "incorpora la página 3", "aparta la 9"
o un cambio en los apuntes se detectan y se ejecutan solos (modo por defecto `[observer]
request_detection = "observer"`; con `wake_word` hace falta empezar diciendo "anel"). Cada
petición y su respuesta aparecen en el chat, y los apuntes se van escribiendo en el documento de
la derecha.

## 6. Revisar y pasar a estudiar (tú)

1. En el espacio de estudio, pide "ya está, prepárame el tema" si no lo has pedido ya. Opus
   escribe los apuntes; tarda un poco (minutos, no segundos).
2. Revisa el documento: cada nota a pie abre en **Recursos** la página o el fragmento del que
   sale. Pide cambios en el chat ("demasiado resumido", "pon un ejemplo", "no inventes", "¿por qué
   pusiste esto?") o pulsa **Editar** para cambiarlos tú.
3. Las dudas que deja el observador aparecen en el chat; respóndelas ahí ("la 2", "pone
   «escrita»", "descártala"). El contador de dudas pendientes está en la cabecera.
4. Cuando estén bien, di o escribe **"ya está, quiero estudiar"**: el asistente cierra la
   captura, marca la versión de estudio y ofrece **Ir a Estudiar**, que abre la pantalla
   **Estudiar** (esquema, ejercicios, examen, quiz, tarjetas y preguntas sobre los apuntes). El
   selector **Construir · Estudiar** de la cabecera hace lo mismo.
5. Desde la mesa, el botón **Estudiar** de cada tema abre directamente esa pantalla, y el nombre
   del tema abre su ficha (resumen, PDF, páginas web, libro de texto, coste y descargas).

## 7. Coste

```sh
curl -s http://localhost:8765/api/cost        # sesión y día, límites, si el observador está pausado
uv run studentassistant cost                  # por tema, desde el ledger del vault
uv run studentassistant cost --topic <asignatura>/<tema>
```

Si `unpriced_*_calls` no es 0, hay llamadas a un modelo sin precio configurado: el total se queda
corto.

## Qué vigilar

- [ ] **Transcripción en directo**: aparece en la pestaña **Captura** del espacio de estudio con
      poco retraso, en español, y no se corta durante minutos (si se para, mira `journalctl`; Chrome a veces pierde el micrófono).
- [ ] **Páginas guardadas**: cada ráfaga queda "guardada" y aparece en
      `~/StudentAssistant/vault/subjects/<asignatura>/topics/<tema>/sources/notes/`
      (`page-NNN.jpg`, `.page.jpg` recortada y, tras unos segundos, `page-NNN.md` con su
      transcripción).
- [ ] **Observador**: en `journalctl` no hay `observer call ... failed`; aparecen dudas
      pendientes (en el chat y en el contador de la cabecera del espacio de estudio) cuando algo no
      se entiende.
- [ ] **Calidad de los apuntes**: dicen lo que querías escribir, sin inventar; cada párrafo cita su
      fuente; lo que no se entendía está como duda, no rellenado.
- [ ] **Coste**: `/api/cost` antes y después de "prepárame el tema"; anota el total de la sesión.
- [ ] **Estudiar**: "ya está, quiero estudiar" abre la pantalla Estudiar con la versión de
      estudio, y el tema se abre desde la mesa con sus botones **Construir** y **Estudiar**.
- [ ] **Vault en GitHub**: `curl -s http://localhost:8765/api/vault/status` sin
      `last_push_failure`, y los commits en el repositorio privado.

Anota lo que falle o sorprenda (hora aproximada, qué dijiste o capturaste): son el material para
las siguientes tareas.
