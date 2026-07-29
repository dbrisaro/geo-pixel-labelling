"""
label.py  —  Herramienta de etiquetado (4 clases, guarda coordenadas)

Lee imágenes de data/{site}/{sensor}/*_ms.tif
Genera/extiende labels.csv con filas:
    image_ms_path, scene_id, sensor, pixel_size_m,
    date, time_utc,
    row, col, lat, lon, class

4 clases: sargassum, water, cloud, land

Columnas y su uso:
  row/col          → el training extrae parches para CNN 2D
  lat/lon          → match espacial con cualquier sensor (MODIS, VIIRS, PACE, S3)
  date + time_utc  → match temporal; permite saber si el pase de MODIS fue
                     antes o después del pase de S2/L8 (el sargassum se mueve)
  pixel_size_m     → resolución del píxel etiquetado (S2≈10m, L8/L9≈30m).
                     Permite calcular ÁREA cubierta por sargassum dentro de un
                     píxel grueso (ej. MODIS 1km) y aplicar umbrales:
                       fracción = Σárea(sargassum) / Σárea(etiquetados en footprint)
                       si fracción >= 0.40 → píxel MODIS = sargassum
  scene_id         → ID de la escena (stem del archivo); permite trazabilidad
                     aunque la ruta local cambie

Máscara de nube:
  S2  → *_cloud.tif  (s2cloudless, umbral > 40)
  L8/L9 → *_qa.tif   (QA_PIXEL bits 3 y 4)

Filtro de escenas (al armar la lista que se abre):
  --months        meses a etiquetar. Default '5-9' (temporada alta may-sep).
                  '5,6,7,8,9' o 'all' también válidos.
  --max-cloud-pct máximo % de píxeles de nube en la escena. Default 40
                  (= cloud mask ≥ 0.6, el tope con que se descargó). El % se mide
                  sobre TODA la imagen con el mismo criterio por-píxel de la viz.
  Escenas sin máscara de nube se conservan (no evaluables) y se avisan.

Uso:
    python label.py --sensor S2 --site cancun                 # default: may-sep, nube ≤40%
    python label.py --sensor S2 --site TulumMX_resample
    python label.py --sensor L8 --site punta_cana --months all # sin filtro de temporada
    python label.py --sensor S2 --site cancun --max-cloud-pct 10
    python label.py --sensor S2 --all-sites

Estructura esperada de datos (ver README.md):
    etiquetado/
      data/
        {site}/
          {sensor}/            # sensor = S2, L8 o L9
            2024-07-15_..._ms.tif      # bandas multiespectrales (obligatorio)
            2024-07-15_..._cloud.tif   # máscara de nube S2 (opcional, s2cloudless)
            2024-07-15_..._qa.tif      # máscara de nube L8/L9 (opcional, QA_PIXEL)
      labels.csv              # se crea/extiende acá
"""

import matplotlib
matplotlib.use('TkAgg')

import os, sys, glob, argparse, re, math
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.widgets import RadioButtons, Button, PolygonSelector
from matplotlib.path import Path
import rasterio
from landmask import land_mask          # máscara de tierra ESA WorldCover 10m (opcional, requiere internet)

HERE      = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(HERE, 'data')

# ── 4 clases ───────────────────────────────────────────────────────────────────
CLASSES = ['sargassum', 'water', 'cloud', 'land']
CLASS_COLORS = {
    'sargassum': '#ff5500',
    'water':     '#00aaff',
    'cloud':     '#dddddd',
    'land':      '#aa7733',
}

# ── posición de bandas en ms.tif ───────────────────────────────────────────────
# S2: B1=Blue B2=Green B3=Red B4=RE1 B5=RE2 B6=RE3 B7=NIR B8=NIR_n B9=SWIR1
# L8/L9: B1=Blue B2=Green B3=Red B4=NIR B5=SWIR1
BAND_IDX = {
    'S2':  {'Red': 2, 'Green': 1, 'Blue': 0, 'NIR': 6, 'SWIR1': 8},
    'L8':  {'Red': 2, 'Green': 1, 'Blue': 0, 'NIR': 3, 'SWIR1': 4},
    'L9':  {'Red': 2, 'Green': 1, 'Blue': 0, 'NIR': 3, 'SWIR1': 4},
}
FAI_COEFF = {
    'S2':  (860 - 665) / (1610 - 665),
    'L8':  (865 - 655) / (1610 - 655),
    'L9':  (865 - 655) / (1610 - 655),
}
S2_CLOUD_THRESHOLD = 40   # % probabilidad s2cloudless

