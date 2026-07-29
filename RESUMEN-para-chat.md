# Etiquetado de inundaciones — estado actual y la pregunta de hostearlo

> Resumen autocontenido para discutir la arquitectura con alguien que no vio el código.

---

## 1. Qué existe hoy

Una **app de escritorio de etiquetado manual por píxel** sobre imágenes satelitales, para
generar datos de entrenamiento de un modelo de detección de inundaciones.

- `flood_label.py` — ~1000 líneas, Python + matplotlib con backend TkAgg + rasterio.
  Es la herramienta activa.
- `label.py` — ~900 líneas, la versión anterior del mismo concepto pero para **sargazo**
  (con overlay de FAI, Floating Algae Index). De ahí salió el diseño de la de inundaciones.
- Sin git. Sin base de datos. Sin server. Dependencias: numpy, pandas, matplotlib,
  rasterio, scipy. Nada de cloud, web ni DB en ningún repo relacionado.

**Cómo funciona:** abre GeoTIFFs de una carpeta, detecta si cada uno es SAR u óptico
leyendo los nombres de banda del archivo, los renderiza de la mejor forma posible para
interpretarlos a ojo, y guarda lo que la persona dibuja. La app **no clasifica nada**:
todo el criterio es humano.

**4 clases:** `water`, `dry_land`, `cloud` (solo óptico), `radar_shadow` (sombra/layover
de relieve — se ve oscuro como el agua pero no es agua; es el falso positivo principal
en SAR).

**4 herramientas de dibujo:** lazo a mano alzada, polígono por vértices, pincel circular
con radio ajustable, y click de píxel suelto. Muestra hectáreas en vivo mientras dibujás.

**Procesamiento de imagen que hace el código:** conversión de SAR a dB (detectando si ya
viene en dB), stretch por percentiles robusto (2–98), filtro de mediana para speckle,
composición de falso color (VV/VH/VV−VH en SAR; SWIR/NIR/Green en óptico, donde el agua
sale negra). Todo esto es solo visual: no altera lo que se guarda.

**Salidas:**
- `labels/flood_polygons.geojson` — polígonos en lat/lon EPSG:4326, con propiedades
  (scene_id, class, n_pixels, area_ha, date, event_id, image_path).
- `labels/flood_labels.csv` — clicks sueltos con row/col + lat/lon.
- El guardado es **append** a esos dos archivos.

**Datos actuales:** apenas una prueba, no una campaña real. Un par de escenas de
Sen1Floods11 (`Bolivia_60373`, 512×512, una SAR S1 y una óptica S2) más un chip sintético
de demo. Unos 16 polígonos y 13 clicks en total.

---

## 2. Lo que se arregló recién (sesión de hoy)

Un pase de endurecimiento antes de empezar a etiquetar en serio. Relevante para la
discusión de arquitectura porque muestra dónde estaban los riesgos reales:

1. **Bug de convención de coordenadas (el grave).** Había dos convenciones chocando: la
   función que convierte la máscara del pincel a polígonos devolvía coordenadas de
   **esquina** de píxel, mientras que el conteo de área usaba **centros**. Medio píxel de
   desfase, con tres consecuencias medidas: un trazo de pincel de 1 px de ancho contaba 0
   píxeles y **se descartaba en silencio**; un bloque de 4×4 se guardaba como 12 px
   corrido 1 px en diagonal; y al exportar, *todos* los polígonos se corrían otro medio
   píxel = **5 m en datos de 10 m**. Arreglado unificando en coordenadas de centro.
   Verificado: ahora el conteo coincide exacto con lo pintado en las 8 formas de prueba,
   y un click y un polígono de 1 píxel caen en el mismo lon/lat con error 0.
2. **Agujeros en los polígonos.** El pincel podía dejar un hueco sin pintar y el área se
   sobre-reportaba. Ahora los anillos interiores se guardan en el GeoJSON, se restan al
   contar píxeles, y se dibujan vacíos.
3. **Riesgo de duplicar trabajo.** Volver a una escena ya etiquetada no mostraba nada y
   todo lo nuevo se anexaba encima de lo viejo. Ahora al cargar una escena se releen sus
   etiquetas guardadas, se dibujan punteadas, y hay un flag `--skip-labeled`.
