# ADR 001 — Fuente TransBorder: nacional (bts.gov) vs SANDAG mirror (`k3a4-5ygm`)

**Fecha:** 2026-04-26 · **Estado:** decidido · **Híbrido**

## Contexto

Para construir el dashboard del corredor Tijuana–San Diego necesitamos data de comercio Mexico→US (TransBorder Freight). Hay dos fuentes accesibles:

1. **BTS nacional** (`bts.gov/sites/bts.dot.gov/files/transborder-raw/`): ZIPs mensuales/anuales con tres tablas (dot1=puerto×modo×estado, dot2=estado×commodity, dot3=puerto×commodity×modo). Cobertura completa todos los puertos US-Mexico (28 distritos), 1993-presente.

2. **SANDAG mirror** (`opendata.sandag.org/resource/k3a4-5ygm`): SODA API con la misma data atribuida a BTS pero filtrada al condado de San Diego/Imperial (puertos 2501-2507). Cobertura 2006-presente, schema con texto descriptivo en lugar de códigos numéricos.

**Restricción crítica detectada en Fase 3.5**: bts.gov geo-bloquea (Akamai 403) todo el tráfico desde IPs mexicanas. Confirmamos vía cross-check de Mar 2024 Otay que ambas fuentes producen byte-a-byte la misma data — SANDAG es subset literal del nacional.

## Decisión

**Híbrido**: usar nacional como fuente primaria + SANDAG como adapter para los meses que el nacional no cubre.

Concretamente:
- **Nacional vía Wayback Machine**: bajar 108 ZIPs (1993-Mar 2025), extraer dot3 CSVs, cargar como `data_source='bts_national'`. Cobertura efectiva: 178 meses entre Ene 2007 y Nov 2024.
- **SANDAG adapter**: cargar Dec 2024 + Q1 2025 desde la SODA API como `data_source='sandag_mirror'`. Cobertura: 4 meses, solo distrito 25.

La columna `data_source` en `fact_transborder` permite filtrar según el caso de uso.

## Alternativas consideradas

### Alt A — Solo SANDAG mirror

- ✓ Más simple operativamente (API estable, no requiere Wayback).
- ✓ Cobertura completa hasta presente.
- ✗ **Solo distrito 25** — imposible calcular "share Otay sobre total nacional Mexico→US".
- ✗ Si SANDAG degrada el mirror, perdemos todo (riesgo de proveedor único). En Fase 3.5 ya detectamos hueco en Oct 2025.

### Alt B — Solo nacional vía Wayback

- ✓ Cobertura geográfica completa.
- ✗ Wayback no archivó Dec 2024 ni Q1 2025 (los ZIPs no fueron crawled antes del bloqueo Akamai). `/save/` SPN devuelve 520 ahora porque bts.gov bloquea al crawler de Wayback también.
- ✗ Sin esos meses, el dashboard tiene gap de 4 meses recientes — UX inaceptable.

### Alt C — Esperar acceso US directo (VPN/proxy)

- ✓ Resuelve todo limpio.
- ✗ Requiere infraestructura adicional, costo, dependencia operativa.
- ✗ Bloquea el proyecto indefinidamente.

## Consecuencias

### Positivas

- Tenemos cobertura nacional completa hasta Nov 2024 + corridor hasta Mar 2025.
- El cross-check Mar 2024 Otay ($2,964,387,972 byte-a-byte) confirma equivalencia entre fuentes.
- La columna `data_source` deja explícito de dónde vino cada fila — auditable.

### Negativas

- 4 meses (Dec 2024 - Mar 2025) tienen cobertura asimétrica: corridor sí, resto del país no. Cualquier query "share corridor sobre nacional" en esa ventana dará un resultado sesgado al alza si no se controla. Mitigación: documentado en `KNOWN_GAPS.md` G3.
- Schema más complejo: `data_source` agrega una dimensión más de filtrado que cualquier query de denominador nacional debe respetar.
- Re-cargar el corredor mensualmente requiere re-correr SOLO el adapter SANDAG (parte 2 de `etl/05_load_fact_transborder.py`). Re-cargar el nacional requiere bajar nuevos ZIPs vía Wayback — improbable hasta que bts.gov desbloqueé o usemos otra IP.

### Riesgos pendientes

- Si SANDAG mirror degrada (ej. deja de actualizar), perdemos los 4 meses recientes. Mitigación: monitorear SANDAG con query mensual de `count(*)` en último mes esperado.
