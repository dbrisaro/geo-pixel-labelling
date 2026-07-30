"""
make_demo_data.py — Genera una escena S2 sintética en archive/sargassum/data/DemoMX/S2/ para probar
la GUI sin necesidad de descargar imágenes reales.

Escribe:
    data/DemoMX/S2/2024-07-15T155819_demo_ms.tif     (9 bandas, float32, reflectancia)
    data/DemoMX/S2/2024-07-15T155819_demo_cloud.tif  (prob. s2cloudless 0-100, uint8)

La escena es UTM 16N (10 m/px) frente a Cancún, así que el overlay de tierra
(ESA WorldCover) también se puede probar si hay internet.

    python archive/sargassum/make_demo_data.py
"""
import os
import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform
from rasterio.crs import CRS

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'data', 'DemoMX', 'S2')
STEM = '2024-07-15T155819_demo'
H = W = 400
RES = 10.0
CRS_UTM = CRS.from_epsg(32616)          # UTM 16N (Península de Yucatán)
LON0, LAT0 = -86.83, 21.20              # esquina NO, costa de Cancún

# reflectancias típicas por clase, en el orden de bandas de S2 que espera label.py:
# Blue, Green, Red, RE1, RE2, RE3, NIR, NIR_narrow, SWIR1
SPECTRA = {
    'water':     [0.035, 0.045, 0.022, 0.015, 0.010, 0.009, 0.008, 0.008, 0.004],
    'land':      [0.060, 0.085, 0.075, 0.140, 0.280, 0.320, 0.350, 0.345, 0.250],
    'sargassum': [0.030, 0.055, 0.032, 0.060, 0.090, 0.100, 0.105, 0.100, 0.020],
    'cloud':     [0.720, 0.730, 0.730, 0.720, 0.700, 0.690, 0.680, 0.675, 0.500],
}


def _disk(rr, cc, r0, c0, radius):
    return (rr - r0) ** 2 + (cc - c0) ** 2 <= radius ** 2


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = np.random.default_rng(42)

    x0, y0 = warp_transform(CRS.from_epsg(4326), CRS_UTM, [LON0], [LAT0])
    transform = from_origin(x0[0], y0[0], RES, RES)

    rr, cc = np.mgrid[0:H, 0:W]

    # tierra: franja costera al oeste con borde ondulado
    coast = 90 + 25 * np.sin(rr / 45.0) + 10 * np.sin(rr / 13.0)
    land = cc < coast

    # sargazo: bandas alargadas paralelas a la costa + parches sueltos mar adentro
    sarg = np.zeros((H, W), bool)
    for off, amp, thick in ((35, 8, 4), (58, 12, 3), (95, 6, 5)):
        band = coast + off + amp * np.sin(rr / 22.0)
        sarg |= (cc > band) & (cc < band + thick)
    for r0, c0, rad in ((70, 250, 9), (180, 300, 12), (300, 210, 7), (330, 340, 10)):
        sarg |= _disk(rr, cc, r0, c0, rad)
    sarg &= ~land

    # nubes: dos cúmulos con borde difuso
    cloud_soft = np.zeros((H, W), np.float32)
    for r0, c0, rad in ((120, 120, 45), (270, 60, 30)):
        d = np.sqrt((rr - r0) ** 2 + (cc - c0) ** 2)
        cloud_soft = np.maximum(cloud_soft, np.clip(1.0 - d / rad, 0, 1))
    cloud_soft = np.clip(cloud_soft * 1.4, 0, 1)

    n_bands = len(SPECTRA['water'])
    stack = np.empty((n_bands, H, W), np.float32)
    for b in range(n_bands):
        arr = np.full((H, W), SPECTRA['water'][b], np.float32)
        arr[land] = SPECTRA['land'][b]
        arr[sarg] = SPECTRA['sargassum'][b]
        # nube se mezcla encima según su opacidad (bordes semitransparentes)
        arr = arr * (1 - cloud_soft) + SPECTRA['cloud'][b] * cloud_soft
        arr += rng.normal(0, 0.004, (H, W)).astype(np.float32)
        stack[b] = np.clip(arr, 0, 1.2)

    ms_path = os.path.join(OUT_DIR, f'{STEM}_ms.tif')
    with rasterio.open(ms_path, 'w', driver='GTiff', height=H, width=W,
                       count=n_bands, dtype='float32', crs=CRS_UTM,
                       transform=transform, nodata=np.nan, compress='deflate') as dst:
        dst.write(stack)

    prob = np.clip(cloud_soft * 130, 0, 100).astype(np.uint8)
    cloud_path = os.path.join(OUT_DIR, f'{STEM}_cloud.tif')
    with rasterio.open(cloud_path, 'w', driver='GTiff', height=H, width=W,
                       count=1, dtype='uint8', crs=CRS_UTM,
                       transform=transform, compress='deflate') as dst:
        dst.write(prob, 1)

    pct = 100.0 * np.count_nonzero(prob > 40) / prob.size
    print(f'✓ {ms_path}')
    print(f'✓ {cloud_path}   (nube {pct:.1f}% de la escena)')
    print(f'  {np.count_nonzero(sarg)} px de sargazo sintético, '
          f'{np.count_nonzero(land)} px de tierra')


if __name__ == '__main__':
    main()
