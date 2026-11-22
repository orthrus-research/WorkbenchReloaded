"""world.preview's painting: a sampler's raster (sampler.py) as a picture, its colours from a palette. A palette maps
values to colours (#rrggbb): a text value takes the colour of its own key, and number keys are the stops of a
gradient that numbers are painted along, held at the end stops beyond them. A value the palette does not cover gets
a colour of its own: a number a grey by its place between the raster's lowest and highest, anything else a hue
drawn from its text, the same on every run. The raster's first row, the lowest z, is the top of the picture and x
runs to the right: north is up, as on a map of the world.

Needs NumPy and Pillow; commands.py imports this module for world.preview alone.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import math
import re

import numpy as np
from PIL import Image

COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
RGB = tuple[int, int, int]


def colours(palette: dict) -> tuple[list[tuple[float, RGB]], dict[str, RGB]]:
    """A palette's stops for numbers, lowest first, and its colours of text values. Raises ValueError."""
    stops, named = [], {}
    for key, colour in palette.items():
        if not isinstance(colour, str) or not COLOUR.match(colour):
            raise ValueError(f"palette: {key} is {json.dumps(colour)}, not a colour like #1f4e9c")
        rgb = (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
        number = _number(str(key))
        if number is None:
            named[str(key)] = rgb
        else:
            stops.append((number, rgb))
    return sorted(stops), named


def paint(raster: dict, stops: list[tuple[float, RGB]], named: dict[str, RGB], scale: int) -> tuple[Image.Image, dict]:
    """The raster as an RGB picture, ``scale`` pixels a side for each value; with how many distinct values it holds
    and those the palette does not cover."""
    keys = [_key(value) for value in raster["values"]]
    distinct = list(dict.fromkeys(keys))
    numbers = [value for value in distinct if not isinstance(value, str)]
    low, high = (min(numbers), max(numbers)) if numbers else (0, 0)
    table, unlisted = [], []
    for value in distinct:
        if not isinstance(value, str) and stops:
            table.append(_along(stops, value))
        elif isinstance(value, str) and value in named:
            table.append(named[value])
        else:
            unlisted.append(value)
            table.append(_grey(value, low, high) if not isinstance(value, str) else _hue(value))
    index = {value: number for number, value in enumerate(distinct)}
    cells = np.fromiter((index[key] for key in keys), dtype=np.int64, count=len(keys))
    pixels = np.asarray(table, dtype=np.uint8)[cells.reshape(raster["height"], raster["width"])]
    if scale > 1:
        pixels = pixels.repeat(scale, axis=0).repeat(scale, axis=1)
    return Image.fromarray(pixels), {"values": len(distinct), "unlisted": unlisted}


def _key(value):
    """A raster value as the table knows it: a finite number as itself, anything else as text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _number(text: str) -> float | None:
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _along(stops: list[tuple[float, RGB]], value: float) -> RGB:
    if value <= stops[0][0]:
        return stops[0][1]
    for (low, below), (high, above) in zip(stops, stops[1:]):
        if value <= high:
            share = (value - low) / (high - low) if high > low else 1.0
            return tuple(round(a + (b - a) * share) for a, b in zip(below, above))
    return stops[-1][1]


def _grey(value: float, low: float, high: float) -> RGB:
    level = round(255 * (value - low) / (high - low)) if high > low else 128
    return level, level, level


def _hue(text: str) -> RGB:
    hue = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:2], "big") / 65536
    return tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 0.55, 0.85))
