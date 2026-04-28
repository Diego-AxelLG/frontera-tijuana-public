# ADR 003 — Códigos sintéticos `_99` para distritos XX residuales

**Fecha:** 2026-04-26 · **Estado:** decidido

## Contexto

El raw nacional TransBorder usa la columna `DEPE` (4 chars) para identificar el puerto:
- Códigos puros 4-dígitos (`2504` = San Ysidro, `2506` = Otay Mesa, etc.) — POEs físicos.
- Códigos `NN01-NN99` específicos de distrito (`2501` = San Diego District aggregate).
- Códigos `NNXX` residuales (`25XX`, `41XX`, `20XX`, ...) = "all other ports in district NN" para flujos no atribuidos a un POE específico.

En el dataset Mexico-filtered (COUNTRY=2010) hay **42 DEPEs `XX` distintos** representando **309,872 filas / ~$100B** (≈13% del total Mexico→US 2023).

Nuestro `dim_port.port_code` es `INTEGER UNIQUE`. Los XX no son enteros válidos.

Primera versión del ETL los descartaba silenciosamente → V5 (Total Mexico→US 2023) daba $697B vs cifra pública $799B (-13%).

## Decisión

**Generar códigos sintéticos `district * 100 + 99` para cada XX y agregarlos a `dim_port` con `is_aggregate=TRUE`**.

```
'25XX' → port_code 2599, name 'District 25XX residual', is_aggregate=TRUE
'41XX' → port_code 4199, name 'District 41XX residual', is_aggregate=TRUE
...
```

En el ETL (`etl/05_load_fact_transborder.py`), el helper `depe_to_port_code()` mapea:
- `'2504'` → `2504`
- `'25XX'` → `2599`
- NULL/inválido → `None` (descarte con log)

Convención: el sufijo `99` se eligió porque ningún distrito CBP existente usa el código `NN99` para puertos físicos. No-colisión verificada en `dim_port`.

## Alternativas consideradas

### Alt A — Cambiar `port_code` a TEXT para soportar XX

- ✓ Conserva la nomenclatura original BTS sin transformación.
- ✗ Requiere ALTER en producción (migración de tipo, rebuild de índices, FK update).
- ✗ Cast en queries que comparan numéricamente (e.g., "todos los puertos del distrito 25" sería más complejo).
- ✗ JOIN con `dim_port` en facts requiere `port_id` (smallint surrogate) — port_code es solo natural key. El cambio de tipo no afecta operaciones rutinarias.

### Alt B — Descartar las filas XX, documentar como gap

- ✓ Cero cambios al schema.
- ✗ -13% de subestimación en cualquier total nacional. V5 falla validación contra cifra pública BTS.
- ✗ Pérdida de información significativa para el caso de uso "share Otay sobre nacional".

### Alt C — Crear una tabla separada `dim_port_aggregate` para los XX

- ✗ Doble FK en `fact_transborder` (port_id puede apuntar a `dim_port` O a `dim_port_aggregate`) — antipatrón fuerte en estrella.

## Consecuencias

### Positivas

- V5 (Total Mexico→US 2023) ahora **$795.9B** = ~99.5% del público BTS (~$799B). Diferencia residual explicable por gaps menores (años faltantes 2008/2010/2011 tienen ínfima contribución a 2023).
- Cero filas legítimas perdidas por mapping fallido.
- `is_aggregate=TRUE` permite excluir limpio en rankings de "puertos físicos" sin lógica adicional: `WHERE p.is_aggregate=FALSE`.

### Negativas

- `dim_port` creció de 27 a 94 filas (27 físicos + 25 numéricos aggregates + 42 XX-sintéticos). Nada significativo.
- El name `District 25XX residual` no es informativo per se — es un compuesto sintético. Documentado.
- Cualquier dashboard que itere `dim_port` SIN filtrar `is_aggregate=FALSE` mostrará entries raras como "District 41XX residual" (Buffalo). Mitigación: convención de queries documentada en `KNOWN_GAPS.md` G7.

### Convenciones

- **Rankings de puertos físicos** ("top 5 POE por valor"): siempre `WHERE p.is_aggregate=FALSE`.
- **Totales nacionales** ("total Mexico→US 2023"): NO filtrar — los aggregates contienen valor real.
- **Mapas geográficos**: usar `WHERE p.latitude IS NOT NULL` (los aggregates tienen lat/lon NULL).
