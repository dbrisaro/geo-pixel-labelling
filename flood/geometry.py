"""
flood/geometry.py — conversión entre píxeles y lon/lat, compartida.

Vive acá y no en label.py porque import_labels.py necesita exactamente la misma
matemática y no puede importar la GUI (arrastraría matplotlib y Tk). Sólo pide
numpy y rasterio.

Convención de coordenadas, que es donde se esconden los errores de medio píxel:
todo lo que sale y entra de acá está en coords de CENTRO de píxel, o sea el
entero N es el centro del píxel N, igual que imshow. rasterio trabaja en coords
de ESQUINA, de ahí los ±0.5 en los dos sentidos.
"""

import numpy as np

import rasterio
from rasterio.features import shapes as rio_shapes
from rasterio.warp import transform as warp_transform
from rasterio.crs import CRS


def ring_signed_area(ring):
    """Área con signo (fórmula del cordón). El signo da la orientación."""
    a = np.asarray(ring, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def shoelace_px(verts):
    """Área de un polígono en px², por la fórmula del cordón. O(n) en vértices,
    así que se puede recalcular en cada movimiento del mouse sin que lagee."""
    if len(verts) < 3:
        return 0.0
    a = np.asarray(verts, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def rings_area_px(rings):
    """Área en píxeles de un polígono con agujeros: exterior menos interiores.
    Sobre los anillos que devuelve mask_to_polygons da el conteo exacto de
    píxeles, porque son contornos pegados al borde de los píxeles."""
    if not rings:
        return 0
    return int(round(shoelace_px(rings[0]) - sum(shoelace_px(r) for r in rings[1:])))


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


def lonlat(transform, crs, cols, rows):
    """(col, row) en convención de CENTRO de píxel → lon/lat EPSG:4326.
    Una sola convención para clicks (enteros) y vértices (floats):
    transform.xy hace transform * (col + 0.5, row + 0.5) y acepta floats,
    verificado bit a bit. Antes los polígonos usaban transform * (col, row),
    que apunta a la esquina y los corría medio píxel."""
    xs, ys = rasterio.transform.xy(transform, list(rows), list(cols))
    if crs and not crs.is_geographic:
        return warp_transform(crs, CRS.from_epsg(4326), xs, ys)
    return xs, ys


def px_from_lonlat(transform, crs, lons, lats):
    """Inversa de lonlat: lon/lat → (col, row) en coords de centro de píxel.
    Se usa para redibujar las etiquetas ya guardadas de esta escena."""
    if crs and not crs.is_geographic:
        xs, ys = warp_transform(CRS.from_epsg(4326), crs, list(lons), list(lats))
    else:
        xs, ys = list(lons), list(lats)
    inv = ~transform
    out = []
    for x, y in zip(xs, ys):
        c, r = inv * (x, y)
        out.append((c - 0.5, r - 0.5))
    return out


def rings_to_coords(transform, crs, rings):
    """Anillos en píxeles → anillos lon/lat cerrados, listos para un GeoJSON."""
    coords = []
    for r in rings:
        lons, lats = lonlat(transform, crs, [v[0] for v in r], [v[1] for v in r])
        ring = [[round(float(x), 7), round(float(y), 7)] for x, y in zip(lons, lats)]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        coords.append(ring)
    return coords
