# Student Assistant — Visión del producto

> **«Me siento con mi cuaderno y mi móvil durante veinte minutos y termino con unos apuntes
> digitales que realmente representan lo que yo quería escribir.»**

Ese es el criterio de éxito del primer prototipo. Todo lo demás (quiz, flashcards, exámenes,
diapositivas, tutor) se construye encima de unos apuntes maestros fieles.

## 1. El uso real

Llego a casa con mis apuntes en papel (y a veces el libro, un PDF del profesor o una web) y
convierto ese material en buenos apuntes digitales **conversando** con el sistema mientras le
enseño las páginas con el móvil.

No es OCR + LLM. El sistema dispone de tres cosas a la vez, alineadas en el tiempo:

```text
lo que VE (fotos de las páginas)  +  lo que LEE (transcripción de la página)
                     +  lo que TÚ LE EXPLICAS (voz)
```

El timestamp une voz e imagen: «mira aquí» a las 00:02:34 → `page-004.jpg`. No hace falta visión
artificial compleja para saber qué estás señalando: **tú se lo dices en lenguaje natural.**

## 2. El concepto central: la mesa de estudio

No estás grabando una clase: inicias una **sesión de estudio** sobre un **tema** de una
**asignatura**. Un tema acumula sesiones, fuentes y material generado, y se puede retomar otro
día («continúa el tema de la Revolución Francesa»):

```text
Historia / Tema 4 — La Revolución Francesa
Fuentes:      ✓ 6 páginas manuscritas  ✓ págs. 82-94 del libro  ✓ PDF del profesor  ✓ 2 webs
Sesiones:     ✓ 2 (31 min de conversación)
Pendiente:    4 dudas por revisar
Material:     ✓ Apuntes v3   ○ Esquema   ○ Quiz   ○ Flashcards   ○ Examen   ○ Diapositivas
```

## 3. Flujo

```text
Cuaderno / libro / PDF / web
        │
        ▼
MÓVIL (app Android «tonta»)
  ├─ audio continuo ──────────────► transcripción local (Whisper en la GPU del PC)
  ├─ fotos en alta resolución ────► sólo cuando lo pides («mira aquí», botón)
  └─ eventos (importante, libro/apuntes, terminar)
        │  WebSocket + HTTP en la LAN
        ▼
BACKEND UBUNTU (toda la inteligencia)
  ├─ STT + comandos de voz deterministas
  ├─ fuentes: selección de la foto más nítida, recorte, transcripción de la página
  ├─ OBSERVADOR (Claude Sonnet): memoria rápida, clasifica lo que le enseñas en vivo
  ├─ EDITOR-TUTOR (Claude Opus): construye contigo el apunte maestro
  └─ BÓVEDA: todo se guarda en un repositorio git privado en GitHub
        │
        ▼
WEB DE REVISIÓN (navegador del PC, también usable en el móvil)
  apuntes con procedencia · fuentes al lado · chat con el editor · bandeja de dudas
        │
        ▼
APUNTES DEFINITIVOS ──► esquema · quiz · flashcards · ejercicios/examen · diapositivas
```

## 4. Experiencia de una sesión (MVP)

1. En el móvil: `Historia / Tema 4` → **Empezar sesión**. Móvil sobre la mesa apuntando al cuaderno.
2. Hablas con normalidad: «Esta es la primera página», «aquí el profesor explicó la diferencia
   entre absolutismo y liberalismo», «esta flecha conecta con esto», «esta palabra no sé qué pone,
   creo que es *soberanía*».
3. «Mira aquí» (o el botón 📷) → el móvil hace una ráfaga de fotos en alta resolución, vibra y
   hace «clic» para que sepas que está capturado. El backend se queda con la más nítida.
4. «Siguiente» → nueva página. «Ahora el libro» → cambia el contexto de fuente a *libro*.
5. En la pantalla ves la transcripción en vivo y un contador discreto de **dudas pendientes**.
   El sistema nunca te interrumpe durante la captura.
