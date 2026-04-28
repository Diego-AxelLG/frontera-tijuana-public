# ADR 005 — Granularidad de commodity: HS-2, no HS-6

**Fecha:** 2026-04-26 · **Estado:** decidido (sin alternativa práctica disponible)

## Contexto

El sistema Harmonized System (HS) para clasificación de commodities tiene jerarquía:
- **HS-2** = capítulo (97-99 capítulos: "84=Maquinaria mecánica", "87=Vehículos", etc.)
- **HS-4** = partida (~1,200 entries más específicos: "8703=Automóviles de pasajeros")
- **HS-6** = subpartida (~5,300 entries: "870323=Autos 1500-3000cc")
- **HS-10** = clasificación nacional final (Schedule B / HTS).

El dataset BTS TransBorder raw (dot1/2/3) usa columna `COMMODITY2` que es int 1-99 → **solo HS-2**.

El dashboard probablemente quisiera análisis sectorial fino (e.g., "autos completos vs autopartes vs motores") que requiere HS-4 o HS-6.

## Decisión

**Quedarnos en HS-2** (granularidad nativa del dataset accesible) y agregar columna derivada `sector_grouping` en `dim_commodity_hs2` que agrupa los 97 capítulos HS-2 en 13 sectores legibles para visualización (Automotriz, Electrónica/Maquinaria, Médico/Precisión, etc.).

## Alternativas consideradas

### Alt A — Bajar dataset BTS HS-6 (Public Use file separado)

- ✓ Granularidad real para análisis sectorial fino.
- ✗ Es un archivo separado, mucho más pesado (estimo 5-10 GB descomprimido para 2007-2024).
- ✗ Si bts.gov sigue geo-bloqueado para nosotros, mismo problema que el TransBorder raw.
- ✗ El hueco principal del proyecto NO es la falta de granularidad de commodity sino la cobertura geográfica del corredor — sería over-engineering para Phase 1 del dashboard.

### Alt B — Imputar HS-6 desde HS-2 + heurística

- ✗ No-no. Pierde precisión, introduce ruido, propenso a equivocaciones graves.

### Alt C — Usar `COMMODITY2` directo sin sector grouping

- ✓ Más simple.
- ✗ 97 categorías son demasiadas para un eje X de gráfico legible. Sector grouping (13 buckets) es manejable.

## Consecuencias

### Positivas

- Schema simple y consistente con la fuente.
- Sector grouping (13 buckets curados) es ergonomico para visualización.
- ETL más simple — un solo nivel de mapping.

### Negativas

- **Imposible distinguir** "autos terminados vs autopartes" — ambos caen en HS-87 = Automotriz. Limitación importante para análisis profundo de la industria del corredor (tiene mucha autoparte).
- Si el dashboard requiere análisis HS-4+ a futuro, hay que rediseñar este dim. Mitigación: la columna `hs2_description_full` conserva el texto original; agregar HS-4 sería una nueva tabla `dim_commodity_hs4` con FK a HS-2.

### Decisión de diseño concreta

`dim_commodity_hs2`:
- `hs2_code` SMALLINT UNIQUE (1-99, sin HS-77 reservado, sin HS-99 que el dataset no separa de HS-98).
- `hs2_description_full` TEXT — descripción literal del dataset (ej. "Vehicles, other than railway or tramway rolling stock, and parts and accessories thereof").
- `hs2_description_short` VARCHAR(80) — versión corta en español para tooltips/ejes (ej. "Vehículos").
- `sector_grouping` VARCHAR(30) — bucket sectorial para visualización (ej. "Automotriz").

13 sectores definidos en `etl/01_load_dimensions.py` HS2_CATALOG, coverage:
- Agro/Alimentos: 24 caps (HS 1-24)
- Textil/Calzado: 18 caps (HS 50-67)
- Química/Cuero: 13 caps
- Metales: 11 caps (HS 72-83 excl 77)
- Otros: 7 caps (88, 89, 91, 92, 93, 97, 98)
- Madera/Papel: 6 caps (44-49)
- Energía/Minerales: 4 caps (25-27, 71)
- Manufacturas varias: 3 (94, 95, 96)
- Construcción: 3 (68-70)
- Electrónica/Maquinaria: 2 (84, 85)
- Plásticos/Caucho: 2 (39, 40)
- **Automotriz: 2 (86 ferroviario, 87 vehículos)**
- Médico/Precisión: 2 (30 farmacéuticos, 90 instrumentos)
