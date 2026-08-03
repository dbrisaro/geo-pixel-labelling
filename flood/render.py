"""
render.py — reading and visual rendering of a chip, shared by the labelling GUI
(flood/label.py) and the PNG renderer for Label Studio (render_chip.py).

Everything in here is only for SEEING the image: SAR vs optical detection by band
names, dB conversion, percentile stretching, speckle smoothing and false colour
composition. It does not alter what gets saved as a label.
"""

import os
import math

import numpy as np
from PIL import Image

import rasterio

try:
    from scipy.ndimage import median_filter
except Exception:                                    # pragma: no cover
    median_filter = None

# Band name aliases → resolved from the .tif descriptions.
# Sen1Floods11 ships them ('VV','VH','B2'...), which is the only robust thing
# across providers.
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
    13: {'Blue': 1, 'Green': 2, 'Red': 3, 'NIR': 7, 'SWIR1': 11},   # full S2
    9:  {'Blue': 0, 'Green': 1, 'Red': 2, 'NIR': 6, 'SWIR1': 8},    # trimmed S2
    5:  {'Blue': 0, 'Green': 1, 'Red': 2, 'NIR': 3, 'SWIR1': 4},    # Landsat 8/9
}
DEFAULT_SPECKLE = 3


def band_map(descriptions, count):
    """Returns (kind, band_map, origin). kind = 'SAR' | 'OPTICAL'."""
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
    """Robust percentile → [0,1]. Works with dB (negatives) and with reflectance."""
    v = arr[np.isfinite(arr)]
    if v.size < 10:
        return np.zeros_like(arr)
    p_lo, p_hi = np.percentile(v, [lo, hi])
    if p_hi - p_lo < 1e-9:
        return np.zeros_like(arr)
    return np.clip((arr - p_lo) / (p_hi - p_lo), 0, 1)


def to_db(arr):
    """SAR to dB. If it already comes in dB (negative median) it touches nothing."""
    v = arr[np.isfinite(arr)]
    if v.size == 0 or np.percentile(v, 50) < 0:
        return arr, 'ya en dB'
    factor = 10.0 if np.percentile(v, 99.9) <= 5 else 20.0
    with np.errstate(divide='ignore', invalid='ignore'):
        return factor * np.log10(np.where(arr > 0, arr, np.nan)), \
            ('lineal → dB' if factor == 10.0 else 'DN → dB relativo')


def despeckle(arr, size):
    """NxN median preserving NaN. Only to see better: it does not alter what you save."""
    if size <= 1 or median_filter is None:
        return arr
    bad = ~np.isfinite(arr)
    if bad.all():
        return arr
    out = median_filter(np.where(bad, np.nanmedian(arr), arr), size=size)
    out[bad] = np.nan
    return out


def pixel_size_m(src):
    res = abs(src.transform.a)
    if src.crs and src.crs.is_projected:
        return round(res, 2)
    lat = src.transform.f + src.transform.e * src.height / 2.0
    return round(res * 111320.0 * math.cos(math.radians(abs(lat))), 2)


class Chip:

    def __init__(self, path):
        with rasterio.open(path) as src:
            raw = src.read().astype(np.float32)
            self.height, self.width = src.height, src.width
            self.crs, self.transform = src.crs, src.transform
            self.pixel_size_m = pixel_size_m(src)
            nd = src.nodata
            self.kind, self.band_index, self.band_origin = band_map(src.descriptions,
                                                                    src.count)
            self.n_bands = src.count

        if nd is not None and not np.isnan(nd):
            raw[raw == nd] = np.nan
        raw[~np.isfinite(raw)] = np.nan

        self.path = path
        self.scene_id = os.path.splitext(os.path.basename(path))[0]
        if self.kind == 'SAR':
            self._prep_sar(raw)
        else:
            self._prep_optical(raw)

    def _prep_sar(self, raw):
        bidx = self.band_index
        vv, note = to_db(raw[bidx['VV']])
        self.vv = vv
        self.vh = None
        if 'VH' in bidx and bidx['VH'] < raw.shape[0]:
            self.vh, _ = to_db(raw[bidx['VH']])
        self.bands_txt = 'VV+VH' if self.vh is not None else 'VV'
        self.prep_msg = (f'{self.bands_txt} · {note} · rango '
                         f'{np.nanpercentile(vv, 1):.1f}..{np.nanpercentile(vv, 99):.1f} dB')
        self.views = ['VV (dB)'] + (['VH (dB)', 'falso color R=VV G=VH B=VV−VH']
                                    if self.vh is not None else [])

    def _prep_optical(self, raw):
        bidx = self.band_index
        self.prep_msg = ''
        v = raw[np.isfinite(raw)]
        if v.size and np.percentile(v, 99) > 100:
            raw = raw / 10000.0
            self.prep_msg = 'reflectancia ×10000 → [0,1]'
        raw = np.where((raw < -0.5) | (raw > 2.0), np.nan, raw)
        self.R, self.G, self.B = raw[bidx['Red']], raw[bidx['Green']], raw[bidx['Blue']]
        self.N, self.S1 = raw[bidx['NIR']], raw[bidx['SWIR1']]
        self.bands_txt = 'RGB+NIR+SWIR1'
        self.vv = self.vh = None
        # SWIR/NIR/Green: the standard composite for flooding. Water absorbs
        # almost all of the SWIR, so it comes out black and contrasts with
        # everything else.
        self.views = ['falso color SWIR/NIR/Green (agua = negro)', 'RGB color natural']

    def image(self, view=0, speckle=DEFAULT_SPECKLE):
        s = speckle
        label = self.views[view % len(self.views)]
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

    def bands_used(self):
        if self.kind == 'SAR':
            names = ['VV'] + (['VH'] if self.vh is not None else [])
        else:
            names = ['Blue', 'Green', 'Red', 'NIR', 'SWIR1']
        return {n: self.band_index[n] for n in names}


def to_uint8(rgb):
    return (np.clip(rgb, 0, 1) * 255).round().astype(np.uint8)


def save_png(rgb, path):
    Image.fromarray(to_uint8(rgb)).save(path)