6. «Ya está, prepárame el tema» → termina la sesión y Opus genera la primera versión.
7. En el PC abres la web: los apuntes, cada párrafo con su procedencia (clic → ves tu hoja
   manuscrita o el minuto de la conversación). Revisas conversando:
   «esta parte está demasiado resumida», «pon un ejemplo aquí», «no inventes nada que no esté en
   mis fuentes», «esta explicación del libro es mejor, usa esa».
8. Resolución de dudas: Opus resuelve solo lo que puede con tus fuentes (y te dice de dónde lo ha
   sacado) y te pregunta el resto, una a una: «Hay una contradicción entre tus apuntes y el libro
   sobre esta fecha, ¿cuál uso? ¿conservo una nota con lo que pone en tus apuntes?».
9. Cada cambio queda versionado (Apuntes v1, v2, v3…) y sincronizado con GitHub.

## 5. Mejoras sobre la idea original

Estas decisiones refinan la idea inicial; las vinculantes están en `docs/adr/`.

1. **Sin streaming de vídeo al servidor.** El móvil sólo envía audio continuo y **fotos
   puntuales en alta resolución** (CameraX), no fotogramas de vídeo. Más nitidez para leer
   letra manuscrita, muchísimo menos ancho de banda y coste, y el backend no necesita decodificar
   vídeo. La vista de cámara sólo existe en la pantalla del móvil. (La captura automática por
   cambio de página queda para más adelante.)
2. **Ráfaga + la más nítida.** Cada captura son 3 fotos; el backend elige la más nítida
   (varianza del laplaciano), la recorta/endereza como un escáner y conserva también el original.
3. **Comandos de voz deterministas.** «Mira aquí», «siguiente», «importante», «ahora el libro»,
   «ya está, prepárame el tema» se detectan con una gramática configurable sobre la transcripción,
   sin LLM: latencia mínima, coste cero y comportamiento predecible. Sonnet se reserva para
   *entender*, no para *obedecer*. Cada comando tiene también su botón.
4. **Transcripción de cada página como fuente derivada.** Al capturar, Sonnet (visión) pasa la
   página a Markdown conservando la estructura (flechas y esquemas como listas anidadas o
   diagramas), marca las palabras dudosas `[[?soberanía]]` y usa lo que dijiste alrededor de la
   foto como pista. Opus trabaja sobre ese texto y sólo vuelve a mirar la imagen cuando hace falta.
5. **Sesión basada en eventos.** Todo lo que ocurre (segmentos de voz, capturas, comandos,
   operaciones del observador) es un registro *append-only*. El estado del observador es el
   plegado de esos eventos: reproducible, auditable y recuperable en otro PC. Eso es, en la
   práctica, «el estado de los LLM»: los modelos no guardan estado; lo guardamos nosotros.
6. **Procedencia en cada párrafo.** El apunte maestro es Markdown con notas al pie que apuntan a
   la fuente exacta (página manuscrita, minuto de la conversación, página del libro, URL). Lo que
   la IA añade sin respaldo en tus fuentes queda marcado como «ampliado por la IA». Modo
   **estricto** (sólo tus fuentes) o **ampliado**. Así se puede preguntar meses después
   «¿por qué pusiste esto?» y el sistema vuelve a mirar la hoja.
7. **Bandeja de dudas.** Durante la captura se acumulan (sin interrumpir): palabra ilegible,
   concepto mencionado pero no explicado, información incompleta, posible error, contradicción
   entre fuentes. Al final se autoresuelven con evidencia o se preguntan.
8. **Bóveda separada del código.** El contenido vive en *otro* repositorio git privado (la
   bóveda); el código de la app en este. En un PC nuevo: instalar la app →
   `studentassistant setup` → indicar el repo de la bóveda → se clona y se reconstruye el índice
   local. Git es la fuente de verdad; la base de datos SQLite local es sólo una caché derivada.
9. **Versiones = git.** «Apuntes v3» es una etiqueta git; se pueden ver diferencias entre
   versiones y volver atrás. Los ficheros *append-only* se fusionan sin conflictos (`merge=union`).
10. **Captura resiliente.** Si se cae la Wi-Fi, el móvil sigue grabando en local y sube lo
    pendiente al reconectar; ninguna foto ni minuto de voz se pierde.
