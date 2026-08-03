"""
flood/label.py — Manual flood labelling app (SAR and optical)

YOU draw the labels. The app does not classify anything: it only shows the image
in the best way it can so that you can interpret it, and saves what you mark.

4 classes:
    1 water          water (without distinguishing permanent from flood: that
                     distinction is not reliable by eye from a single image)
    2 dry_land       dry land
    3 cloud          cloud (optical only)
    4 radar_shadow   terrain shadow/layover — looks dark like water but is NOT
                     water (SAR only). It is the main false positive.

4 drawing tools (right-hand panel, or keys z x c v):
    lasso    press and draw the outline freehand; it closes when you release.
             Fastest for water edges, which are irregular curves.
    polygon  one click per vertex. Right click or double click closes; Esc cancels.
    brush    paint by dragging a circle; the mouse wheel changes the radius.
    click    marks a single pixel (control points).

While you draw you see the fill in the class colour and the hectares live.

It only detects whether each .tif is SAR or optical, by reading the band names
from the file.

Outputs (labels/ folder):
    labels/flood_polygons.geojson   the polygons, in lat/lon EPSG:4326
    labels/flood_labels.csv         the single clicks, with row/col and lat/lon

Whatever you already saved for a scene is redrawn dashed when you reopen it, so
the same thing does not get labelled twice. New work is added to what was saved,
it does not replace it.

Usage (from the repository root):
    python flood/label.py                          # reads data/SAR/
    python flood/label.py --dir data/another_folder
    python flood/label.py --event-id bolivia-60373
    python flood/label.py --skip-labeled           # skips already labelled scenes
"""

import os
import sys
import glob
import json
import argparse
import re

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
from rasterio.crs import CRS

import render
from render import DEFAULT_SPECKLE
from geometry import mask_to_polygons, rings_to_coords, shoelace_px
import geometry

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CLASSES = ['water', 'dry_land', 'cloud', 'radar_shadow']
CLASS_COLORS = {
    'water':        '#0a84ff',
    'dry_land':     '#c69c6d',
    'cloud':        '#f2f2f7',
    'radar_shadow': '#bf5af2',
}

TOOLS = ['lazo', 'polígono', 'pincel', 'click']
TOOL_KEYS = {'z': 'lazo', 'x': 'polígono', 'c': 'pincel', 'v': 'click'}

SKIP_SUFFIXES = ('_mask.tif', '_qa.tif', '_cloud.tif', '_label.tif', '_labelhand.tif')
BRUSH_MIN, BRUSH_MAX = 2, 120
LASSO_MIN_STEP = 1.5      # px entre puntos del trazo: evita miles de vértices redundantes


# ── reading ────────────────────────────────────────────────────────────────────
# Band detection, dB conversion, stretching, despeckling and false colour
# composition live in render.py, shared with render_chip.py.


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


def list_scenes(d):
    files = sorted(glob.glob(os.path.join(d, '*.tif')) + glob.glob(os.path.join(d, '*.tiff')))
    return [f for f in files if not f.lower().endswith(SKIP_SUFFIXES)]


def compound_path(rings):
    """Matplotlib path with exterior + holes. The Agg fill uses the rule
    non-zero winding, so the interiors have to come with the opposite
    orientation to the exterior for them to show as empty (mask_to_polygons
    guarantees that)."""
    verts, codes = [], []
    for r in rings:
        verts += list(r) + [r[0]]
        codes += [Path.MOVETO] + [Path.LINETO] * (len(r) - 1) + [Path.CLOSEPOLY]
    return Path(verts, codes)


