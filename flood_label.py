"""
flood_label.py — App de etiquetado manual de inundaciones (SAR y óptico)

VOS dibujás las etiquetas. La app no clasifica nada: solo muestra la imagen de la
mejor forma posible para que puedas interpretarla, y guarda lo que marcás.

4 clases:
    1 water          agua (sin distinguir permanente de inundación: esa distinción
                     no es confiable a ojo en una sola imagen)
    2 dry_land       tierra seca
    3 cloud          nube (solo óptico)
    4 radar_shadow   sombra/layover de relieve — se ve oscuro como el agua pero
                     NO es agua (solo SAR). Es el falso positivo principal.

4 herramientas de dibujo (panel derecho, o teclas z x c v):
    lazo      apretás y dibujás el contorno a mano alzada; se cierra al soltar.
              Lo más rápido para bordes de agua, que son curvas irregulares.
    polígono  un clic por vértice. Clic derecho o doble clic cierra; Esc cancela.
    pincel    pintás arrastrando un círculo; la rueda del mouse cambia el radio.
    click     marca un píxel suelto (puntos de control).

Mientras dibujás ves el relleno del color de la clase y las hectáreas en vivo.

Detecta solo si cada .tif es SAR u óptico, leyendo los nombres de banda del archivo.

Salidas (carpeta labels/):
    labels/flood_polygons.geojson   los polígonos, en lat/lon EPSG:4326
    labels/flood_labels.csv         los clicks sueltos, con row/col y lat/lon

Lo que ya guardaste para una escena se vuelve a dibujar punteado al reabrirla, así
no se etiqueta dos veces lo mismo. Lo nuevo se suma a lo guardado, no lo reemplaza.

Uso:
    python flood_label.py                          # lee data/SAR/
    python flood_label.py --dir data/otra_carpeta
    python flood_label.py --event-id bolivia-60373
    python flood_label.py --skip-labeled           # saltea escenas ya etiquetadas
"""

import os
import sys
import glob
import json
import argparse
import re
import math

import matplotlib
if not os.environ.get('FLOOD_LABEL_HEADLESS'):
    matplotlib.use('TkAgg')

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.widgets import RadioButtons, Button
from matplotlib.patches import Polygon as MplPolygon, PathPatch, Circle
from matplotlib.lines import Line2D
from matplotlib.path import Path

import rasterio
from rasterio.features import shapes as rio_shapes
from rasterio.warp import transform as warp_transform
from rasterio.crs import CRS

try:
    from scipy.ndimage import median_filter
except Exception:                                    # pragma: no cover
    median_filter = None

HERE = os.path.dirname(os.path.abspath(__file__))

CLASSES = ['water', 'dry_land', 'cloud', 'radar_shadow']
CLASS_COLORS = {
    'water':        '#0a84ff',
    'dry_land':     '#c69c6d',
    'cloud':        '#f2f2f7',
    'radar_shadow': '#bf5af2',
}

TOOLS = ['lazo', 'polígono', 'pincel', 'click']
TOOL_KEYS = {'z': 'lazo', 'x': 'polígono', 'c': 'pincel', 'v': 'click'}

# Alias de nombre de banda → se resuelven desde las descripciones del .tif.
# Sen1Floods11 las trae ('VV','VH','B2'...), que es lo único robusto entre proveedores.
ALIASES = {
    'VV':    ('VV',),
    'VH':    ('VH',),
    'Blue':  ('BLUE', 'B2', 'B02'),
    'Green': ('GREEN', 'B3', 'B03'),
    'Red':   ('RED', 'B4', 'B04'),
    'NIR':   ('NIR', 'B8', 'B08'),
    'SWIR1': ('SWIR1', 'SWIR', 'B11', 'B011'),
}
FALLBACK_SAR = {'VV': 0, 'VH': 1}
FALLBACK_OPT = {
    13: {'Blue': 1, 'Green': 2, 'Red': 3, 'NIR': 7, 'SWIR1': 11},   # S2 completo
    9:  {'Blue': 0, 'Green': 1, 'Red': 2, 'NIR': 6, 'SWIR1': 8},    # S2 recortado
    5:  {'Blue': 0, 'Green': 1, 'Red': 2, 'NIR': 3, 'SWIR1': 4},    # Landsat 8/9
}
SKIP_SUFFIXES = ('_mask.tif', '_qa.tif', '_cloud.tif', '_label.tif', '_labelhand.tif')
DEFAULT_SPECKLE = 3
BRUSH_MIN, BRUSH_MAX = 2, 120
LASSO_MIN_STEP = 1.5      # px entre puntos del trazo: evita miles de vértices redundantes


# ── lectura ────────────────────────────────────────────────────────────────────
def band_map(descriptions, count):
    """Devuelve (tipo, mapa_de_bandas, origen). tipo = 'SAR' | 'OPTICAL'."""
    desc = {}
    for i, d in enumerate(descriptions or []):
        if d:
            desc[str(d).strip().upper()] = i

    def by_name(names):
        out = {}
        for n in names:
            for alias in ALIASES[n]:
                if alias in desc:
                    out[n] = desc[alias]
                    break
        return out

    sar = by_name(['VV', 'VH'])
    if 'VV' in sar:
        return 'SAR', sar, 'descripciones del TIFF'
    opt = by_name(['Blue', 'Green', 'Red', 'NIR', 'SWIR1'])
    if len(opt) == 5:
        return 'OPTICAL', opt, 'descripciones del TIFF'

    if count <= 3:
        m = dict(FALLBACK_SAR) if count > 1 else {'VV': 0}
        return 'SAR', m, 'orden posicional (asumido SAR)'
    fb = FALLBACK_OPT.get(count)
    if fb:
        return 'OPTICAL', dict(fb), f'orden posicional ({count} bandas)'
    return 'OPTICAL', dict(FALLBACK_OPT[13]), 'orden posicional (asumido S2 de 13 bandas)'


