# Etiquetado de sargazo

GUI de etiquetado por píxel sobre imágenes satelitales (Sentinel-2 / Landsat 8-9),
con overlay de FAI (Floating Algae Index) y máscaras de nube/tierra para guiar
el etiquetado. Guarda las etiquetas en `labels.csv` (clase + fila/columna + lat/lon + fecha).

Clases: `sargassum`, `water`, `cloud`, `land` (cloud + land se agrupan como `other`
si las usás para entrenar un clasificador binario sargazo/no-sargazo).

## Instalación

```bash
pip install -r requirements.txt
```

Requiere Python 3.9+. `landmask.py` descarga on-the-fly (streaming, sin bajar el
archivo entero) tiles de ESA WorldCover 10m desde un bucket público de AWS para
pintar la máscara de tierra — necesita internet. Si no hay internet o falla la
descarga, el script sigue funcionando, solo sin ese overlay.

## Estructura de datos esperada

```
etiquetado/
  data/
    {site}/                        # nombre libre, ej. "CancunMX"
      S2/                          # o "L8" / "L9"
        2024-07-15_..._ms.tif      # bandas multiespectrales — OBLIGATORIO
        2024-07-15_..._cloud.tif   # máscara de nube S2 (prob. s2cloudless 0-100) — opcional
        2024-07-15_..._qa.tif      # máscara de nube L8/L9 (QA_PIXEL) — opcional
  labels.csv                       # se crea acá al guardar la primera etiqueta
```

El nombre del archivo debe empezar con `YYYY-MM-DD` (se usa para fecha y filtro
de meses/años). Bandas esperadas en el `_ms.tif` (en este orden):

- **S2**: Blue, Green, Red, RE1, RE2, RE3, NIR, NIR_narrow, SWIR1
- **L8/L9**: Blue, Green, Red, NIR, SWIR1

Si tus imágenes vienen en otro orden de bandas, ajustá el diccionario `BAND_IDX`
al inicio de `label.py`.

## Uso

```bash
python label.py --sensor S2 --site CancunMX                  # default: may-sep, nube ≤40%
python label.py --sensor S2 --site CancunMX --months all      # sin filtro de temporada
python label.py --sensor S2 --site CancunMX --max-cloud-pct 10
python label.py --sensor S2 --all-sites                       # todos los sitios en data/
python label.py --sensor S2 --site CancunMX --skip-labeled    # salta escenas ya etiquetadas
```

Controles en la GUI:
- Click izquierdo → etiqueta el píxel con la clase seleccionada (radio "Clase").
- Herramienta "polígono" → selecciona un área completa (útil para manchas grandes de sargazo).
- Tecla `m` → prende/apaga el overlay de máscaras (nube detectada en magenta, tierra en marrón).
- "Guardar → Siguiente" → escribe las etiquetas acumuladas a `labels.csv` y pasa a la próxima escena.
- "Descartar" → pasa a la próxima escena sin guardar.
- "Limpiar" → borra las etiquetas puestas en la escena actual (sin guardar).
- "◄ Anterior" → vuelve a la escena previa.

## Notas de adaptación

- Los umbrales de FAI (`vmin=-0.05, vmax=0.10`) y de nube (`S2_CLOUD_THRESHOLD = 40`)
  están calibrados para nuestras escenas del Caribe; puede convenir ajustarlos según
  la región/sensor.
- El panel izquierdo muestra RGB (con overlays), el derecho el índice FAI
  (verde = posible sargazo).
- Este código salió de un pipeline mayor de detección costera de sargazo; acá
  quedó aislado solo con lo necesario para etiquetar. No incluye el modelo de
  entrenamiento ni los datos ya etiquetados.