11. **Simulador de sesiones.** `studentassistant replay` reproduce una sesión grabada (audio +
    fotos con tiempos) por el mismo camino que un móvil real: el backend se desarrolla y se
    prueba sin tener el móvil en la mano, y sirve de test de extremo a extremo.
12. **Guía de estilo por asignatura.** Lo que pides en las revisiones («me gustan las tablas para
    comparar», «siempre un ejemplo») se guarda y se aplica a los temas siguientes.
13. **Coste visible.** Cada llamada a Claude se apunta en un libro de costes del tema; la web
    muestra cuánto ha costado cada sesión y hay topes configurables.
14. **Revisión en la web, no en el móvil.** Leer y editar un tema largo es mucho mejor en la
    pantalla del PC; la misma web es *responsive* y se puede abrir desde el móvil.

## 6. Dos mundos: fuentes y conocimiento

```text
FUENTES (nunca se borran)                     CONOCIMIENTO (lo genera la IA contigo)
────────────────────────                      ──────────────────────────────────────
sources/notes/page-001.jpg  (+ .md, .yaml)    notes/apuntes.md
sources/book/page-083.jpg                       1. Contexto
sources/pdf/profesor.pdf                        2. Causas  (2.1 Económicas, 2.2 Sociales…)
sources/web/001-estados-generales.md            3. Estados Generales
sessions/<fecha>/transcript.jsonl             generated/quiz.yaml, flashcards.apkg, …
sessions/<fecha>/events.jsonl
```

## 7. Roles de los modelos

| Rol | Modelo por defecto | Qué hace |
|---|---|---|
| Observador («memoria rápida») | Claude Sonnet (`claude-sonnet-5`) | En vivo: clasifica lo que enseñas en secciones, conceptos y fuentes; enlaza fotos con lo que dijiste; acumula dudas. Transcribe las páginas. |
| Editor-tutor | Claude Opus (`claude-opus-5`; `claude-opus-5-5` configurable) | Bajo demanda: genera el apunte maestro, lo edita contigo, resuelve dudas, explica el porqué de cada párrafo, genera el material de estudio. |
| Transcripción de voz | faster-whisper local (GPU del PC) | Voz → texto en español con marcas de tiempo, sin salir del PC. |

Los modelos se configuran por rol; no hay ningún identificador de modelo escrito en el código.

## 8. Fases

| Fase | Objetivo | Resultado |
|---|---|---|
| 0. Fundaciones | Monorepo, CI, contrato móvil↔PC, bóveda | Esqueletos que compilan y se prueban |
| 1. Esqueleto andante | Móvil emparejado, sesión, audio → transcripción en vivo, fotos a la bóveda, push a GitHub | Sesión completa **sin IA** |
| 2. Observador | Sonnet mantiene el estado de la sesión, transcribe páginas, bandeja de dudas | La sesión «se entiende» |
| 3. Editor + web | Opus genera y edita el apunte maestro con procedencia; web de revisión | **MVP: el objetivo de 20 minutos** |
| 4. Más fuentes | Libro, PDF, búsqueda web | Apuntes enriquecidos |
| 5. Generadores | Esquema, quiz, flashcards, ejercicios/examen, diapositivas | Material de estudio |
| 6. Modo estudio | Práctica, repetición espaciada, tutor por voz | Estudiar con el sistema |

El plan de desarrollo detallado está en `docs/PLAN.md` y el trabajo en el tablero de GitHub.

## 9. Fuera de alcance (por ahora)

- Grabar clases en directo en el aula.
- Uso fuera de la red de casa (se podrá documentar Tailscale más adelante).
- iOS.
- Varios usuarios simultáneos sobre la misma bóveda (un estudiante, varios PCs).

## 10. Preguntas abiertas (a resolver durante el refinamiento)

- ¿Se guarda el audio en la bóveda (Opus ~10 MB/h, en Git LFS) o sólo la transcripción? Por
  defecto: sólo transcripción; audio local opcional.
- Imágenes en git normal o Git LFS cuando la bóveda crezca (umbral a medir).
- ¿Un perfil de estudiante o varios (p. ej. hermanos) en el mismo PC?
- Opus 5 u Opus 5.5 como editor por defecto (coste/calidad a medir con sesiones reales).