def stretch(arr, lo=2, hi=98):
    """Percentil robusto → [0,1]. Funciona con dB (negativos) y con reflectancia."""
    v = arr[np.isfinite(arr)]
    if v.size < 10:
        return np.zeros_like(arr)
    p_lo, p_hi = np.percentile(v, [lo, hi])
    if p_hi - p_lo < 1e-9:
        return np.zeros_like(arr)
    return np.clip((arr - p_lo) / (p_hi - p_lo), 0, 1)


def to_db(arr):
    """SAR a dB. Si ya viene en dB (mediana negativa) no toca nada."""
    v = arr[np.isfinite(arr)]
    if v.size == 0 or np.percentile(v, 50) < 0:
        return arr, 'ya en dB'
    factor = 10.0 if np.percentile(v, 99.9) <= 5 else 20.0
    with np.errstate(divide='ignore', invalid='ignore'):
        return factor * np.log10(np.where(arr > 0, arr, np.nan)), \
            ('lineal → dB' if factor == 10.0 else 'DN → dB relativo')


def despeckle(arr, size):
    """Mediana NxN preservando NaN. Solo para ver mejor: no altera lo que guardás."""
    if size <= 1 or median_filter is None:
        return arr
    bad = ~np.isfinite(arr)
    if bad.all():
        return arr
    out = median_filter(np.where(bad, np.nanmedian(arr), arr), size=size)
    out[bad] = np.nan
    return out


