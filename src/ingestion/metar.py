"""METAR do aeroporto da Pampulha (SBBH): fonte sem credencial para o modo METAR.

O METAR não informa a chuva em mm, só se está chovendo ou trovejando. Ele substitui a
observação do INMET nas features de tempo real; o rótulo continua vindo do INMET.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlencode

from ingestion.columns import Row
from ingestion.http import Fetch

STATION: Final = "SBBH"
# A estação A521 do INMET fica a cerca de 3 km da pista da Pampulha.
POINT_ID: Final = "A521"
SOURCE_ARCHIVE: Final = "metar_iem"
SOURCE_LIVE: Final = "metar_awc"
LIVE_URL: Final = "https://aviationweather.gov/api/data/metar"
ARCHIVE_URL: Final = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

_KNOT_MS: Final = 0.514444
_INHG_HPA: Final = 33.8639
_CEILING_COVERS: Final = frozenset({"BKN", "OVC", "VV"})
# Chuva, garoa, pancada e trovoada com chuva, inclusive a recente (RE), que cobre a hora anterior.
_RAIN: Final = re.compile(r"(RA|DZ)")
_THUNDER: Final = re.compile(r"TS")


class MetarFormatError(ValueError):
    """A resposta da fonte de METAR não tem o formato esperado."""


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "M", "VRB", "null"):
        return None
    try:
        return float(text)
    except ValueError as error:
        raise MetarFormatError(f"valor não numérico: {text!r}") from error


def weather_flags(codes: str | None) -> tuple[float, float]:
    """Chuva e trovoada (0 ou 1) a partir dos códigos de tempo presente, como '-TSRA'."""
    tokens = [] if not codes or codes.strip() in ("M", "") else codes.split()
    rain = any(_RAIN.search(token) for token in tokens)
    thunder = any(_THUNDER.search(token) for token in tokens)
    return float(rain), float(thunder)


def ceiling(layers: Iterable[tuple[str | None, float | None]]) -> float | None:
    """Base da camada mais baixa com céu nublado ou encoberto, em pés."""
    bases = [base for cover, base in layers if cover in _CEILING_COVERS and base is not None]
    return min(bases) if bases else None


def _row(
    observed_at: datetime,
    source: str,
    temperature_c: float | None,
    dew_point_c: float | None,
    pressure_hpa: float | None,
    direction: float | None,
    speed_kt: float | None,
    gust_kt: float | None,
    codes: str | None,
    ceiling_ft: float | None,
) -> Row:
    rain, thunder = weather_flags(codes)
    return {
        "point_id": POINT_ID,
        # Relatórios especiais no meio da hora contam para a hora cheia anterior.
        "observed_at": observed_at.replace(minute=0, second=0, microsecond=0),
        "source": source,
        "temperature_c": temperature_c,
        "dew_point_c": dew_point_c,
        "pressure_hpa": pressure_hpa,
        "wind_direction_deg": direction,
        "wind_speed_ms": speed_kt * _KNOT_MS if speed_kt is not None else None,
        "wind_gust_ms": gust_kt * _KNOT_MS if gust_kt is not None else None,
        "ceiling_ft": ceiling_ft,
        "rain": rain,
        "thunder": thunder,
        "weather": (codes or "").strip() or None,
    }


def _fahrenheit(value: Any) -> float | None:
    number = _number(value)
    return (number - 32) * 5 / 9 if number is not None else None


def parse_archive(text: str) -> list[Row]:
    """CSV do Iowa Environmental Mesonet (temperatura em °F, pressão em inHg)."""
    if text.lstrip().lower().startswith("too many requests"):
        raise MetarFormatError("limite de requisições do IEM atingido")
    reader = csv.DictReader(io.StringIO(text))
    required = {"valid", "tmpf", "dwpf", "alti", "drct", "sknt", "gust", "wxcodes"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise MetarFormatError("colunas ausentes no CSV do IEM")
    rows = []
    for record in reader:
        try:
            observed_at = datetime.strptime(record["valid"], "%Y-%m-%d %H:%M")
        except ValueError as error:
            raise MetarFormatError(f"horário inválido: {record['valid']!r}") from error
        pressure = _number(record["alti"])
        layers = [(record.get(f"skyc{i}"), _number(record.get(f"skyl{i}"))) for i in (1, 2, 3)]
        rows.append(
            _row(
                observed_at,
                SOURCE_ARCHIVE,
                _fahrenheit(record["tmpf"]),
                _fahrenheit(record["dwpf"]),
                pressure * _INHG_HPA if pressure is not None else None,
                _number(record["drct"]),
                _number(record["sknt"]),
                _number(record["gust"]),
                record["wxcodes"],
                ceiling(layers),
            )
        )
    return rows


def parse_live(body: bytes) -> list[Row]:
    """JSON da API do aviationweather.gov (temperatura em °C, pressão em hPa)."""
    try:
        records = json.loads(body)
    except json.JSONDecodeError as error:
        raise MetarFormatError("JSON inválido") from error
    if not isinstance(records, list):
        raise MetarFormatError("resposta não é uma lista")
    rows = []
    for record in records:
        if not isinstance(record, Mapping) or "obsTime" not in record:
            raise MetarFormatError("registro sem obsTime")
        observed_at = datetime.fromtimestamp(int(record["obsTime"]), UTC).replace(tzinfo=None)
        clouds = record.get("clouds") or []
        layers = [
            (c.get("cover"), _number(c.get("base"))) for c in clouds if isinstance(c, Mapping)
        ]
        rows.append(
            _row(
                observed_at,
                SOURCE_LIVE,
                _number(record.get("temp")),
                _number(record.get("dewp")),
                _number(record.get("altim")),
                _number(record.get("wdir")),
                _number(record.get("wspd")),
                _number(record.get("wgst")),
                record.get("wxString"),
                ceiling(layers),
            )
        )
    return rows


def live_url(hours: int = 3) -> str:
    return f"{LIVE_URL}?{urlencode({'ids': STATION, 'format': 'json', 'hours': hours})}"


def archive_url(year: int) -> str:
    params: list[tuple[str, str | int]] = [("station", STATION)]
    params += [
        ("data", name) for name in ("tmpf", "dwpf", "alti", "drct", "sknt", "gust", "wxcodes")
    ]
    params += [("data", f"{kind}{i}") for i in (1, 2, 3) for kind in ("skyc", "skyl")]
    params += [
        ("year1", year),
        ("month1", 1),
        ("day1", 1),
        ("year2", year + 1),
        ("month2", 1),
        ("day2", 1),
        ("tz", "Etc/UTC"),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("missing", "M"),
        ("report_type", 3),
        ("report_type", 4),
    ]
    return f"{ARCHIVE_URL}?{urlencode(params)}"


def fetch_live(fetch: Fetch, hours: int = 3) -> list[Row]:
    return parse_live(fetch(live_url(hours)))
