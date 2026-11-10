"""Pontos públicos monitorados, todos em estações automáticas do INMET."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Point:
    point_id: str
    name: str
    latitude: float
    longitude: float


POINTS: Final[tuple[Point, ...]] = (
    Point("A521", "Belo Horizonte - Pampulha", -19.88388888, -43.96944443),
    Point("A572", "Belo Horizonte - Santo Agostinho", -19.93388888, -43.95222222),
    Point("A535", "Florestal", -19.88527777, -44.41694443),
    Point("A569", "Sete Lagoas", -19.45527777, -44.17333332),
)

POINT_IDS: Final[tuple[str, ...]] = tuple(point.point_id for point in POINTS)
