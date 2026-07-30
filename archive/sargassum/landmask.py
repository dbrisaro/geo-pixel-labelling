"""
landmask.py — Máscara de tierra a 10 m desde ESA WorldCover 2021 (v200),
alineada exactamente a la grilla de cada escena S2.

WorldCover es un raster global de cobertura a 10 m (mismo píxel que S2). La clase
80 = cuerpos de agua permanentes. Todo lo demás (incl. manglar=95, humedal=90,
construido, vegetación) se considera tierra/no-clasificable para sargazo.

Se lee por ventana con /vsicurl desde el bucket público de AWS (sin bajar el tile
entero). Cachea la máscara por grilla de escena (mismas escenas de un tile S2
comparten grilla → una sola lectura remota).
"""
import os
import math
import numpy as np
import rasterio
from rasterio.vrt import WarpedVRT
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import transform_bounds

os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif')
os.environ.setdefault('GDAL_HTTP_MAX_RETRY', '5')
os.environ.setdefault('GDAL_HTTP_RETRY_DELAY', '2')

WC_BASE = ('/vsicurl/https://esa-worldcover.s3.eu-central-1.amazonaws.com/'
           'v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif')
WATER_CLASS = 80
_TILE_CACHE = {}    # (crs_wkt, H, W, transform6) -> land mask bool


def _tile_name(lat, lon):
    la = int(math.floor(lat / 3) * 3); lo = int(math.floor(lon / 3) * 3)
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def _tiles_for_bbox(w, s, e, n):
    tiles = set()
    la = math.floor(s / 3) * 3
    while la <= n:
        lo = math.floor(w / 3) * 3
        while lo <= e:
            tiles.add(_tile_name(la, lo))
            lo += 3
        la += 3
    return sorted(tiles)


def land_mask(crs, transform, H, W):
    """(H,W) bool, True = tierra (NO agua/clase80 de WorldCover)."""
    key = (crs.to_wkt(), H, W, tuple(transform)[:6])
    if key in _TILE_CACHE:
        return _TILE_CACHE[key]

    l, b, r, t = array_bounds(H, W, transform)
    w, s, e, n = transform_bounds(crs, 'EPSG:4326', l, b, r, t)
    wc = np.zeros((H, W), np.uint8)
    covered = np.zeros((H, W), bool)
    for tname in _tiles_for_bbox(w, s, e, n):
        url = WC_BASE.format(tile=tname)
        try:
            with rasterio.open(url) as src:
                with WarpedVRT(src, crs=crs, transform=transform, width=W, height=H,
                               resampling=Resampling.nearest) as vrt:
                    arr = vrt.read(1)
        except Exception as ex:
            print(f'  [landmask] aviso: no se pudo leer tile {tname}: {ex}')
            continue
        m = arr > 0
        wc[m] = arr[m]; covered |= m

    # tierra = cubierto por WC y NO agua; lo no cubierto se deja como NO-tierra
    land = covered & (wc != WATER_CLASS)
    _TILE_CACHE[key] = land
    return land