def shoelace_px(verts):
    """Área de un polígono en px², por la fórmula del cordón. O(n) en vértices,
    así que se puede recalcular en cada movimiento del mouse sin que lagee."""
    if len(verts) < 3:
        return 0.0
    a = np.asarray(verts, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def parse_date(name):
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', name)
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    m = re.search(r'(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])', name)
    if m:
        s = m.group(0)
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    return ''


def parse_time(name):
    m = re.search(r'T(\d{2})(\d{2})(\d{2})(?:\D|$)', name)
    return f'{m.group(1)}:{m.group(2)}:{m.group(3)}' if m else ''


def pixel_size_m(src):
    res = abs(src.transform.a)
    if src.crs and src.crs.is_projected:
        return round(res, 2)
    lat = src.transform.f + src.transform.e * src.height / 2.0
    return round(res * 111320.0 * math.cos(math.radians(abs(lat))), 2)


def list_scenes(d):
    files = sorted(glob.glob(os.path.join(d, '*.tif')) + glob.glob(os.path.join(d, '*.tiff')))
    return [f for f in files if not f.lower().endswith(SKIP_SUFFIXES)]


def ring_signed_area(ring):
    """Área con signo (fórmula del cordón). El signo da la orientación."""
    a = np.asarray(ring, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def compound_path(rings):
    """Path de matplotlib con exterior + agujeros. El relleno de Agg usa regla
    non-zero winding, así que los interiores tienen que venir con orientación
    opuesta al exterior para que se vean vacíos (lo garantiza mask_to_polygons)."""
    verts, codes = [], []
    for r in rings:
        verts += list(r) + [r[0]]
        codes += [Path.MOVETO] + [Path.LINETO] * (len(r) - 1) + [Path.CLOSEPOLY]
    return Path(verts, codes)


def saved_features(geojson_path):
    """Features ya guardadas, o lista vacía si el archivo no existe o está roto.
    No revienta acá: si el GeoJSON es ilegible, _save lo respalda al guardar."""
    if not os.path.exists(geojson_path):
        return []
    try:
        with open(geojson_path) as f:
            return json.load(f).get('features', []) or []
    except Exception:
        return []


def labeled_scene_ids(geojson_path, csv_path):
    ids = {f.get('properties', {}).get('scene_id') for f in saved_features(geojson_path)}
    if os.path.exists(csv_path):
        try:
            ids |= set(pd.read_csv(csv_path, usecols=['scene_id'])['scene_id'])
        except Exception:
            pass
    return {i for i in ids if i}


def mask_to_polygons(mask):
    """Máscara booleana → lista de polígonos en coords de píxel; cada polígono es
    [anillo_exterior, *anillos_interiores]. Se usa para que el pincel termine
    guardando geometría igual que las otras herramientas, en vez de un formato de
    salida aparte.

    rio_shapes trabaja en coords de ESQUINA de píxel, mientras que el resto de la
    app usa coords de CENTRO (como imshow: el entero N es el centro del píxel N).
    De ahí el −0.5: sin ese ajuste los anillos caen entre los centros, un trazo de
    1 px de ancho cuenta 0 píxeles y se descarta sin aviso, y todo lo demás queda
    corrido medio píxel.
    """
    out = []
    for geom, _ in rio_shapes(mask.astype(np.uint8), mask=mask,
                              transform=rasterio.Affine.identity()):
        rings = [[(float(x) - 0.5, float(y) - 0.5) for x, y in r[:-1]]
                 for r in geom['coordinates'] if len(r) >= 4]
        if not rings:
            continue
        # rio_shapes devuelve exterior e interiores con la misma orientación
        sign_ext = ring_signed_area(rings[0])
        rings = [rings[0]] + [r if ring_signed_area(r) * sign_ext < 0 else r[::-1]
                              for r in rings[1:]]
        out.append(rings)
    return out


# ── app ────────────────────────────────────────────────────────────────────────
class FloodLabelApp:

    def __init__(self, scenes, csv_path, geojson_path, event_id='',
                 speckle=DEFAULT_SPECKLE, rasterize=False):
        self.scenes = scenes
        self.csv_path = csv_path
        self.geojson_path = geojson_path
        self.event_id = event_id
        self.speckle = int(speckle or 0)
        self.rasterize = rasterize
        self.smooth = self.speckle > 1

        self.idx = 0
        self.current_class = 'water'
        self.current_tool = 'lazo'
        self.brush_r = 14
        self.points = []
        self.polygons = []
        self.view = 0

        # estado del dibujo en curso
        self._draw_verts = []
        self._drawing = False
        self._brush_mask = None
        self._artists = []
        self._live_line = None
        self._live_patch = None
        self._brush_img = None
        self._brush_rgba = None
        self._brush_cursor = None

        self._build_figure()
        self._load()
        plt.show()

    # ── carga de escena ────────────────────────────────────────────────────────
    def _load(self):
        path = self.scenes[self.idx]
        name = os.path.basename(path)
        self.path = path
        self.points, self.polygons = [], []
        self.saved_polygons, self.saved_points = [], []
        self.view = 0
        self._cancel_draw()
        self._brush_rgba = None                  # cambia el tamaño entre escenas

        with rasterio.open(path) as src:
            raw = src.read().astype(np.float32)
            self.H, self.W = src.height, src.width
            self.crs, self.transform = src.crs, src.transform
            self.pixel_size_m = pixel_size_m(src)
            nd = src.nodata
            kind, bidx, origin = band_map(src.descriptions, src.count)
            n_bands = src.count

        if nd is not None and not np.isnan(nd):
            raw[raw == nd] = np.nan
        raw[~np.isfinite(raw)] = np.nan

        self.kind = kind
        self.scene_id = os.path.splitext(name)[0]
        self.date = parse_date(name)
        self.time_utc = parse_time(name)

        print(f'\n[{self.idx + 1}/{len(self.scenes)}] {name}')
        print(f'  {n_bands} bandas · {kind} · bandas por {origin} · {self.pixel_size_m} m/px')
        if self.crs is None:
            print('  ⚠ el TIFF no declara CRS: las etiquetas se van a guardar con las '
                  'coordenadas crudas del transform, NO son lat/lon usables.')
        self._load_saved()

        if kind == 'SAR':
            self._prep_sar(raw, bidx)
        else:
            self._prep_optical(raw, bidx)
        self._render()

    def _load_saved(self):
        """Trae lo ya guardado para ESTA escena y lo deja listo para dibujar en
        gris punteado. Sin esto, volver a una escena ya etiquetada (con ◄ o en otra
        corrida) no muestra nada y todo lo que dibujes se anexa duplicado."""
        for f in saved_features(self.geojson_path):
            if f.get('properties', {}).get('scene_id') != self.scene_id:
                continue
            coords = (f.get('geometry') or {}).get('coordinates') or []
            rings = []
            for ring in coords:
                px = self._px_from_lonlat([c[0] for c in ring], [c[1] for c in ring])
                if len(px) >= 4:
                    rings.append(px[:-1] if px[0] == px[-1] else px)
            if rings:
                self.saved_polygons.append({'class': f['properties'].get('class', 'water'),
                                            'n_px': f['properties'].get('n_pixels', 0),
                                            'rings': rings})
        if os.path.exists(self.csv_path):
            try:
                df = pd.read_csv(self.csv_path)
                if {'scene_id', 'row', 'col', 'class'} <= set(df.columns):
                    d = df[df['scene_id'] == self.scene_id]
                    self.saved_points = list(zip(d['row'], d['col'], d['class']))
            except Exception as ex:
                print(f'  ⚠ no pude leer {os.path.basename(self.csv_path)}: {ex}')

        if self.saved_polygons or self.saved_points:
            ha = sum(p['n_px'] for p in self.saved_polygons) * self.pixel_size_m ** 2 / 1e4
            print(f'  ✋ esta escena YA tiene {len(self.saved_polygons)} polígonos '
                  f'({ha:.1f} ha) + {len(self.saved_points)} puntos guardados — se '
                  f'dibujan punteados. Lo que agregues se suma, no los reemplaza.')

    def _prep_sar(self, raw, bidx):
        vv, note = to_db(raw[bidx['VV']])
        self.vv = vv
        self.vh = None
        if 'VH' in bidx and bidx['VH'] < raw.shape[0]:
            self.vh, _ = to_db(raw[bidx['VH']])
        self.bands_txt = 'VV+VH' if self.vh is not None else 'VV'
        print(f'  {self.bands_txt} · {note} · rango '
              f'{np.nanpercentile(vv, 1):.1f}..{np.nanpercentile(vv, 99):.1f} dB')
        self.views = ['VV (dB)'] + (['VH (dB)', 'falso color R=VV G=VH B=VV−VH']
                                    if self.vh is not None else [])

    def _prep_optical(self, raw, bidx):
        v = raw[np.isfinite(raw)]
        if v.size and np.percentile(v, 99) > 100:
            raw = raw / 10000.0
            print('  reflectancia ×10000 → [0,1]')
        raw = np.where((raw < -0.5) | (raw > 2.0), np.nan, raw)
        self.R, self.G, self.B = raw[bidx['Red']], raw[bidx['Green']], raw[bidx['Blue']]
        self.N, self.S1 = raw[bidx['NIR']], raw[bidx['SWIR1']]
        self.bands_txt = 'RGB+NIR+SWIR1'
        self.vv = self.vh = None
        # SWIR/NIR/Green: el compuesto estándar para inundaciones. El agua absorbe
        # casi todo el SWIR, así que sale negra y contrasta con todo lo demás.
        self.views = ['falso color SWIR/NIR/Green (agua = negro)', 'RGB color natural']

    def _image(self):
        s = self.speckle if self.smooth else 0
        label = self.views[self.view % len(self.views)]
        if self.kind == 'SAR':
            if label.startswith('VV'):
                img = np.dstack([stretch(despeckle(self.vv, s))] * 3)
            elif label.startswith('VH'):
                img = np.dstack([stretch(despeckle(self.vh, s))] * 3)
            else:
                vv, vh = despeckle(self.vv, s), despeckle(self.vh, s)
                img = np.dstack([stretch(vv), stretch(vh), stretch(vv - vh)])
        else:
            if label.startswith('falso'):
                img = np.dstack([stretch(self.S1), stretch(self.N), stretch(self.G)])
            else:
                img = np.dstack([stretch(self.R), stretch(self.G), stretch(self.B)])
        return np.nan_to_num(img), label

    # ── figura ─────────────────────────────────────────────────────────────────
    def _build_figure(self):
        self.fig = plt.figure(figsize=(13.5, 9))
        self.fig.patch.set_facecolor('#12121f')
        self.ax = self.fig.add_axes([0.05, 0.15, 0.71, 0.76])
        self.ax.set_facecolor('#0d0d1a')

        self.ttl = self.fig.text(0.5, 0.985, '', ha='center', va='top',
                                 color='white', fontsize=11, fontweight='bold')
        self.sub = self.fig.text(0.5, 0.957, '', ha='center', va='top',
                                 color='#8888aa', fontsize=8.5)

        ax_cls = self.fig.add_axes([0.79, 0.68, 0.19, 0.23])
        ax_cls.set_facecolor('#1a1a2e')
        ax_cls.set_title('Clase  (1-4)', color='#aaaaaa', fontsize=9, pad=4)
        self.radio_cls = RadioButtons(ax_cls, CLASSES, activecolor='#0a84ff')
        for i, lbl in enumerate(self.radio_cls.labels):
            lbl.set_color(CLASS_COLORS[CLASSES[i]]); lbl.set_fontsize(9)
        self.radio_cls.on_clicked(self._on_class)

        ax_tool = self.fig.add_axes([0.79, 0.44, 0.19, 0.21])
        ax_tool.set_facecolor('#1a1a2e')
        ax_tool.set_title('Herramienta  (z x c v)', color='#aaaaaa', fontsize=9, pad=4)
        self.radio_tool = RadioButtons(ax_tool, TOOLS, activecolor='#44aaff')
        for lbl in self.radio_tool.labels:
            lbl.set_color('white'); lbl.set_fontsize(9)
        self.radio_tool.on_clicked(self._on_tool)

        kw = dict(color='#22223a', hovercolor='#33335a')

        def btn(rect, label, cb, fs=9):
            a = self.fig.add_axes(rect)
            b = Button(a, label, **kw)
            b.label.set_color('white'); b.label.set_fontsize(fs)
            b.on_clicked(cb)
            return b

        self.b_save = btn([0.79, 0.32, 0.19, 0.075], 'Guardar → Siguiente', self._save_next)
        self.b_skip = btn([0.79, 0.255, 0.19, 0.05], 'Saltar sin guardar', self._next, 8)
        self.b_undo = btn([0.79, 0.195, 0.09, 0.05], 'Deshacer', self._undo, 8)
        self.b_clr = btn([0.89, 0.195, 0.09, 0.05], 'Limpiar', self._clear, 8)
        self.b_prev = btn([0.79, 0.135, 0.19, 0.05], '◄ Imagen anterior', self._prev, 8)

        self.status = self.fig.text(0.05, 0.107, '', color='#aaaacc', fontsize=9)
        self.help = self.fig.text(0.05, 0.068, '', color='#666688', fontsize=7.5)
        self.outtxt = self.fig.text(0.05, 0.04, '', color='#666688', fontsize=7.5)

        c = self.fig.canvas
        c.mpl_connect('button_press_event', self._on_press)
        c.mpl_connect('motion_notify_event', self._on_motion)
        c.mpl_connect('button_release_event', self._on_release)
        c.mpl_connect('scroll_event', self._on_scroll)
        c.mpl_connect('key_press_event', self._on_key)

    def _render(self):
        self.ax.clear()
        self._artists = []
        self._live_line = self._live_patch = None
        self._brush_img = self._brush_cursor = None
        img, label = self._image()
        self.ax.imshow(img, origin='upper', aspect='equal', interpolation='nearest')
        self.ax.set_title(label, color='#9999bb', fontsize=10, pad=6)
        self.ax.axis('off')

        # texto de área en vivo, arriba a la izquierda de la imagen
        self.live_txt = self.ax.text(
            0.015, 0.985, '', transform=self.ax.transAxes, va='top', ha='left',
            fontsize=10, color='white', zorder=10, visible=False,
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#12121fdd', edgecolor='none'))

        sm = f' · speckle suavizado {self.speckle}x{self.speckle}' if self.smooth else ''
        self.ttl.set_text(f'{self.scene_id}   [{self.idx + 1}/{len(self.scenes)}]')
        self.sub.set_text(f'{self.kind} · {self.bands_txt} · {self.pixel_size_m} m/px'
                          f'{" · " + self.date if self.date else ""}{sm}')
        self._update_help()
        self.outtxt.set_text(f'guardando en: {os.path.relpath(self.geojson_path, HERE)}  +  '
                             f'{os.path.relpath(self.csv_path, HERE)}')
        self._draw_labels()
        self.fig.canvas.draw_idle()

    def _update_help(self):
        tips = {
            'lazo': 'LAZO: apretá y dibujá el contorno · soltá para cerrar',
            'polígono': 'POLÍGONO: clic por vértice · clic derecho o doble clic cierra · Esc cancela',
            'pincel': f'PINCEL: arrastrá para pintar · rueda del mouse = radio ({self.brush_r} px)',
            'click': 'CLICK: un clic marca un píxel suelto',
        }
        self.help.set_text(tips[self.current_tool] +
                           '   |   1-4 clase · z x c v herramienta · a bandas · m suavizar · u deshacer')

    def _draw_labels(self):
        for a in self._artists:
            try: a.remove()
            except Exception: pass
        self._artists = []
        # lo ya guardado en disco: punteado, sin relleno, no se puede editar ni
        # deshacer desde acá — está para que no lo vuelvas a etiquetar
        for poly in self.saved_polygons:
            s = PathPatch(compound_path(poly['rings']), fill=False,
                          edgecolor=CLASS_COLORS[poly['class']], linewidth=1.4,
                          linestyle=(0, (4, 3)), alpha=0.75, zorder=3)
            self.ax.add_patch(s)
            self._artists.append(s)
        for cls in CLASSES:
            pts = [(c, r) for r, c, cl in self.saved_points if cl == cls]
            if pts:
                xs, ys = zip(*pts)
                self._artists.append(self.ax.scatter(
                    xs, ys, s=34, zorder=3, facecolors='none', linewidths=1.1,
                    edgecolors=CLASS_COLORS[cls], marker='o', alpha=0.75))
        for poly in self.polygons:
            c = CLASS_COLORS[poly['class']]
            path = compound_path([poly['verts']] + poly.get('holes', []))
            p = PathPatch(path, facecolor=c, edgecolor='white',
                          alpha=0.40, linewidth=1.2, zorder=4)
            self.ax.add_patch(p)
            self._artists.append(p)
            # contorno del color de la clase por encima del borde blanco: se lee
            # bien tanto sobre agua oscura como sobre tierra clara
            o = PathPatch(path, fill=False, edgecolor=c,
                          linewidth=2.2, alpha=0.95, zorder=5)
            self.ax.add_patch(o)
            self._artists.append(o)
        for cls in CLASSES:
            pts = [(c, r) for r, c, cl in self.points if cl == cls]
            if pts:
                xs, ys = zip(*pts)
                self._artists.append(self.ax.scatter(
                    xs, ys, c=CLASS_COLORS[cls], s=28, zorder=6, linewidths=0.8,
                    edgecolors='white', marker='o'))
        self._update_status()
        self.fig.canvas.draw_idle()

    def _update_status(self):
        parts = []
        for cls in CLASSES:
            npx = sum(p['n_px'] for p in self.polygons if p['class'] == cls)
            npt = sum(1 for _, _, c in self.points if c == cls)
            if npx or npt:
                bits = []
                if npx:
                    bits.append(f'{npx * self.pixel_size_m ** 2 / 1e4:.1f} ha')
                if npt:
                    bits.append(f'{npt} pt')
                parts.append(f'{cls}: ' + ' + '.join(bits))
        txt = '   |   '.join(parts) if parts else 'sin etiquetas nuevas en esta imagen'
        if self.saved_polygons or self.saved_points:
            ha = sum(p['n_px'] for p in self.saved_polygons) * self.pixel_size_m ** 2 / 1e4
            txt += (f'      ⟨ya guardado: {ha:.1f} ha'
                    + (f' + {len(self.saved_points)} pt' if self.saved_points else '') + '⟩')
        self.status.set_text(txt)

    # ── dibujo en vivo ─────────────────────────────────────────────────────────
    def _color(self):
        return CLASS_COLORS[self.current_class]

    def _show_live(self, verts, closed):
        """Relleno + contorno del color de la clase mientras dibujás, con el área."""
        c = self._color()
        if self._live_patch is None:
            self._live_patch = MplPolygon([(0, 0), (0, 0), (0, 0)], closed=True,
                                          facecolor=c, alpha=0.35, edgecolor='none',
                                          zorder=7)
            self.ax.add_patch(self._live_patch)
            self._live_line = Line2D([], [], color=c, linewidth=2.0, alpha=0.95,
                                     marker='o', markersize=4, markerfacecolor='white',
                                     markeredgecolor=c, zorder=8)
            self.ax.add_line(self._live_line)
        self._live_patch.set_facecolor(c)
        self._live_line.set_color(c)
        self._live_line.set_markeredgecolor(c)
        # el lazo tiene cientos de puntos: sin marcadores se ve como un trazo limpio
        self._live_line.set_marker('o' if self.current_tool == 'polígono' else '')

        if len(verts) >= 3:
            self._live_patch.set_xy(verts)
            self._live_patch.set_visible(True)
        else:
            self._live_patch.set_visible(False)
        xs = [v[0] for v in verts] + ([verts[0][0]] if closed and verts else [])
        ys = [v[1] for v in verts] + ([verts[0][1]] if closed and verts else [])
        self._live_line.set_data(xs, ys)

        ha = shoelace_px(verts) * self.pixel_size_m ** 2 / 1e4
        extra = f'{len(verts)} vértices · ' if self.current_tool == 'polígono' else ''
        self.live_txt.set_text(f'{self.current_class}  ·  {extra}{ha:.1f} ha')
        self.live_txt.set_color(c)
        self.live_txt.set_visible(True)
        self.fig.canvas.draw_idle()

    def _show_brush(self):
        if self._brush_mask is None:
            return
        c = matplotlib.colors.to_rgb(self._color())
        # el buffer se aloca una vez por escena y se escribe en el lugar: antes se
        # creaba un array H×W×4 nuevo en cada movimiento del mouse, lo que en una
        # escena grande son cientos de MB por evento. Como la máscara sólo crece
        # durante el trazo, reescribir los píxeles pintados alcanza — y además
        # sigue el cambio de clase a mitad del trazo.
        if self._brush_rgba is None:
            self._brush_rgba = np.zeros((self.H, self.W, 4), np.float32)
        self._brush_rgba[self._brush_mask] = (*c, 0.45)
        if self._brush_img is None:
            self._brush_img = self.ax.imshow(self._brush_rgba, origin='upper',
                                             interpolation='nearest', zorder=7)
        else:
            self._brush_img.set_data(self._brush_rgba)
        ha = int(self._brush_mask.sum()) * self.pixel_size_m ** 2 / 1e4
        self.live_txt.set_text(f'{self.current_class}  ·  {ha:.1f} ha')
        self.live_txt.set_color(self._color())
        self.live_txt.set_visible(True)
        self.fig.canvas.draw_idle()

    def _cancel_draw(self):
        self._draw_verts = []
        self._drawing = False
        if self._brush_mask is not None and self._brush_rgba is not None:
            self._brush_rgba[self._brush_mask] = 0      # una vez por trazo, no por evento
        self._brush_mask = None
        for a in (self._live_patch, self._live_line, self._brush_img):
            if a is not None:
                try: a.remove()
                except Exception: pass
        self._live_patch = self._live_line = self._brush_img = None
        if getattr(self, 'live_txt', None) is not None:
            self.live_txt.set_visible(False)
        if self.fig.canvas is not None:
            self.fig.canvas.draw_idle()

    def _paint(self, x, y):
        if self._brush_mask is None:
            self._brush_mask = np.zeros((self.H, self.W), bool)
        r = self.brush_r
        c0, c1 = max(0, int(x - r)), min(self.W, int(x + r) + 1)
        r0, r1 = max(0, int(y - r)), min(self.H, int(y + r) + 1)
        if c1 <= c0 or r1 <= r0:
            return
        cc, rr = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
        self._brush_mask[r0:r1, c0:c1] |= (cc - x) ** 2 + (rr - y) ** 2 <= r * r

    # ── eventos ────────────────────────────────────────────────────────────────
    def _in_ax(self, event):
        return event.inaxes is self.ax and event.xdata is not None

    def _on_press(self, event):
        if not self._in_ax(event):
            return
        x, y = event.xdata, event.ydata
        tool = self.current_tool

        if tool == 'click':
            if event.button == 1:
                c, r = int(round(x)), int(round(y))
                if 0 <= r < self.H and 0 <= c < self.W:
                    self.points.append((r, c, self.current_class))
                    self._draw_labels()
            return

        if tool == 'polígono':
            if event.button == 3 or event.dblclick:      # clic derecho / doble = cerrar
                self._finish_polygon(self._draw_verts)
            elif event.button == 1:
                self._draw_verts.append((x, y))
                self._show_live(self._draw_verts, closed=False)
            return

        if event.button != 1:
            return
        if tool == 'lazo':
            self._drawing = True
            self._draw_verts = [(x, y)]
            self._show_live(self._draw_verts, closed=False)
        elif tool == 'pincel':
            self._drawing = True
            self._paint(x, y)
            self._show_brush()

    def _on_motion(self, event):
        if not self._in_ax(event):
            return
        x, y = event.xdata, event.ydata

        if self.current_tool == 'pincel':
            self._move_brush_cursor(x, y)
            if self._drawing:
                self._paint(x, y)
                self._show_brush()
            return

        if self.current_tool == 'lazo' and self._drawing:
            lx, ly = self._draw_verts[-1]
            if (x - lx) ** 2 + (y - ly) ** 2 >= LASSO_MIN_STEP ** 2:
                self._draw_verts.append((x, y))
                self._show_live(self._draw_verts, closed=True)
        elif self.current_tool == 'polígono' and self._draw_verts:
            # banda elástica: el contorno sigue al cursor hasta el próximo clic
            self._show_live(self._draw_verts + [(x, y)], closed=True)

    def _on_release(self, event):
        if not self._drawing:
            return
        if self.current_tool == 'lazo':
            self._drawing = False
            self._finish_polygon(self._draw_verts)
        elif self.current_tool == 'pincel':
            self._drawing = False
            mask = self._brush_mask
            self._cancel_draw()
            if mask is not None and mask.any():
                added = 0
                for rings in mask_to_polygons(mask):
                    added += self._add_polygon(rings[0], rings[1:], quiet=True)
                if added:
                    print(f'  + {self.current_class}: {added} px = '
                          f'{added * self.pixel_size_m ** 2 / 1e4:.1f} ha (pincel)')
                self._draw_labels()

    def _on_scroll(self, event):
        if self.current_tool != 'pincel' or not self._in_ax(event):
            return
        self.brush_r = int(np.clip(self.brush_r + (2 if event.step > 0 else -2),
                                   BRUSH_MIN, BRUSH_MAX))
        self._move_brush_cursor(event.xdata, event.ydata)
        self._update_help()
        self.fig.canvas.draw_idle()

    def _move_brush_cursor(self, x, y):
        if self._brush_cursor is None:
            self._brush_cursor = Circle((x, y), self.brush_r, fill=False,
                                        edgecolor=self._color(), linewidth=1.6,
                                        alpha=0.9, zorder=9)
            self.ax.add_patch(self._brush_cursor)
        self._brush_cursor.set_center((x, y))
        self._brush_cursor.set_radius(self.brush_r)
        self._brush_cursor.set_edgecolor(self._color())
        self._brush_cursor.set_visible(True)
        self.fig.canvas.draw_idle()

    def _finish_polygon(self, verts):
        verts = list(verts)
        self._cancel_draw()
        if len(verts) >= 3:
            self._add_polygon(verts)
            self._draw_labels()

    def _pixels_in(self, verts, holes=()):
        """Centros de píxel dentro del anillo exterior y fuera de los interiores,
        como array de pares (col, row). Path.contains_points no descuenta agujeros
        por sí solo (probado: los rellena con cualquier orientación), así que los
        interiores se restan a mano."""
        va = np.array(verts, float)
        c0, c1 = max(0, int(va[:, 0].min())), min(self.W - 1, int(np.ceil(va[:, 0].max())))
        r0, r1 = max(0, int(va[:, 1].min())), min(self.H - 1, int(np.ceil(va[:, 1].max())))
        if c1 < c0 or r1 < r0:
            return np.empty((0, 2), int)
        cols, rows = np.meshgrid(np.arange(c0, c1 + 1), np.arange(r0, r1 + 1))
        st = np.column_stack([cols.ravel(), rows.ravel()])
        keep = Path(verts).contains_points(st)
        for h in holes:
            if len(h) >= 3:
                keep &= ~Path(h).contains_points(st)
        return st[keep]

    def _add_polygon(self, verts, holes=(), quiet=False):
        """Guarda el polígono y devuelve su cantidad exacta de píxeles."""
        if len(verts) < 3:
            return 0
        n_px = len(self._pixels_in(verts, holes))
        if n_px == 0:
            return 0
        self.polygons.append({
            'class': self.current_class, 'n_px': n_px,
            'verts': [(float(x), float(y)) for x, y in verts],
            'holes': [[(float(x), float(y)) for x, y in h] for h in holes if len(h) >= 3]})
        if not quiet:
            print(f'  + {self.current_class}: {n_px} px = '
                  f'{n_px * self.pixel_size_m ** 2 / 1e4:.1f} ha ({self.current_tool})')
        return n_px

    def _on_class(self, label):
        self.current_class = label
        if self._draw_verts:
            self._show_live(self._draw_verts, closed=self.current_tool != 'polígono')
        if self._brush_cursor is not None:
            self._brush_cursor.set_edgecolor(self._color())
        self.fig.canvas.draw_idle()

    def _on_tool(self, label):
        self._cancel_draw()
        self.current_tool = label
        if self._brush_cursor is not None:
            self._brush_cursor.set_visible(label == 'pincel')
        self._update_help()
        self.fig.canvas.draw_idle()

    def _on_key(self, event):
        k = event.key
        if k in ('1', '2', '3', '4'):
            i = int(k) - 1
            self.radio_cls.set_active(i)          # dispara _on_class
        elif k in TOOL_KEYS:
            self.radio_tool.set_active(TOOLS.index(TOOL_KEYS[k]))   # dispara _on_tool
        elif k == 'escape':
            self._cancel_draw()
        elif k == 'enter' and self.current_tool == 'polígono':
            self._finish_polygon(self._draw_verts)
        elif k == 'a':
            self.view = (self.view + 1) % len(self.views)
            self._render()
        elif k == 'm':
            self.smooth = not self.smooth
            self._render()
        elif k == 'u':
            self._undo(None)

    def _undo(self, _):
        if self._draw_verts or self._brush_mask is not None:
            self._cancel_draw()                    # primero cancela lo que estás dibujando
            return
        if self.polygons:
            p = self.polygons.pop()
            print(f'  – deshecho: {p["class"]}')
        elif self.points:
            self.points.pop()
        self._draw_labels()

    def _clear(self, _):
        self._cancel_draw()
        self.points, self.polygons = [], []
        self._draw_labels()

    # ── guardado ───────────────────────────────────────────────────────────────
    def _lonlat(self, cols, rows):
        """(col, row) en convención de CENTRO de píxel → lon/lat EPSG:4326.
        Una sola convención para clicks (enteros) y vértices (floats):
        transform.xy hace transform * (col + 0.5, row + 0.5) y acepta floats,
        verificado bit a bit. Antes los polígonos usaban transform * (col, row),
        que apunta a la esquina y los corría medio píxel."""
        xs, ys = rasterio.transform.xy(self.transform, list(rows), list(cols))
        if self.crs and not self.crs.is_geographic:
            return warp_transform(self.crs, CRS.from_epsg(4326), xs, ys)
        return xs, ys

    def _px_from_lonlat(self, lons, lats):
        """Inversa de _lonlat: lon/lat → (col, row) en coords de centro de píxel.
        Se usa para redibujar las etiquetas ya guardadas de esta escena."""
        if self.crs and not self.crs.is_geographic:
            xs, ys = warp_transform(CRS.from_epsg(4326), self.crs, list(lons), list(lats))
        else:
            xs, ys = list(lons), list(lats)
        inv = ~self.transform
        out = []
        for x, y in zip(xs, ys):
            c, r = inv * (x, y)
            out.append((c - 0.5, r - 0.5))
        return out

    def _props(self):
        return {'scene_id': self.scene_id, 'image_type': self.kind,
                'pixel_size_m': self.pixel_size_m, 'date': self.date,
                'time_utc': self.time_utc, 'event_id': self.event_id,
                'image_path': os.path.relpath(self.path, HERE)}

    def _save(self):
        self._cancel_draw()
        if not self.polygons and not self.points:
            print('  (nada que guardar en esta imagen)')
            return False

        if self.polygons:
            fc = {'type': 'FeatureCollection', 'features': []}
            if os.path.exists(self.geojson_path):
                try:
                    with open(self.geojson_path) as f:
                        fc = json.load(f)
                    fc.setdefault('features', [])
                except Exception as ex:
                    bak = self.geojson_path + '.bak'
                    n = 1
                    while os.path.exists(bak):          # no pisar un respaldo previo
                        bak = f'{self.geojson_path}.bak-{n}'
                        n += 1
                    os.rename(self.geojson_path, bak)
                    print(f'  ⚠ GeoJSON ilegible ({ex}) → lo moví a {bak}')
                    fc = {'type': 'FeatureCollection', 'features': []}
            n0 = len(fc['features'])
            for i, poly in enumerate(self.polygons):
                coords = []
                for r in [poly['verts']] + poly.get('holes', []):
                    lons, lats = self._lonlat([v[0] for v in r], [v[1] for v in r])
                    ring = [[round(float(x), 7), round(float(y), 7)]
                            for x, y in zip(lons, lats)]
                    if ring[0] != ring[-1]:
                        ring.append(ring[0])
                    coords.append(ring)
                pr = self._props()
                pr.update({'class': poly['class'], 'n_pixels': poly['n_px'],
                           'area_ha': round(poly['n_px'] * self.pixel_size_m ** 2 / 1e4, 3),
                           'geom_id': f'{self.scene_id}#{n0 + i}'})
                fc['features'].append({'type': 'Feature', 'properties': pr,
                                       'geometry': {'type': 'Polygon',
                                                    'coordinates': coords}})
            tmp = self.geojson_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(fc, f)
            os.replace(tmp, self.geojson_path)

        pts = list(self.points)
        if self.rasterize:
            for poly in self.polygons:
                px = self._pixels_in(poly['verts'], poly.get('holes', []))
                pts += [(int(r), int(c), poly['class']) for c, r in px]
        n_pts = 0
        if pts:
            lons, lats = self._lonlat([c for _, c, _ in pts], [r for r, _, _ in pts])
            rows_out = []
            for (r, c, cls), lon, lat in zip(pts, lons, lats):
                rec = self._props()
                rec.update({'row': r, 'col': c, 'lat': round(float(lat), 6),
                            'lon': round(float(lon), 6), 'class': cls})
                rows_out.append(rec)
            # un CSV existente pero vacío (0 bytes) igual necesita encabezado
            has_header = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0
            pd.DataFrame(rows_out).to_csv(
                self.csv_path, mode='a', index=False, header=not has_header)
            n_pts = len(rows_out)

        ha = sum(p['n_px'] for p in self.polygons) * self.pixel_size_m ** 2 / 1e4
        print(f'  ✓ guardado: {len(self.polygons)} polígonos ({ha:.1f} ha)'
              + (f' + {n_pts} puntos' if n_pts else ''))
        # pasan a "ya guardado": siguen visibles (punteadas) pero volver a apretar
        # Guardar no las duplica
        for poly in self.polygons:
            self.saved_polygons.append({'class': poly['class'], 'n_px': poly['n_px'],
                                        'rings': [poly['verts']] + poly.get('holes', [])})
        self.saved_points += self.points
        self.polygons, self.points = [], []
        self._draw_labels()
        return True

    def _save_next(self, _):
        self._save()
        self._go(self.idx + 1)

    def _next(self, _):
        self._go(self.idx + 1)

    def _prev(self, _):
        self._go(self.idx - 1)

    def _go(self, i):
        if i < 0:
            print('  (ya estás en la primera imagen)')
            return
        if i >= len(self.scenes):
            print('\n¡Listo! No hay más imágenes. Podés cerrar la ventana.')
            self.ttl.set_text('¡Listo! No hay más imágenes — podés cerrar la ventana')
            self.fig.canvas.draw_idle()
            return
        self.idx = i
        self._load()


def main():
    p = argparse.ArgumentParser(
        description='Etiquetado manual de inundaciones sobre imágenes SAR u ópticas')
    p.add_argument('--dir', default='data/SAR', help='Carpeta con los .tif (default: data/SAR)')
    p.add_argument('--event-id', default='', help='Nombre del evento, se guarda en cada etiqueta')
    p.add_argument('--speckle', type=int, default=DEFAULT_SPECKLE,
                   help=f'Ventana del suavizado visual de speckle en SAR '
                        f'(default {DEFAULT_SPECKLE}, 0 = ninguno). Solo afecta lo que ves.')
    p.add_argument('--rasterize', action='store_true',
                   help='Además de la geometría, escribe cada píxel del polígono al CSV')
    p.add_argument('--skip-labeled', action='store_true',
                   help='Saltea las escenas que ya tienen etiquetas guardadas')
    p.add_argument('--out', default=os.path.join(HERE, 'labels'),
                   help='Carpeta de salida (default: labels/)')
    args = p.parse_args()

    d = args.dir if os.path.isabs(args.dir) else os.path.join(HERE, args.dir)
    scenes = list_scenes(d)
    if not scenes:
        sys.exit(f'No encontré ningún .tif en {d}')

    os.makedirs(args.out, exist_ok=True)
    geojson_path = os.path.join(args.out, 'flood_polygons.geojson')
    csv_path = os.path.join(args.out, 'flood_labels.csv')

    if args.skip_labeled:
        done = labeled_scene_ids(geojson_path, csv_path)
        keep = [s for s in scenes
                if os.path.splitext(os.path.basename(s))[0] not in done]
        if len(keep) < len(scenes):
            print(f'\n--skip-labeled: salteo {len(scenes) - len(keep)} escena(s) ya etiquetada(s)')
        if not keep:
            sys.exit('Todas las escenas de esa carpeta ya están etiquetadas.')
        scenes = keep

    print(f'\n{len(scenes)} imagen(es) en {d}:')
    for i, s in enumerate(scenes, 1):
        print(f'   {i}. {os.path.basename(s)}')
    print(f'\nClases:      ' + '   '.join(f'{i + 1}={c}' for i, c in enumerate(CLASSES)))
    print(f'Herramientas: ' + '   '.join(f'{k}={v}' for k, v in TOOL_KEYS.items()))
    print(f'Salidas:      {geojson_path}\n              {csv_path}\n')

    FloodLabelApp(scenes, csv_path, geojson_path, event_id=args.event_id,
                  speckle=args.speckle, rasterize=args.rasterize)


if __name__ == '__main__':
    main()
