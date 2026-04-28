# Known Gaps — Frontera Tijuana–San Diego

Listado consolidado de huecos de cobertura conocidos. **Cualquier query del dashboard debe considerarlos.**

Última actualización: **2026-04-26** (cierre Fase 4 — datos cargados al estado actual).

---

## TransBorder Freight (`fact_transborder`)

**Cobertura efectiva**: 178 meses entre Ene 2007 y Mar 2025 (esperados 219 meses → 41 meses faltantes ≈ 19% del rango).

### G1 — Año 2006 entero ausente (12 meses)

- **Causa**: el ZIP `2006/tbdrRDzip170406164948.zip` en bts.gov tiene formato legacy con inner ZIPs cifrados (`1701.zip`, `060112.zip`) que no matchean el regex `dot3_MMYY.csv`.
- **Impacto**: tendencias largas arrancan en 2007-01 en lugar de 2006-01.
- **Solución potencial**: lógica especial de parsing legacy (binarios sin extensión, posiblemente compresión vieja). No priorizado.
- **Acción visual recomendada**: que los gráficos de tendencia muestren el inicio en 2007 con badge "Datos disponibles desde 2007-01".

### G2 — Años 2008, 2010, 2011 ausentes (36 meses)

- **Causa**: los ZIPs anuales (`2008.zip`, `Revised2010PublicData.zip`, `Revised2011PublicData.zip`) están en Wayback Machine pero **truncados** (Wayback sirve un cuerpo HTTP corto que falla `zipfile.testzip()`). bts.gov geo-bloqueado para descarga directa desde MX (Akamai 403).
- **Impacto**: gráficos largo plazo tienen 3 ranuras vacías. Pico de la crisis financiera 2008 + recuperación post-crisis 2010-11 ausentes.
- **Acción visual recomendada**: NO interpolar. Romper la línea (`NULL` explícito en el dataset, no fill-forward).
- **Solución potencial**: bajar desde IP US (VPN/colaborador) o esperar que Wayback recapture (improbable porque su crawler también recibe 403 de bts.gov, ver G4).

### G3 — Diciembre 2024 + Q1 2025 — fuente mixta (`data_source='sandag_mirror'`)

- **Cobertura**:
  - Dic 2024 + Ene/Feb/Mar 2025 = `data_source='sandag_mirror'` (3,368 filas totales).
  - Solo distrito 25 (puertos 2501-2507: Andrade, Calexico, Calexico East, San Ysidro, Tecate, Otay Mesa Station + el agregado distrito).
  - Resto de puertos US-Mexico (Texas Sur, Texas Oeste, Arizona) **NO tienen** Dic 2024 ni Q1 2025.
- **Acción de query**:
  - Análisis del corredor SD/Imperial → sin filtro adicional, las 4 fechas están cubiertas.
  - Análisis nacional → `WHERE data_source = 'bts_national'` (último mes cargado: noviembre 2024).
- **Causa última**: Wayback Machine no archivó los ZIPs nacionales de Dic 2024 ni de Ene/Feb/Mar 2025. `/save/` SPN devolvió HTTP 520 (Akamai bloquea al crawler de Wayback también).

### G4 — Apr 2025 en adelante: nada (ni nacional ni SANDAG si caduca)

- Wayback Machine **no puede capturar nuevos snapshots** de `bts.gov/sites/bts.dot.gov/files/transborder-raw/...` porque Akamai sirve HTTP 520 al crawler de archive.org.
- El SANDAG mirror `k3a4-5ygm` cubre Apr 2025+ pero solo distrito 25 (mismo caso que G3).
- **Implicación operativa**: para mantener actualizado el corredor mensualmente, basta re-correr el adapter SANDAG (`etl/05_load_fact_transborder.py` parte SANDAG) extendiendo los meses target. Para el resto del país, requiere alternativa de acceso (VPN US, etc.).

### G5 — ~178 filas con COMMODITY2 fuera del catálogo HS-2

- **Causa**: valores `COMMODITY2 = 99` u otros códigos exóticos no documentados en HS-2 estándar (que va 1-97 + 98 incluido en nuestro catálogo).
- **Impacto**: ~0.015% del total de filas, valor despreciable. Descarte silencioso en ETL con log.
- **No-acción**: dejarlo así. Si crecen significativamente, agregar mapping para HS-99.

### G6 — Bucket `Rail (legacy code 2)` actualmente vacío

- **Estado**: 0 filas. Mantenido como fallback para futuras cargas si raw nacional reintroduce DISAGMOT 2.
- **Origen histórico**: en mi heurística inicial de Fase 3 supuse `DISAGMOT 2 = Rail`. Docs oficiales BTS (publicadas Apr 2026) confirmaron que DISAGMOT 2 NO existe; el verdadero Rail es DISAGMOT 6. Ver `docs/decisions/004-disagmot-6-rail.md`.

