"""
flood/geometry.py — conversion between pixels and lon/lat, shared.

It lives here and not in label.py because import_labels.py needs exactly the
same maths and cannot import the GUI (that would drag in matplotlib and Tk). It
only requires numpy and rasterio.

Coordinate convention, which is where half-pixel errors hide: everything that
goes in and out of here is in pixel CENTRE coords, that is, the integer N is the
centre of pixel N, just like imshow. rasterio works in CORNER coords, hence the
±0.5 in both directions.
"""

import numpy as np

import rasterio
from rasterio.features import shapes as rio_shapes
from rasterio.warp import transform as warp_transform
from rasterio.crs import CRS


def ring_signed_area(ring):
    """Signed area (shoelace formula). The sign gives the orientation."""
    a = np.asarray(ring, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def shoelace_px(verts):
    """Area of a polygon in px², by the shoelace formula. O(n) in vertices, so it
    can be recomputed on every mouse move without lagging."""
    if len(verts) < 3:
        return 0.0
    a = np.asarray(verts, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def rings_area_px(rings):
    """Pixel area of a polygon with holes: exterior minus interiors. Over the
    rings returned by mask_to_polygons it gives the exact pixel count, because
    those are outlines flush with the pixel edges."""
    if not rings:
        return 0
    return int(round(shoelace_px(rings[0]) - sum(shoelace_px(r) for r in rings[1:])))


def mask_to_polygons(mask):
    """Boolean mask → list of polygons in pixel coords; each polygon is
    [exterior_ring, *interior_rings]. Used so that the brush ends up saving
    geometry just like the other tools, rather than a separate output format.

    rio_shapes works in pixel CORNER coords, while the rest of the app uses
    CENTRE coords (like imshow: the integer N is the centre of pixel N). Hence
    the −0.5: without that adjustment the rings fall between the centres, a
    stroke 1 px wide counts 0 pixels and is discarded without warning, and
    everything else ends up shifted by half a pixel.
    """
    out = []
    for geom, _ in rio_shapes(mask.astype(np.uint8), mask=mask,
                              transform=rasterio.Affine.identity()):
        rings = [[(float(x) - 0.5, float(y) - 0.5) for x, y in r[:-1]]
                 for r in geom['coordinates'] if len(r) >= 4]
        if not rings:
            continue
        # rio_shapes returns exterior and interiors with the same orientation
        sign_ext = ring_signed_area(rings[0])
        rings = [rings[0]] + [r if ring_signed_area(r) * sign_ext < 0 else r[::-1]
                              for r in rings[1:]]
        out.append(rings)
    return out


def lonlat(transform, crs, cols, rows):
    """(col, row) in pixel CENTRE convention → lon/lat EPSG:4326.
    One single convention for clicks (integers) and vertices (floats):
    transform.xy does transform * (col + 0.5, row + 0.5) and accepts floats,
    verified bit by bit. Polygons used to use transform * (col, row), which
    points at the corner and shifted them by half a pixel."""
    xs, ys = rasterio.transform.xy(transform, list(rows), list(cols))
    if crs and not crs.is_geographic:
        return warp_transform(crs, CRS.from_epsg(4326), xs, ys)
    return xs, ys


def px_from_lonlat(transform, crs, lons, lats):
    """Inverse of lonlat: lon/lat → (col, row) in pixel centre coords.
    Used to redraw the labels already saved for this scene."""
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
    """Rings in pixels → closed lon/lat rings, ready for a GeoJSON."""
    coords = []
    for r in rings:
        lons, lats = lonlat(transform, crs, [v[0] for v in r], [v[1] for v in r])
        ring = [[round(float(x), 7), round(float(y), 7)] for x, y in zip(lons, lats)]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        coords.append(ring)
    return coords