4. **Memoria del pincel.** Alocaba un array `H×W×4` nuevo en *cada evento de mouse*
   (4 MB en 512², pero ~1.6 GB por evento en una escena de 10 000²). Ahora el buffer se
   aloca una vez por escena y se escribe en el lugar.
5. Menores: aviso si el TIFF no declara CRS (antes escribía coordenadas crudas como si
   fueran lat/lon), y arreglos en el respaldo del GeoJSON y el encabezado del CSV.

**El principio de diseño que salió de ahí, y que importa para hostear:** el bug de medio
píxel existió porque había dos convenciones de coordenadas en el mismo archivo. Con varios
clientes web haciendo la conversión del lado del cliente, ese tipo de bug se multiplica y
no se detecta nunca. → **El cliente debería mandar píxeles y el servidor hacer toda la
geometría**, para que haya una sola verdad.

---

## 3. La pregunta abierta: hostearlo con un backend

### "Hostear" puede significar cuatro cosas distintas, con costos muy distintos

| lectura | qué problema resuelve | ¿necesita hosting? |
|---|---|---|
| **distribución** | que alguien use la herramienta sin instalar conda | **sí** |
| **concurrencia** | que dos personas etiqueten sin pisarse | no — alcanza una DB |
| **datos** | que las etiquetas no vivan en archivos sueltos en una laptop | no — alcanza una DB |
| **integración** | que el etiquetado sea un paso de un pipeline mayor | parcial — API + cola |

Observación: **concurrencia y datos se resuelven sin hostear nada**, apuntando la GUI de
escritorio actual a Postgres en vez de a los archivos. Hosting solo es imprescindible para
distribución, y su costo real no es escribirlo sino mantenerlo vivo (server, DB con
respaldos, auth, deploys, y si se cae nadie etiqueta ese día).

### La app ya está medio partida en dos, sin haberlo planeado

| pieza actual | dónde viviría hosteada |
|---|---|
| abrir GeoTIFF multibanda, resolver bandas | **backend** (el browser no lee int16 de 13 bandas) |
| conversión a dB, stretch, despeckle, falso color | **backend** → devuelve PNG o tiles |
| lazo / polígono / pincel / click | **frontend** (lo único atado a matplotlib) |
| contar píxeles dentro de un polígono, calcular área | **backend** |
| conversión px ↔ lon/lat | **backend** |
| geojson + csv por append | **Postgres/PostGIS**; el GeoJSON pasa a ser un endpoint de export |

La mitad de "leer, renderizar, contar, georreferenciar" ya son funciones puras de
numpy/rasterio: es un backend sin HTTP todavía.

### La bifurcación que decide la arquitectura: tamaño de escena

- **Chips de ~512×512** (lo que hay hoy): el backend renderiza la escena entera a un PNG
  de ~200 KB y lo manda. **No hace falta COG, ni servidor de tiles, ni un map lib.** Un
  `<canvas>` con coordenadas en píxeles replica el modelo mental actual. El render es
  determinista, así que se cachea.
- **Escenas completas de Sentinel-1** (~25 000 × 16 000): hace falta COG en object storage
  + servidor de tiles (ej. titiler, que es FastAPI + rio-tiler y soporta band math por
  `expression`) + MapLibre/OpenLayers, todo en coordenadas geográficas. Bastante más
  trabajo.

### Opciones evaluadas

1. **Panel + Bokeh** — Bokeh trae `FreehandDrawTool`, `PolyDrawTool` y `PointDrawTool` de
   fábrica (= lazo, polígono, click). Panel lo sirve como app web. Se reusa casi todo el
   Python existente, sin JS ni endpoints. El pincel no tiene equivalente nativo. Días de
   trabajo.
2. **Backend propio completo** — FastAPI + PostGIS + frontend en canvas con las 4
   herramientas incluido el pincel (que en canvas es *más fácil* que en un map lib).
   1–2 semanas. Control total.