# ── colormap FAI ───────────────────────────────────────────────────────────────
_cmap_fai = mcolors.LinearSegmentedColormap.from_list('fai', [
    (0.04, 0.12, 0.28), (0.10, 0.30, 0.55), (0.75, 0.82, 0.85),
    (0.55, 0.88, 0.35), (0.08, 0.72, 0.08),
], N=256)
_cmap_fai.set_bad('#1a1a2e')


def _stretch(arr, lo=2, hi=98):
    v = arr[np.isfinite(arr) & (arr > 0)]
    if len(v) < 10:
        return np.clip(arr, 0, 1)
    p_lo, p_hi = np.percentile(v, [lo, hi])
    return np.clip((arr - p_lo) / (p_hi - p_lo + 1e-10), 0, 1)


def _companion(ms_path, suffix):
    """Reemplaza _ms.tif por el suffix dado."""
    return ms_path.replace('_ms.tif', suffix)


def _read_cloud_mask(ms_path, sensor):
    """
    Retorna bool mask (True = nube/sombra) o None.
    Detecta automáticamente el formato:
      - Nuevo (download.py): *_cloud.tif / *_qa.tif junto al ms
      - CoastSat: ../mask/*_mask.tif
    """
    if sensor == 'S2':
        # formato nuevo
        cld_path = _companion(ms_path, '_cloud.tif')
        if os.path.exists(cld_path):
            with rasterio.open(cld_path) as src:
                prob = src.read(1).astype(np.float32)
            return prob > S2_CLOUD_THRESHOLD
        # formato CoastSat: mask/ hermano de ms/
        mask_path = ms_path.replace('/ms/', '/mask/').replace('_ms.tif', '_mask.tif')
        if os.path.exists(mask_path):
            with rasterio.open(mask_path) as src:
                qa = src.read(1)
            return np.isin(qa, [3, 8, 9, 10])
        return None

    else:  # L8 / L9
        # formato nuevo
        qa_path = _companion(ms_path, '_qa.tif')
        if os.path.exists(qa_path):
            with rasterio.open(qa_path) as src:
                qa = src.read(1)
            return ((qa >> 3) & 1).astype(bool) | ((qa >> 4) & 1).astype(bool)
        # formato CoastSat
        mask_path = ms_path.replace('/ms/', '/mask/').replace('_ms.tif', '_mask.tif')
        if os.path.exists(mask_path):
            with rasterio.open(mask_path) as src:
                qa = src.read(1)
            return ((qa & (1 << 2)) != 0) | ((qa & (1 << 3)) != 0)
        return None


def _cloud_fraction(ms_path, sensor):
    """
    Fracción de píxeles de nube en la escena, en [0,1], sobre TODA la imagen
    (nodata cuenta como no-nube). Retorna None si no hay máscara disponible.
    Usa el mismo criterio por-píxel que la visualización:
      S2    → s2cloudless prob > S2_CLOUD_THRESHOLD
      L8/L9 → QA_PIXEL bits de nube/sombra
    """
    mask = _read_cloud_mask(ms_path, sensor)
    if mask is None or mask.size == 0:
        return None
    return float(np.count_nonzero(mask)) / mask.size


def _parse_months(spec):
    """'6-9' | '6,7,8,9' | 'all' → set de meses (1-12). 'all' → todos."""
    spec = (spec or '').strip().lower()
    if spec in ('all', '', 'todos'):
        return set(range(1, 13))
    months = set()
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-')
            months.update(range(int(a), int(b) + 1))
        elif part:
            months.add(int(part))
    return months


