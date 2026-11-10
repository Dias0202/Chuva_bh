"""Leitura dos arquivos históricos anuais das estações automáticas do INMET."""

from __future__ import annotations

import csv
import io
import unicodedata
import zipfile
from collections.abc import Collection, Iterator
from datetime import datetime
from typing import Final

from ingestion.columns import Row

BULK_URL_TEMPLATE: Final = "https://portal.inmet.gov.br/uploads/dadoshistoricos/{year}.zip"
SOURCE_INMET_BULK: Final = "inmet_bulk"

_ENCODING: Final = "latin-1"
_MISSING: Final = frozenset({"", "-9999", "-9999,0", "-9999.0"})
_MAX_MEMBER_BYTES: Final = 50 * 1024 * 1024
_STATION_CODE_FIELD: Final = 3

_HEADER_PREFIXES: Final[dict[str, str]] = {
    "precipitation_mm": "PRECIPITACAO TOTAL",
    "pressure_hpa": "PRESSAO ATMOSFERICA AO NIVEL DA ESTACAO",
    "radiation_kj_m2": "RADIACAO GLOBAL",
    "temperature_c": "TEMPERATURA DO AR - BULBO SECO",
    "dew_point_c": "TEMPERATURA DO PONTO DE ORVALHO",
    "relative_humidity_pct": "UMIDADE RELATIVA DO AR",
    "wind_direction_deg": "VENTO, DIRECAO",
    "wind_gust_ms": "VENTO, RAJADA MAXIMA",
    "wind_speed_ms": "VENTO, VELOCIDADE",
}


class InmetFormatError(ValueError):
    """O arquivo do INMET não tem o formato esperado."""


def _normalize(header: str) -> str:
    decomposed = unicodedata.normalize("NFKD", header)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).upper().strip()


def _number(raw: str) -> float | None:
    value = raw.strip()
    if value in _MISSING:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError as error:
        raise InmetFormatError(f"valor não numérico: {value!r}") from error


def _column_positions(header: list[str]) -> dict[str, int]:
    normalized = [_normalize(name) for name in header]
    positions: dict[str, int] = {}
    for column, prefix in _HEADER_PREFIXES.items():
        matches = [i for i, name in enumerate(normalized) if name.startswith(prefix)]
        if len(matches) != 1:
            raise InmetFormatError(f"coluna não identificada: {prefix}")
        positions[column] = matches[0]
    return positions


def parse_station_csv(text: str, point_id: str) -> list[Row]:
    """Converte o CSV de uma estação em linhas com horário em UTC."""
    lines = text.splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if line.lower().startswith("data;")), None
    )
    if header_index is None:
        raise InmetFormatError("cabeçalho não encontrado")

    reader = csv.reader(lines[header_index:], delimiter=";")
    positions = _column_positions(next(reader))

    rows: list[Row] = []
    for fields in reader:
        if len(fields) < 2 or not fields[0].strip():
            continue
        try:
            observed_at = datetime.strptime(f"{fields[0]} {fields[1]}", "%Y/%m/%d %H%M UTC")
        except ValueError as error:
            raise InmetFormatError(f"data inválida: {fields[:2]}") from error
        row: Row = {
            "point_id": point_id,
            "observed_at": observed_at,
            "source": SOURCE_INMET_BULK,
        }
        for column, position in positions.items():
            row[column] = _number(fields[position]) if position < len(fields) else None
        rows.append(row)
    return rows


def iter_stations(archive: bytes, point_ids: Collection[str]) -> Iterator[tuple[str, list[Row]]]:
    """Percorre as estações pedidas em um ZIP anual, sem extrair para o disco."""
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for info in bundle.infolist():
            fields = info.filename.rsplit("/", 1)[-1].split("_")
            if len(fields) <= _STATION_CODE_FIELD or fields[_STATION_CODE_FIELD] not in point_ids:
                continue
            if info.file_size > _MAX_MEMBER_BYTES:
                raise InmetFormatError(f"arquivo acima do limite: {info.filename}")
            point_id = fields[_STATION_CODE_FIELD]
            yield point_id, parse_station_csv(bundle.read(info).decode(_ENCODING), point_id)