3. **Label Studio o CVAT self-hosted** — cero código de app: se pre-renderizan PNGs por
   vista y se adapta la salida con ~20 líneas reusando la conversión a lon/lat. Traen
   usuarios, asignación, revisión, atajos y brush de segmentación ya hechos. Se pierde la
   georreferenciación nativa y el control del render. CVAT tiene mejor brush; Label Studio
   es más fácil de operar.
4. **Solo cambiar el almacenamiento** — dejar la GUI de escritorio y mover geojson/csv a
   PostGIS. Resuelve duplicados y multiusuario con muy poco esfuerzo, sin hostear nada.

**Descartado:** Streamlit (re-corre el script entero en cada interacción, inviable para una
app de dibujo), y armar COG/tiles antes de saber si se necesita pan/zoom.

### Recomendación tentativa

Orden propuesto: **base de datos primero, pre-etiquetado segundo, frontend web al final**
— porque el frontend es lo único que se tira si se cambia de idea, y el backend (render +
geometría + DB) sobrevive a cualquier cambio de interfaz.

Dos argumentos que pesan más que la elección de stack:

- **El volumen decide más que la tecnología.** Si el modelo se entrena con unos cientos de
  chips, se hace con la herramienta de escritorio y no hay nada que hostear. Si necesita
  decenas de miles, ningún frontend lo salva: el salto lo da el **pre-etiquetado** — que
  la persona *corrija* en vez de dibujar de cero. En SAR, un umbral sobre VV en dB más
  morfología da la mayor parte del agua, y el humano corrige bordes y mata falsos
  positivos. Que `radar_shadow` ya sea una de las 4 clases sugiere que el problema ya está
  identificado.
- **Lo más valioso a guardar no son los polígonos, es la trazabilidad**: qué escena, quién,
  cuándo, con qué vista y umbrales, y sobre todo cuánto coincidieron dos personas sobre el
  mismo chip. Eso permite defender el modelo y estimar su techo de precisión (si dos
  humanos coinciden en 85%, no tiene sentido exigirle 95% al modelo). Eso es una tabla en
  una base, no una interfaz.

---

## 4. Lo que falta definir (el objetivo de la conversación)

1. **¿Cuántas etiquetas necesita el modelo?** Cientos vs. decenas de miles cambia todo.
2. **¿Quién etiqueta?** Solo una persona técnica / equipo interno de 3–10 / anotadores
   externos. Lo último es lo que más justifica adoptar Label Studio o CVAT en vez de
   construir.
3. **¿Chips o escenas completas con pan/zoom?** Es la bifurcación técnica principal.
4. **¿En qué estado está el pipeline aguas abajo?** ¿Ya hay un modelo entrenándose, o el
   etiquetado es el primer paso? ¿Cómo consume los datos: GeoJSON, máscaras rasterizadas,
   una tabla de muestras?
5. **¿Qué infra hay disponible?** No existe nada de cloud/DB/web en los repos locales, así
   que sería desde cero. ¿Hay cuenta de AWS/GCP, o algún servicio interno donde apoyarse?
6. **¿Quién lo mantiene después?** Es el costo que se subestima siempre.
7. **Contexto a confirmar:** esto parece alimentar un modelo de detección de inundaciones
   en el marco de seguros paramétricos (Suyana). Si es así, hay un requisito extra que
   cambia el diseño: **auditabilidad**, porque un pago depende de la detección.

---

## 5. Datos técnicos sueltos (para no reinventarlos)

- El intérprete con las dependencias es `/opt/anaconda3/bin/python`; el `python3` del PATH
  no tiene matplotlib ni rasterio.
- `flood_label.py` respeta `FLOOD_LABEL_HEADLESS=1` (saltea `matplotlib.use('TkAgg')`), y
  con backend Agg se puede instanciar la app completa y renderizar a PNG sin abrir ventana
  — sirve para testear.
- Bandas esperadas: SAR por descripciones `VV`/`VH`; óptico por `B2/B3/B4/B8/B11` con
  fallback posicional según cantidad de bandas (13 = S2 completo, 5 = Landsat 8/9).
- Los 16 polígonos que ya están guardados tienen el desfase viejo de medio píxel. Son de
  una prueba, así que probablemente se descarten.
- No hay control de versiones en el proyecto.