def _parse_years(spec):
    """'2024' | '2024-2026' | '2024,2025' → set de años. None/'' → None (sin filtro).
    Ojo: '2024' = solo 2024. Para "desde 2024" usar '2024-2026'."""
    spec = (spec or '').strip().lower()
    if spec in ('', 'all', 'todos'):
        return None
    years = set()
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-')
            years.update(range(int(a), int(b) + 1))
        elif part:
            years.add(int(part))
    return years or None


# ── metadatos de escena ────────────────────────────────────────────────────────
def _extract_image_meta(ms_path, src):
    """
    Extrae (scene_id, time_utc, pixel_size_m) desde el path y un src rasterio abierto.

    scene_id    : stem del archivo (ej. '2025-07-15' o 'S2B_T16QBJ_20250715T155819')
    time_utc    : 'HH:MM:SS' si se encuentra en el nombre o en tags TIFF, else ''
    pixel_size_m: resolución del píxel en metros (desde el transform; si CRS geográfico
                  se aproxima usando la latitud central)
    """
    stem = os.path.basename(ms_path).replace('_ms.tif', '')
    scene_id = stem

    # ── time_utc: busca T\d{6} en el nombre (S2 / Landsat Collection-2) ──────
    time_utc = ''
    m = re.search(r'T(\d{2})(\d{2})(\d{2})(?:\D|$)', stem)
    if m:
        time_utc = f'{m.group(1)}:{m.group(2)}:{m.group(3)}'
    else:
        # fallback: tags TIFF (algunos pipelines los escriben)
        tags = src.tags()
        for key in ('TIFFTAG_DATETIME', 'datetime', 'acquisition_time',
                    'sensing_time', 'DATE_ACQUIRED', 'SCENE_CENTER_TIME'):
            val = tags.get(key, '')
            if val:
                tm = re.search(r'(\d{2})[:\-](\d{2})[:\-](\d{2})', val)
                if tm:
                    time_utc = f'{tm.group(1)}:{tm.group(2)}:{tm.group(3)}'
                    break

    # ── pixel_size_m: desde el transform ──────────────────────────────────────
    res_x = abs(src.transform.a)           # ancho de píxel en unidades del CRS
    if src.crs and src.crs.is_projected:
        pixel_size_m = round(res_x, 2)    # ya está en metros
    else:
        # CRS geográfico (grados): convierte a metros en la latitud central
        center_lat = src.transform.f + src.transform.e * src.height / 2.0
        m_per_deg  = 111320.0 * math.cos(math.radians(abs(center_lat)))
        pixel_size_m = round(res_x * m_per_deg, 2)

    return scene_id, time_utc, pixel_size_m


