# Consultas Athena de exemplo

Use o workgroup `chuva-bh`, que limita cada consulta a 1 GB lido, e o banco `chuva_bh`. Filtre
por `year` e `month` sempre que possível: são as partições.

## Cobertura das observações por ano e ponto

```sql
SELECT year, point_id, count(*) AS horas, count(precipitation_mm) AS horas_com_chuva_medida
FROM observations
GROUP BY year, point_id
ORDER BY year, point_id;
```

## Horas com chuva acima do limiar

```sql
SELECT point_id, count_if(precipitation_mm >= 0.5) AS horas_chuvosas, count(*) AS horas
FROM observations
WHERE year = 2025
GROUP BY point_id;
```

## Lacunas na ingestão horária

```sql
SELECT issued_at, count(DISTINCT point_id) AS pontos
FROM forecasts
WHERE source = 'open_meteo_forecast' AND year = 2026 AND month = 11
GROUP BY issued_at
HAVING count(DISTINCT point_id) < 4
ORDER BY issued_at;
```

## Previsão histórica contra observação

```sql
SELECT
    o.point_id,
    avg(CASE WHEN o.precipitation_mm >= 0.5 THEN 1.0 ELSE 0.0 END) AS frequencia_observada,
    avg(CASE WHEN f.precipitation_mm >= 0.5 THEN 1.0 ELSE 0.0 END) AS frequencia_prevista
FROM observations o
JOIN forecasts f
    ON o.point_id = f.point_id
    AND o.year = f.year
    AND o.month = f.month
    AND o.observed_at = f.valid_at
WHERE o.year = 2025
    AND f.source = 'open_meteo_historical_forecast'
    AND o.precipitation_mm IS NOT NULL
    AND f.precipitation_mm IS NOT NULL
GROUP BY o.point_id;
```