### G7 — DEPE 'XX' residuales recuperados con códigos sintéticos

- 309,872 filas con DEPE tipo `20XX, 41XX, 25XX, ...` (residuales de distrito CBP) se mapean a port_codes sintéticos `_99` (ej. `25XX → 2599`) en `dim_port` con `is_aggregate=TRUE`.
- **Acción de query para rankings de puertos físicos**: agregar `WHERE p.is_aggregate=FALSE`.
- **Acción de query para totales nacionales**: NO filtrar — los aggregates contienen flujos reales, su omisión causaría -13% de subestimación.

---

## Border Crossing (`fact_border_crossing`)

**Cobertura**: Ene 1996 → Mar 2026 (30 años, mensual). 65,550 filas tras filtros.

### G8 — San Ysidro deja de reportar Trucks/Trains/Train Passengers en 2016-07

- **No es bug**. La carga migró operativamente a Otay Mesa.
- **Datos visibles**: 0 trucks en Jun y Jul 2016 (placeholder de BTS), después la fila desaparece del dataset.
- Caveat documentado en `COMMENT ON TABLE frontera.fact_border_crossing` (R3 del reporte Fase 1).
- **Acción de query**: si reportás "carga histórica de San Ysidro", ponerla como discontinuada en 2016-07 con anotación. Para "carga del corredor", agregar Otay desde 2016-08+.

### G9 — Tecate también pierde Buses (2023-02), Bus Passengers (2017-03), Trains (2016-07)

- **Realidad operativa CBP**: Tecate redujo servicio comercial; carga consolidada en Otay/Calexico Este.
- **Acción de query**: similar a G8, anotar como discontinuado.

### G10 — 14 filas duplicadas en source en `2019-06-01`

- 14 filas con misma PK (`port_id, mode_id, date_id`) pero `crossing_count` distinto, en distintos puertos del Texas valle.
- Comportamiento conocido del CSV de BTS (re-ingest accidental ese mes).
- **Resolución en ETL**: agregadas con `SUM(crossing_count)` → 65,557 input → 65,550 output (-7 grupos colapsados de 2-en-1).
- **Impacto**: <0.022% del total. Defendible.

### G11 — CBX (Cross Border Xpress) excluido del scope

- Decisión arquitectónica documentada en `docs/decisions/006-cbx-fuera-de-scope.md`.
- CBX aparece como puerto separado en BTS desde 2017-10 con solo `Personal Vehicles`.
- Filtrado en ETL: `df[df["Port Name"] != "Cross Border Xpress"]`.

---

## Wait Times (`fact_wait_time`)

**Cobertura**: Ene 2023 → Apr 2026 (3.2 años, diario). 25,894 filas.

### G12 — Cobertura solo desde 2023-01-29

- **Limitación nativa del dataset SANDAG `5tga-nezt`**. SANDAG empezó a publicarlo en 2023.
- **Acción visual**: la sección de wait times del dashboard tiene horizonte natural 2023+. NO extender artificialmente con interpolación o backfill.

### G13 — Algunos puertos no operan ciertos carriles

- **San Ysidro**: NO tiene Commercial (no es puerto comercial — ver G8).
- **Tecate**: solo Commercial Standard (sin Fast).
- **Andrade**: solo POV Standard + Pedestrian Standard (carriles limitados).
- **Calexico (West)**: sin Commercial (esos cruzan por Calexico East).
- **Calexico East**: tiene Commercial Fast/Standard pero NO Pedestrian Ready.
- **No es gap, es realidad operativa CBP**. Las queries deben tolerar combinaciones puerto×carril vacías sin asumir error.

### G14 — Calexico East / Tecate Commercial: cobertura reducida

- Calexico East Commercial Fast/Standard: 982 días (vs 1,160 para POE 24/7).
- Tecate Commercial Standard: 778 días.
- **Causa**: estos POE no operan fines de semana / feriados; CBP no genera registro de wait time.
- **Acción de query**: para promedios, NO dividir por días-calendario; usar `COUNT(*)` real.

---

## Resumen para frontend

| Sección dashboard | Filtros recomendados | Caveats visuales |
|---|---|---|
| Tendencia largo plazo TransBorder | `data_source='bts_national'` | Marcar gaps 2008/2010/2011 con NULL explícito, no interpolar |
| Mapa nacional Mexico→US | `data_source='bts_national'` AND `is_aggregate=FALSE` para "puertos físicos"; sin filtro para totales | — |
| Corridor SD/Tijuana mensual | sin filtro (incluye SANDAG mirror para 2024-12+) | — |
| Border Crossings histórico | sin filtro adicional | Notar discontinuidad San Ysidro Trucks 2016-07 |
| Wait Times | sin filtro adicional | Empieza 2023-01; combinaciones puerto×carril vacías son por diseño |