# ── aplicación ─────────────────────────────────────────────────────────────────
class LabelingApp:

    def __init__(self, sensor, site, csv_path, all_sites=False, no_cloud_mask=False,
                 months=None, max_cloud_pct=10.0, skip_labeled=False, years=None):
        self.sensor    = sensor
        self.csv_path  = csv_path
        self.months        = months if months is not None else set(range(1, 13))
        self.years         = years          # set de años permitidos, o None = todos
        self.max_cloud_pct = max_cloud_pct
        self.skip_labeled  = skip_labeled

        if all_sites:
            # recopila todas las imágenes S2 de todos los sitios en DATA_ROOT
            sites = sorted([
                d for d in os.listdir(DATA_ROOT)
                if os.path.isdir(os.path.join(DATA_ROOT, d)) and d != 'validation'
            ])
            self.ms_files = []
            self.site_per_file = []
            for s in sites:
                sensor_dir = os.path.join(DATA_ROOT, s, sensor)
                files = sorted(glob.glob(os.path.join(sensor_dir, '*_ms.tif')))
                if not files:
                    files = sorted(glob.glob(os.path.join(sensor_dir, 'ms', '*_ms.tif')))
                for f in files:
                    self.ms_files.append(f)
                    self.site_per_file.append(s)
            if not self.ms_files:
                sys.exit(f'No se encontraron imágenes {sensor} en {DATA_ROOT}')
            print(f'{len(self.ms_files)} imágenes {sensor} en {len(sites)} sitios: {sites}')
            self.site = 'all'
        else:
            self.site = site
            sensor_dir = os.path.join(DATA_ROOT, site, sensor)
            # formato nuevo (download.py): archivos directo en L8/ o S2/
            self.ms_files = sorted(glob.glob(os.path.join(sensor_dir, '*_ms.tif')))
            # formato CoastSat (fallback): archivos en L8/ms/
            if not self.ms_files:
                self.ms_files = sorted(glob.glob(os.path.join(sensor_dir, 'ms', '*_ms.tif')))
            if not self.ms_files:
                sys.exit(f'No se encontraron imágenes en {sensor_dir}')
            self.site_per_file = [site] * len(self.ms_files)
            print(f'{len(self.ms_files)} imágenes en {sensor_dir}')

        if self.skip_labeled:
            self._drop_labeled()
        self._filter_scenes()

        self.no_cloud_mask = no_cloud_mask
        # máscaras (nube + tierra, las que usa la detección) mostradas como overlay.
        # Default: ON (para ver qué descarta la detección y etiquetar lo que se le escapa).
        self.show_masks    = not no_cloud_mask
        self.idx           = 0
        self.H = self.W    = 0
        self.labels        = []
        self.current_class = 'sargassum'
        self.current_tool  = 'click'
        self._poly_sel     = []
        self._sc           = {'rgb': [], 'fai': []}

        self._build_figure()
        self._load_image()
        plt.show()

    # ── filtro: escenas ya etiquetadas ───────────────────────────────────────────
    def _drop_labeled(self):
        """
        Saca de la lista las escenas que ya tienen etiquetas en el CSV, para no
        re-mostrarlas ni pisar labels. Match por image_ms_path (exacto) y por
        scene_id (stem del archivo), así no importa si la ruta cambió.
        """
        if not os.path.exists(self.csv_path):
            print('CSV no existe aún → no hay escenas etiquetadas que saltar.')
            return
        try:
            df = pd.read_csv(self.csv_path, on_bad_lines='skip',
                             usecols=['image_ms_path', 'scene_id'])
        except Exception as e:
            print(f'No pude leer el CSV para saltar etiquetadas ({e}). Sigo sin filtrar.')
            return

        labeled_paths  = set(df['image_ms_path'].dropna().astype(str))
        labeled_scenes = set(df['scene_id'].dropna().astype(str))

        kept_f, kept_s, dropped = [], [], 0
        for ms_path, site in zip(self.ms_files, self.site_per_file):
            scene_id = os.path.basename(ms_path).replace('_ms.tif', '')
            if ms_path in labeled_paths or scene_id in labeled_scenes:
                dropped += 1
                continue
            kept_f.append(ms_path)
            kept_s.append(site)

        self.ms_files      = kept_f
        self.site_per_file = kept_s
        print(f'Saltadas {dropped} escenas ya etiquetadas → quedan {len(kept_f)} sin etiquetar.')

    # ── filtro temporada + año + nube ─────────────────────────────────────────────
    def _filter_scenes(self):
        """
        Deja solo escenas que (a) caen en los meses de self.months,
        (b) caen en los años de self.years (si se especificó) y
        (c) tienen fracción de nube <= self.max_cloud_pct %.
        Escenas sin máscara de nube se conservan (no se pueden evaluar) y se avisan.
        """
        full   = bool(self.months) and len(self.months) >= 12
        if full and self.max_cloud_pct >= 100 and self.years is None:
            return  # sin filtro

        max_frac = self.max_cloud_pct / 100.0
        kept_f, kept_s = [], []
        n_total = len(self.ms_files)
        drop_season = drop_year = drop_cloud = no_mask = 0

        yr_txt = f', años {sorted(self.years)}' if self.years else ''
        print(f'Filtrando {n_total} escenas '
              f'(meses {sorted(self.months)}{yr_txt}, nube ≤ {self.max_cloud_pct:.0f}%)...')
        for ms_path, site in zip(self.ms_files, self.site_per_file):
            m = re.match(r'(\d{4})-(\d{2})-\d{2}', os.path.basename(ms_path))
            year  = int(m.group(1)) if m else None
            month = int(m.group(2)) if m else None
            if self.years is not None and year is not None and year not in self.years:
                drop_year += 1
                continue
            if month is not None and month not in self.months:
                drop_season += 1
                continue
            frac = _cloud_fraction(ms_path, self.sensor)
            if frac is None:
                no_mask += 1            # no evaluable → se conserva
            elif frac > max_frac:
                drop_cloud += 1
                continue
            kept_f.append(ms_path)
            kept_s.append(site)

        self.ms_files      = kept_f
        self.site_per_file = kept_s
        print(f'  → {len(kept_f)}/{n_total} escenas pasan el filtro '
              f'(descartadas: {drop_year} fuera de año, {drop_season} fuera de temporada, '
              f'{drop_cloud} con nube > {self.max_cloud_pct:.0f}%'
              + (f', {no_mask} sin máscara conservadas)' if no_mask else ')'))
        if not self.ms_files:
            sys.exit('Ninguna escena pasa el filtro. Ajustá --months / --max-cloud-pct.')

    # ── figura ─────────────────────────────────────────────────────────────────
    def _build_figure(self):
        self.fig = plt.figure(figsize=(16, 9))
        self.fig.patch.set_facecolor('#12121f')

        self.ax_rgb = self.fig.add_axes([0.02, 0.14, 0.45, 0.82])
        self.ax_fai = self.fig.add_axes([0.50, 0.14, 0.45, 0.82])
        for ax in (self.ax_rgb, self.ax_fai):
            ax.set_facecolor('#0d0d1a')
            for sp in ax.spines.values():
                sp.set_edgecolor('#333355')

        self.ttl = self.fig.text(0.5, 0.985, '', ha='center', va='top',
                                 color='white', fontsize=10, fontweight='bold')

        ax_cls = self.fig.add_axes([0.02, 0.01, 0.27, 0.11])
        ax_cls.set_facecolor('#1a1a2e')
        ax_cls.set_title('Clase', color='#aaaaaa', fontsize=8, pad=2)
        self.radio_cls = RadioButtons(ax_cls, CLASSES, activecolor='#ff6600')
        for lbl in self.radio_cls.labels:
            lbl.set_color('white'); lbl.set_fontsize(8)
        self.radio_cls.on_clicked(lambda l: setattr(self, 'current_class', l))

        ax_tool = self.fig.add_axes([0.31, 0.01, 0.14, 0.11])
        ax_tool.set_facecolor('#1a1a2e')
        ax_tool.set_title('Herramienta', color='#aaaaaa', fontsize=8, pad=2)
        self.radio_tool = RadioButtons(ax_tool, ('click', 'polígono'), activecolor='#44aaff')
        for lbl in self.radio_tool.labels:
            lbl.set_color('white'); lbl.set_fontsize(8)
        self.radio_tool.on_clicked(self._on_tool)

        kw = dict(color='#22223a', hovercolor='#33335a')
        def _btn(rect, label, cb):
            ax = self.fig.add_axes(rect); b = Button(ax, label, **kw)
            b.label.set_color('white'); b.label.set_fontsize(8); b.on_clicked(cb); return b

        self.btn_save = _btn([0.48, 0.04, 0.16, 0.07], 'Guardar → Siguiente', self._save_and_next)
        self.btn_dis  = _btn([0.66, 0.04, 0.10, 0.07], 'Descartar',           self._discard)
        self.btn_clr  = _btn([0.78, 0.04, 0.10, 0.07], 'Limpiar',             self._clear)
        self.btn_prv  = _btn([0.90, 0.04, 0.09, 0.07], '◄ Anterior',          self._prev)

        self.status = self.fig.text(0.02, 0.125, '', color='#8888aa', fontsize=7.5)
        self.fig.canvas.mpl_connect('button_press_event', self._on_click)
        self.fig.canvas.mpl_connect('key_press_event', self._on_key)

    # ── carga de imagen ─────────────────────────────────────────────────────────
    def _load_image(self):
        ms_path = self.ms_files[self.idx]
        self.site = self.site_per_file[self.idx]
        bidx    = BAND_IDX[self.sensor]

        with rasterio.open(ms_path) as src:
            raw          = src.read().astype(np.float32)
            self.H, self.W = src.height, src.width
            nd           = src.nodata
            self.crs       = src.crs
            self.transform = src.transform
            self.scene_id, self.time_utc, self.pixel_size_m = \
                _extract_image_meta(ms_path, src)

        # limpia nodata (CoastSat usa -inf, nuevo pipeline usa NaN)
        if nd is not None:
            raw[raw == nd] = np.nan
        raw = np.where(~np.isfinite(raw) | (raw < -0.5) | (raw > 2.0), np.nan, raw)

        R  = raw[bidx['Red']]
        G  = raw[bidx['Green']]
        B  = raw[bidx['Blue']]
        N  = raw[bidx['NIR']]
        S1 = raw[bidx['SWIR1']]

        coeff    = FAI_COEFF[self.sensor]
        self.fai = N - (R + (S1 - R) * coeff)
        self.ms_path = ms_path
        self.labels  = []

        # RGB base (sin máscaras) — se guarda para poder prender/apagar overlays sin recargar
        base = np.stack([_stretch(R), _stretch(G), _stretch(B)], axis=-1)
        self._base_rgb = np.nan_to_num(base)

        # máscaras de referencia: las MISMAS que usa la detección
        self._cloud_mask = _read_cloud_mask(ms_path, self.sensor)   # nube (s2cloudless / QA)
        self._land = None
        try:
            self._land = land_mask(self.crs, self.transform, self.H, self.W)  # tierra (WorldCover)
        except Exception as ex:
            print(f'  [landmask] no se pudo cargar WorldCover ({ex}); sigo sin overlay de tierra.')

        stem = os.path.basename(ms_path).replace('_ms.tif', '')
        self.ttl.set_text(f'{self.sensor} · {self.site} · {stem}  '
                          f'[{self.idx+1}/{len(self.ms_files)}]   ·   tecla "m" = máscaras on/off')
        self._render_display()

    # ── tinte semitransparente de una máscara sobre el RGB ───────────────────────
    def _tint(self, img, mask, color, alpha=0.5):
        m = mask[:self.H, :self.W]
        if not m.any():
            return
        for k in range(3):
            ch = img[..., k]
            ch[m] = (1 - alpha) * ch[m] + alpha * color[k]

    # ── dibujado (overlays de máscara según self.show_masks) ──────────────────────
    def _render_display(self):
        rgb      = self._base_rgb.copy()
        fai_disp = self.fai.copy()
        if self.show_masks:
            # tierra (marrón) y nube DETECTADA (magenta). Lo que quede SIN pintar y
            # sea nube → es lo que el cloud mask se escapa: eso es lo que hay que etiquetar.
            if self._land is not None:
                self._tint(rgb, self._land, (0.55, 0.38, 0.20))
                fai_disp[self._land[:self.H, :self.W]] = np.nan
            if self._cloud_mask is not None:
                self._tint(rgb, self._cloud_mask, (1.0, 0.25, 1.0))
                fai_disp[self._cloud_mask[:self.H, :self.W]] = np.nan

        self.ax_rgb.clear(); self.ax_fai.clear()
        self._sc = {'rgb': [], 'fai': []}

        self.ax_rgb.imshow(rgb, origin='upper', aspect='equal', interpolation='nearest')
        masks_txt = ('máscaras ON · magenta=nube detectada · marrón=tierra'
                     if self.show_masks else 'máscaras OFF (imagen cruda)')
        self.ax_rgb.set_title(f'RGB — {masks_txt}', color='#9999bb', fontsize=9, pad=4)
        self.ax_rgb.axis('off')

        self.ax_fai.imshow(fai_disp, cmap=_cmap_fai, origin='upper', aspect='equal',
                           vmin=-0.05, vmax=0.10, interpolation='nearest')
        self.ax_fai.set_title('FAI  (verde = posible sargassum)', color='#9999bb', fontsize=9, pad=4)
        self.ax_fai.axis('off')

        self._setup_poly()
        self._update_scatter()   # re-dibuja las etiquetas ya puestas (no las pierde)
        self._update_status()
        self.fig.canvas.draw_idle()

    # ── toggle de máscaras (tecla 'm') ───────────────────────────────────────────
    def _on_key(self, event):
        if event.key == 'm':
            self.show_masks = not self.show_masks
            self._render_display()

    # ── poly selector ───────────────────────────────────────────────────────────
    def _setup_poly(self):
        for ps in self._poly_sel:
            try: ps.disconnect_events(); ps.set_visible(False)
            except Exception: pass
        self._poly_sel = []
        if self.current_tool == 'polígono':
            kw = dict(props=dict(color='yellow', linewidth=1.2, alpha=0.8),
                      handle_props=dict(markersize=5, markerfacecolor='yellow'))
            self._poly_sel = [
                PolygonSelector(self.ax_rgb, self._on_polygon, **kw),
                PolygonSelector(self.ax_fai, self._on_polygon, **kw),
            ]

    # ── callbacks ───────────────────────────────────────────────────────────────
    def _on_tool(self, label): self.current_tool = label; self._setup_poly()

    def _on_click(self, event):
        if self.current_tool != 'click': return
        if event.inaxes not in (self.ax_rgb, self.ax_fai): return
        if event.button != 1 or event.xdata is None: return
        c, r = int(round(event.xdata)), int(round(event.ydata))
        if 0 <= r < self.H and 0 <= c < self.W:
            self.labels.append((r, c, self.current_class))
            self._update_scatter(); self._update_status()

    def _on_polygon(self, verts):
        path = Path(verts); va = np.array(verts)
        c0, c1 = max(0, int(va[:,0].min())), min(self.W-1, int(va[:,0].max()))
        r0, r1 = max(0, int(va[:,1].min())), min(self.H-1, int(va[:,1].max()))
        cols, rows = np.meshgrid(np.arange(c0, c1+1), np.arange(r0, r1+1))
        pts  = np.column_stack([cols.ravel(), rows.ravel()])
        mask = path.contains_points(pts)
        for cc, rr in pts[mask]:
            self.labels.append((int(rr), int(cc), self.current_class))
        print(f'  Polígono: {mask.sum()} px ({self.current_class})')
        self._update_scatter(); self._update_status(); self._setup_poly()

    def _update_scatter(self):
        for lst in self._sc.values():
            for sc in lst:
                try: sc.remove()
                except Exception: pass
        self._sc = {'rgb': [], 'fai': []}
        for cls, color in CLASS_COLORS.items():
            pts = [(c, r) for r, c, cl in self.labels if cl == cls]
            if not pts: continue
            xs, ys = zip(*pts)
            kw = dict(c=color, s=10, alpha=0.8, linewidths=0, zorder=5)
            self._sc['rgb'].append(self.ax_rgb.scatter(xs, ys, **kw))
            self._sc['fai'].append(self.ax_fai.scatter(xs, ys, **kw))
        self.fig.canvas.draw_idle()

    def _update_status(self):
        counts = {cls: sum(1 for _, _, c in self.labels if c == cls) for cls in CLASSES}
        n_csv  = 0
        if os.path.exists(self.csv_path):
            try:
                with open(self.csv_path) as f:
                    n_csv = max(0, sum(1 for _ in f) - 1)
            except Exception: pass
        self.status.set_text(
            '  '.join(f'{cls}: {counts[cls]}' for cls in CLASSES) +
            f'     |     CSV total: {n_csv} px'
        )
        self.fig.canvas.draw_idle()

    # ── guardar ─────────────────────────────────────────────────────────────────
    def _save(self):
        if not self.labels:
            print('  Sin etiquetas.'); return

        # fecha desde el nombre del archivo (YYYY-MM-DD)
        m = re.match(r'(\d{4}-\d{2}-\d{2})', os.path.basename(self.ms_path))
        date_str = m.group(1) if m else ''

        # lat/lon en WGS84 para todos los píxeles etiquetados
        from rasterio.warp import transform as warp_transform
        from rasterio.crs import CRS
        with rasterio.open(self.ms_path) as src:
            t       = src.transform
            src_crs = src.crs
        rs  = [r for r, c, _ in self.labels]
        cs  = [c for r, c, _ in self.labels]
        xs, ys = rasterio.transform.xy(t, rs, cs)
        if src_crs and not src_crs.is_geographic:
            lons, lats = warp_transform(src_crs, CRS.from_epsg(4326), xs, ys)
        else:
            lons, lats = xs, ys

        rows = [{'image_ms_path': self.ms_path,
                 'scene_id':      self.scene_id,
                 'sensor':        self.sensor,
                 'pixel_size_m':  self.pixel_size_m,
                 'date':          date_str,
                 'time_utc':      self.time_utc,
                 'row': r, 'col': c,
                 'lat': round(float(lat), 6), 'lon': round(float(lon), 6),
                 'class': cls}
                for (r, c, cls), lat, lon in zip(self.labels, lats, lons)]
        df     = pd.DataFrame(rows)
        header = not os.path.exists(self.csv_path)
        df.to_csv(self.csv_path, mode='a', index=False, header=header)
        counts = df['class'].value_counts().to_dict()
        print(f'  ✓ {len(rows)} px guardados  {counts}')

    def _save_and_next(self, _): self._save(); self._go_to(self.idx + 1)
    def _discard(self, _):       self._go_to(self.idx + 1)
    def _clear(self, _):         self.labels = []; self._update_scatter(); self._update_status()
    def _prev(self, _):          self._go_to(self.idx - 1)

    def _go_to(self, new_idx):
        if new_idx < 0:  print('  Primera imagen.'); return
        if new_idx >= len(self.ms_files): print('  ¡Listo!'); return
        self.idx = new_idx; self._load_image()


