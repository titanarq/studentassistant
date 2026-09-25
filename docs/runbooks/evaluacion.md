# Evaluar la canalización con tus propias sesiones

`studentassistant eval run` repite sesiones reales que grabaste, con llamadas reales a Claude, y
mide lo fieles que son la transcripción de las páginas, las secciones del observador y los
apuntes del editor frente a lo que tú escribiste como referencia. Sirve para comparar antes y
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

## 3. Ejecutar

```sh
studentassistant eval run            # todos los casos
studentassistant eval run --case celula
```

Primero enseña el **coste estimado** por caso y por papel (observador, transcriptor, editor) y
pregunta antes de llamar a Claude; `--yes` no pregunta. Cada caso se repite en una bóveda nueva
y propia (sin remoto), nunca en la tuya.

## 4. Leer el informe

En `<path>/runs/<fecha>/`:

- `report.md`: la tabla de puntuaciones y, por caso, las páginas, las secciones, las ideas de
  referencia que faltan y las ideas generadas sin apoyo en tus fuentes.
- `report.json`: lo mismo en JSON, para comparar con otra ejecución.
- `<caso>/vault/`: la bóveda que produjo el caso, para mirar qué pasó.

Todas las puntuaciones van de 0 a 100 %, más es mejor. Son medidas léxicas y deterministas: una
bajada señala dónde mirar, no un veredicto; lee las ideas que faltan o sobran antes de concluir.
