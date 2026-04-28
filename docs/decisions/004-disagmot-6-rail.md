# ADR 004 — DISAGMOT 6 = Rail (corrección con docs BTS oficiales)

**Fecha:** 2026-04-26 · **Estado:** decidido (corrección de un error previo)

## Contexto

El raw nacional TransBorder codifica el modo de transporte en columna `DISAGMOT` (entero). En Fase 3, sin docs oficiales a mano, mapeé heurísticamente (basado en frecuencia + cross-check parcial con SANDAG):

```
1: Vessel, 2: Rail, 3: Air, 4: Mail, 5: Truck,
6: Other (DISAGMOT 6),     ← bucket de "no sé qué es"
7: Pipeline, 8: Other, 9: Foreign Trade Zone
```

Esta heurística era **incorrecta**. En Fase 4A.3.5 detectamos:
- DISAGMOT 6 tiene 88,746 filas con $586B Automotive acumulado, dominado por Laredo + Eagle Pass.
- DISAGMOT 2 tenía solo 186 filas (atribuidas a "Rail") — sospechosamente bajo para flujo Mexico-US Rail real.

Patrón Auto+Laredo+volumen alto = Rail intermodal. La hipótesis fue confirmada cuando aparecieron las docs BTS oficiales (Apr 2026):

> https://www.bts.gov/sites/bts.dot.gov/files/docs/browse-statistical-products-and-data/transborder-freight-data/220171/all-codes-north-american-transborder-freight-raw-data.pdf
>
> DISAGMOT codes:
> 1=Vessel, 3=Air, 4=Mail, 5=Truck, **6=Rail**, 7=Pipeline, 8=Other, 9=FTZ
> (DISAGMOT 2 NO está en docs)

## Decisión

1. **Renombrar el modo "Other (DISAGMOT 6)" → "Rail"** (mode_id=15, source_dataset='both').
2. **Renombrar el "Rail" antiguo (mode_id=2) → "Rail (legacy code 2)"** (source_dataset='transborder', currently 0 rows).
3. **Mantener el bucket mode_id=2** como fallback por si raw nacional reintroduce DISAGMOT 2 en el futuro.
4. Persistir el lookup oficial en `etl/lookups/disagmot_lookup.json`:
   ```json
   {
     "1": "Vessel", "3": "Air", "4": "Mail", "5": "Truck",
     "6": "Rail", "7": "Pipeline", "8": "Other", "9": "Foreign Trade Zone"
   }
   ```
   (sin entry para 2 — per docs oficiales).
5. Actualizar `DISAGMOT_TO_MODE_NAME` en `etl/05_load_fact_transborder.py` con la corrección + entry para `2 → "Rail (legacy code 2)"` como fallback runtime.
6. **Migrar las 186 filas que estaban en mode_id=2**: investigación reveló que NO eran DISAGMOT 2 reales — eran **filas SANDAG-Rail mis-mapeadas por bug en el adapter** (el `mode_name_to_id` se construyó cuando "Rail" aún apuntaba al slot vacío). Se ejecutó `UPDATE fact_transborder SET mode_id=15 WHERE mode_id=2;` para corregir.

## Alternativas consideradas

### Alt A — Mantener "Other (DISAGMOT 6)" como nombre

- ✗ Engañoso: hace pensar que DISAGMOT 6 es residual cuando es el segundo modo más importante por valor (~$1.1T acumulado 2007-2024).
- ✗ Cualquier query "Top modes" con label "Other (DISAGMOT 6)" en #2 confunde al lector del dashboard.

### Alt B — Eliminar el bucket mode_id=2 (nadie lo usa)

- ✓ DB más limpia.
- ✗ Si BTS reintroduce DISAGMOT 2 en futuras publicaciones (improbable pero posible), el ETL fallaría en el mapping — perderíamos filas silenciosamente.
- ✗ Pierde la trazabilidad histórica del bug.

### Alt C — Combinar Rail (legacy 2) + Rail en un solo modo

- ✗ Rompe el principio "un mode_id por código fuente distinto" — dificulta debugging si en el futuro DISAGMOT 2 aparece y se mezcla con DISAGMOT 6 sin trazabilidad.

## Consecuencias

### Positivas

- V2 (top 5 modes 2023) ahora muestra Rail = $95.4B en posición #2, congruente con la realidad operativa Mexico-US (Laredo + Eagle Pass dominan rail, sectores Automotriz/Electrónica como esperado).
- V4 (Rail por puerto 2023): Laredo $49.8B + Eagle Pass $26.7B = 80% del rail nacional, coincide con corredores KCS de México y Union Pacific.
- Lookup oficial documentado y reproducible.
- Bucket fallback `Rail (legacy code 2)` mantiene robustez ante futuras divergencias de schema.

### Negativas

- Una columna `mode_canonical_name` en `dim_mode` extendida de VARCHAR(40) a VARCHAR(60) para acomodar el nombre largo "Rail (DISAGMOT 2 - investigación pendiente)" que luego acortamos a "Rail (legacy code 2)" (21 chars). El cambio VARCHAR(60) ya está aplicado, no requiere revertir.

### Lecciones aprendidas

- En ausencia de docs oficiales, cualquier mapping de códigos se debe **etiquetar como "investigación pendiente"** en lugar de adivinar nombres descriptivos. Mi error fue confundir DISAGMOT 6 con "Other" basándome en frecuencia.
- Cross-checks byte-a-byte (Mar 2024 Otay Truck Import) son insuficientes para revelar errores en modos no-Truck.
- **Bug latente en SANDAG adapter**: el `mode_name_to_id` lookup se construyó al inicio del script. Después de cambiar el nombre del modo, el lookup quedó stale para la segunda parte del ETL. Solución futura: re-leer lookups después de cualquier modificación a `dim_mode` dentro del mismo run.