# ── main ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sensor',    choices=['S2', 'L8', 'L9'], required=True)
    parser.add_argument('--site',      default=None)
    parser.add_argument('--all-sites',      action='store_true',
                        help='Carga todos los sitios en DATA_ROOT en secuencia')
    parser.add_argument('--no-cloud-mask', action='store_true',
                        help='No aplica el mask de nubes en la visualización')
    parser.add_argument('--months',         default='5-9',
                        help="Meses a etiquetar. Ej: '5-9' (default, temporada alta may-sep), "
                             "'5,6,7,8,9' o 'all' para todos.")
    parser.add_argument('--max-cloud-pct',  type=float, default=40.0,
                        help='Máximo %% de píxeles de nube permitido en la escena '
                             '(default 40 = cloud mask ≥ 0.6, el tope con que se descargó).')
    parser.add_argument('--skip-labeled',   action='store_true',
                        help='No mostrar escenas que ya tienen etiquetas en el CSV '
                             '(para no pisar lo ya hecho).')
    parser.add_argument('--years',          default=None,
                        help="Años a etiquetar. Ej: '2024' (solo 2024), '2024-2026' (desde 2024), "
                             "'2024,2025'. Default: todos los años.")
    parser.add_argument('--csv',            default=os.path.join(HERE, 'labels.csv'))
    args = parser.parse_args()

    months = _parse_months(args.months)
    years  = _parse_years(args.years)

    all_sites = args.all_sites
    if not all_sites and not args.site:
        parser.error('Debes especificar --site o --all-sites')

    print(f'\nSensor: {args.sensor}  Sitio: {"ALL" if all_sites else args.site}  CSV: {args.csv}')
    if os.path.exists(args.csv):
        try:
            df = pd.read_csv(args.csv, on_bad_lines='skip')
        except TypeError:
            df = pd.read_csv(args.csv, error_bad_lines=False)
        print(f'CSV existente: {len(df)} etiquetas  {df["class"].value_counts().to_dict()}')
    yr_txt = f'  ·  años {sorted(years)}' if years else ''
    print(f'Filtro: meses {sorted(months)}{yr_txt}  ·  nube ≤ {args.max_cloud_pct:.0f}%')
    print('Clases: sargassum(naranja) · water(azul) · cloud(blanco) · land(marrón)\n')
    LabelingApp(args.sensor, args.site, args.csv, all_sites=all_sites,
                no_cloud_mask=args.no_cloud_mask,
                months=months, max_cloud_pct=args.max_cloud_pct,
                skip_labeled=args.skip_labeled, years=years)

if __name__ == '__main__':
    main()