def saved_features(geojson_path):
    """Features already saved, or an empty list if the file does not exist or is
    broken. It does not blow up here: if the GeoJSON is unreadable, _save backs it
    up when saving."""
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

        # state of the drawing in progress
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

    # ── scene loading ──────────────────────────────────────────────────────────
    def _load(self):
        path = self.scenes[self.idx]
        name = os.path.basename(path)
        self.path = path
        self.points, self.polygons = [], []
        self.saved_polygons, self.saved_points = [], []
        self.view = 0
        self._cancel_draw()
        self._brush_rgba = None                  # cambia el tamaño entre escenas

        self.chip = render.Chip(path)
        c = self.chip
        self.H, self.W = c.height, c.width
        self.crs, self.transform = c.crs, c.transform
        self.pixel_size_m = c.pixel_size_m
        self.kind = c.kind
        self.bands_txt = c.bands_txt
        self.views = c.views
        self.scene_id = c.scene_id
        self.date = parse_date(name)
        self.time_utc = parse_time(name)

        print(f'\n[{self.idx + 1}/{len(self.scenes)}] {name}')
        print(f'  {c.n_bands} bandas · {c.kind} · bandas por {c.band_origin} · '
              f'{self.pixel_size_m} m/px')
        if self.crs is None:
            print('  ⚠ el TIFF no declara CRS: las etiquetas se van a guardar con las '
                  'coordenadas crudas del transform, NO son lat/lon usables.')
        self._load_saved()

        if c.prep_msg:
            print(f'  {c.prep_msg}')
        self._render()

    def _load_saved(self):
        """Brings in what was already saved for THIS scene and gets it ready to be
        drawn in dashed grey. Without this, coming back to an already labelled
        scene (with ◄ or in another run) shows nothing and everything you draw gets
        appended as a duplicate."""
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

    def _image(self):
        return self.chip.image(self.view, self.speckle if self.smooth else 0)

    # ── figure ─────────────────────────────────────────────────────────────────
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

        # live area text, top left of the image
        self.live_txt = self.ax.text(
            0.015, 0.985, '', transform=self.ax.transAxes, va='top', ha='left',
            fontsize=10, color='white', zorder=10, visible=False,
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#12121fdd', edgecolor='none'))

        sm = f' · speckle suavizado {self.speckle}x{self.speckle}' if self.smooth else ''
        self.ttl.set_text(f'{self.scene_id}   [{self.idx + 1}/{len(self.scenes)}]')
        self.sub.set_text(f'{self.kind} · {self.bands_txt} · {self.pixel_size_m} m/px'
                          f'{" · " + self.date if self.date else ""}{sm}')
        self._update_help()
        self.outtxt.set_text(f'guardando en: {os.path.relpath(self.geojson_path, ROOT)}  +  '
                             f'{os.path.relpath(self.csv_path, ROOT)}')
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
        # what is already saved on disk: dashed, no fill, cannot be edited or
        # undone from here — it is there so you do not label it again
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
            # class-coloured outline on top of the white edge: it reads well both
            # over dark water and over light ground
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

    # ── live drawing ───────────────────────────────────────────────────────────
    def _color(self):
        return CLASS_COLORS[self.current_class]

    def _show_live(self, verts, closed):
        """Class-coloured fill + outline while you draw, with the area."""
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
        # the lasso has hundreds of points: without markers it reads as a clean stroke
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
        # the buffer is allocated once per scene and written in place: it used to
        # create a new H×W×4 array on every mouse move, which on a large scene is
        # hundreds of MB per event. Since the mask only grows during the stroke,
        # rewriting the painted pixels is enough — and it also follows a class
        # change midway through the stroke.
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

    # ── events ─────────────────────────────────────────────────────────────────
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
            # rubber band: the outline follows the cursor until the next click
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
        """Pixel centres inside the exterior ring and outside the interiors, as an
        array of (col, row) pairs. Path.contains_points does not discount holes on
        its own (tested: it fills them with either orientation), so the interiors
        are subtracted by hand."""
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
        """Saves the polygon and returns its exact pixel count."""
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

    # ── saving ─────────────────────────────────────────────────────────────────
    def _lonlat(self, cols, rows):
        return geometry.lonlat(self.transform, self.crs, cols, rows)

    def _px_from_lonlat(self, lons, lats):
        return geometry.px_from_lonlat(self.transform, self.crs, lons, lats)

    def _props(self):
        return {'scene_id': self.scene_id, 'image_type': self.kind,
                'pixel_size_m': self.pixel_size_m, 'date': self.date,
                'time_utc': self.time_utc, 'event_id': self.event_id,
                'image_path': os.path.relpath(self.path, ROOT)}

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
                coords = rings_to_coords(self.transform, self.crs,
                                         [poly['verts']] + poly.get('holes', []))
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
            # a CSV that exists but is empty (0 bytes) still needs a header
            has_header = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0
            pd.DataFrame(rows_out).to_csv(
                self.csv_path, mode='a', index=False, header=not has_header)
            n_pts = len(rows_out)

        ha = sum(p['n_px'] for p in self.polygons) * self.pixel_size_m ** 2 / 1e4
        print(f'  ✓ guardado: {len(self.polygons)} polígonos ({ha:.1f} ha)'
              + (f' + {n_pts} puntos' if n_pts else ''))
        # they move to "already saved": still visible (dashed) but pressing Save
        # again does not duplicate them
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
        description='Manual flood labelling over SAR or optical imagery')
    p.add_argument('--dir', default='data/SAR', help='Folder with the .tif files (default: data/SAR)')
    p.add_argument('--event-id', default='', help='Name of the event, saved on every label')
    p.add_argument('--speckle', type=int, default=DEFAULT_SPECKLE,
                   help=f'Window of the visual speckle smoothing in SAR '
                        f'(default {DEFAULT_SPECKLE}, 0 = none). Only affects what you see.')
    p.add_argument('--rasterize', action='store_true',
                   help='On top of the geometry, writes every pixel of the polygon to the CSV')
    p.add_argument('--skip-labeled', action='store_true',
                   help='Skips the scenes that already have saved labels')
    p.add_argument('--out', default=os.path.join(ROOT, 'labels'),
                   help='Output folder (default: labels/)')
    args = p.parse_args()

    d = args.dir if os.path.isabs(args.dir) else os.path.join(ROOT, args.dir)
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
    print('\nClases:      ' + '   '.join(f'{i + 1}={c}' for i, c in enumerate(CLASSES)))
    print('Herramientas: ' + '   '.join(f'{k}={v}' for k, v in TOOL_KEYS.items()))
    print(f'Salidas:      {geojson_path}\n              {csv_path}\n')

    FloodLabelApp(scenes, csv_path, geojson_path, event_id=args.event_id,
                  speckle=args.speckle, rasterize=args.rasterize)


if __name__ == '__main__':
    main()
