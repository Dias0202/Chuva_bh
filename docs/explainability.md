# Explicabilidade do modelo

Gerado por `scripts/explain.py` em 2026-11-10, para a versão 1 do grupo `chuva-bh-rain`, com 20000 linhas do teste de 2025.

Valores SHAP exatos (TreeSHAP do XGBoost), na escala de log-odds. Quanto maior a média do
valor absoluto, mais a feature move a probabilidade prevista.

| Feature | Média de \|SHAP\| (todas as horas) | Média de \|SHAP\| (horas com chuva) |
|---|---:|---:|
| `fc_cloud_cover_pct` | 0.7914 | 0.2476 |
| `fc_precipitation_sum_mm` | 0.6232 | 0.7703 |
| `fc_relative_humidity_pct` | 0.5350 | 0.2992 |
| `fc_cape_j_kg` | 0.3280 | 0.1454 |
| `doy_cos` | 0.2175 | 0.0955 |
| `temperature_c` | 0.2114 | 0.0645 |
| `fc_precipitation_mm` | 0.1986 | 0.4346 |
| `doy_sin` | 0.1825 | 0.0793 |
| `precip_mm` | 0.1018 | 0.4027 |
| `pressure_hpa` | 0.0985 | 0.0974 |
| `precip_sum_6h_mm` | 0.0903 | 0.1378 |
| `pressure_tendency_3h_hpa` | 0.0733 | 0.0938 |
| `precip_sum_24h_mm` | 0.0689 | 0.0864 |
| `wind_gust_ms` | 0.0665 | 0.0632 |
| `wind_dir_sin` | 0.0566 | 0.0398 |

O TreeSHAP usa a estrutura das árvores e calcula os valores de Shapley exatos, sem
amostragem e sem precisar de um endpoint.
