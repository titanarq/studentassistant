# Evaluar la canalización con tus propias sesiones

`studentassistant eval run` repite sesiones reales que grabaste, con llamadas reales a Claude, y
mide lo fieles que son la transcripción de las páginas, las secciones del observador, las
peticiones al asistente que detecta y los apuntes del editor frente a lo que tú escribiste como referencia. Sirve para comparar antes y
después de cambiar un prompt o un modelo. La rúbrica exacta está en `docs/modules/infra.md`
("Evals").

## 1. Dónde vive el conjunto

En `[eval] path` de `~/.config/studentassistant/config.toml` (por defecto
`~/StudentAssistant/evals`). **Nunca** dentro del repositorio del código ni de la bóveda: son
tus grabaciones, privadas; el comando se niega a usar una carpeta dentro de cualquiera de los dos.

```toml
[eval]
path = "~/StudentAssistant/evals"
speed = 4.0   # cuántas veces más rápido que la grabación se repite cada sesión
```

## 2. Preparar un caso

1. Graba una sesión normal con `studentassistant serve --record`: queda en
   `~/.cache/studentassistant/recordings/<sesión>/`.
2. Crea la carpeta del caso y copia la grabación dentro:

   ```sh
   mkdir -p ~/StudentAssistant/evals/celula
   cp -r ~/.cache/studentassistant/recordings/<sesión> ~/StudentAssistant/evals/celula/recording
   ```

3. Escribe la referencia en `celula/reference/`:
   - `notes.md` (**obligatorio**): los apuntes que de verdad querías, en Markdown libre (sin
     notas al pie ni anclas: basta con títulos, párrafos y listas).
   - `pages/<capture_id>.md` (opcional): lo que pone de verdad cada página fotografiada. El
     `capture_id` está en `recording/captures.jsonl`.
   - `sections.yaml` (opcional): a qué sección pertenece cada frase dictada, con los
     `segment_id` de `recording/transcript.jsonl`. Las frases que no pongas no cuentan.

     ```yaml
     sections:
       - title: Definición
         segments: [seg-1]
       - title: Partes
         segments: [seg-2, seg-3]
     ```

   - `requests.yaml` (opcional): las peticiones que le hiciste al asistente hablando («anel,
     haz una tabla…», «prepárame el tema», «incorpora la página 3»…). Cada una lleva su tipo
     (`kind`), los `segment_id` de las frases en que la dijiste y, si quieres, una nota que
     aparecerá en el informe. Los tipos son `edit` (cambiar los apuntes), `question`
     (preguntar algo), `prepare_notes` («prepárame el tema»), `incorporate`, `set_aside`,
     `restore` (incorporar, apartar o recuperar páginas), `doubt_answer` (responder una duda
     del chat) y `study` («ya está, quiero estudiar»). Un tipo desconocido o un segmento que no
     está en la grabación hacen que el caso no se pueda leer. Sin este fichero el caso no
     puntúa las peticiones.

     ```yaml
     requests:
       - kind: edit
         segments: [seg-7]
         note: pon las tres partes en una tabla
       - kind: prepare_notes
         segments: [seg-12, seg-13]
     ```

     Una petición detectada acierta cuando es del mismo tipo y comparte al menos una frase con
     una de la referencia (cada una cuenta una sola vez).

## 3. Ejecutar

```sh
studentassistant eval run            # todos los casos
studentassistant eval run --case celula
```

Primero enseña el **coste estimado** por caso y por papel (observador, detector de peticiones,
transcriptor, editor) y pregunta antes de llamar a Claude; `--yes` no pregunta. Cada caso se repite en una bóveda nueva
y propia (sin remoto), nunca en la tuya.

Dos opciones de `[eval]` eligen qué se mide (quedan anotadas en el informe):

```toml
[eval]
request_detection = "observer"   # o "wake_word"; sin ponerla, lo que diga [observer]
notes_path = "generate"          # o "chat"
```

- `request_detection`: quién detecta tus peticiones en esta ejecución, sin tocar la
  configuración de la aplicación: `observer` (Sonnet lee la transcripción) o `wake_word` (solo
  lo que empieza por «anel», sin llamadas a Claude).
- `notes_path`: qué apuntes se puntúan. Con `generate` (por defecto), al acabar la sesión se
  pide «prepárame el tema» y se puntúan esos apuntes. Con `chat` no se pide nada al final: se
  puntúan los apuntes que construyeron tus propias peticiones durante la sesión (incorporar
  páginas, editar…), después de esperar a que terminen todas. Si no hiciste ninguna que los
  escribiera, el caso no tiene apuntes y «conservado» y «con fuente» cuentan 0.

## 4. Leer el informe

En `<path>/runs/<fecha>/`:

- `report.md`: la tabla de puntuaciones y, por caso, las páginas, las secciones, las
  peticiones (precisión, exhaustividad y F1, en total y por tipo, con las que no se detectaron
  y las que se detectaron sin estar en tu referencia), las ideas de referencia que faltan y las
  ideas generadas sin apoyo en tus fuentes. Arriba dice qué detector y qué apuntes se usaron.
- `report.json`: lo mismo en JSON, para comparar con otra ejecución.
- `<caso>/vault/`: la bóveda que produjo el caso, para mirar qué pasó.

Todas las puntuaciones van de 0 a 100 %, más es mejor. Son medidas léxicas y deterministas: una
bajada señala dónde mirar, no un veredicto; lee las ideas que faltan o sobran antes de concluir.

## 5. Comparar con la ejecución anterior

Cada informe termina con la sección **«Comparación con la ejecución anterior»**: se compara con
la ejecución más reciente de `runs/` que sea anterior a esta. Por caso y por puntuación (páginas
por caracteres y por palabras, acuerdo y cobertura de secciones, precisión, exhaustividad y F1
de las peticiones, conservado, con fuente y global) muestra el valor anterior, el nuevo y la diferencia en puntos. Una bajada de más de
`[eval] regression_margin` (por defecto `0.05`, es decir, 5 puntos) se marca como
**regresión**. También se listan los casos nuevos y los que ya no están. Si un informe anterior
no se puede leer, se avisa y se usa el anterior a él. Las regresiones se informan, pero no hacen
fallar el comando.

```toml
[eval]
regression_margin = 0.05
```

Para comparar dos ejecuciones cualesquiera ya hechas, sin llamar a Claude:

```sh
studentassistant eval compare 20260901-101500 20260925-093000
```

Cada ejecución es el nombre de su carpeta en `runs/` o una ruta a ella; la primera es la
anterior y la segunda la nueva.

## 6. Comparar los dos detectores de peticiones

Con los mismos casos (y su `requests.yaml`), haz dos ejecuciones cambiando solo el detector:

```sh
SA_EVAL__REQUEST_DETECTION=observer studentassistant eval run
SA_EVAL__REQUEST_DETECTION=wake_word studentassistant eval run
studentassistant eval compare <primera> <segunda>
```

La comparación dice qué detector usó cada ejecución y, por caso, cómo cambian la precisión (lo
que detectó y era de verdad una petición), la exhaustividad (las peticiones que no se le
escaparon) y el F1. Mira también el coste: `wake_word` no llama a Claude para detectar. Para
comparar cómo quedan los apuntes construidos desde el chat, repite con
`SA_EVAL__NOTES_PATH=chat`.
