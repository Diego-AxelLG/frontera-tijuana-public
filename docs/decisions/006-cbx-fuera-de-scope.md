# ADR 006 — Cross Border Xpress (CBX) fuera de scope

**Fecha:** 2026-04-26 · **Estado:** decidido

## Contexto

CBX (Cross Border Xpress) es un cruce **peatonal exclusivo para pasajeros del aeropuerto de Tijuana** que conecta directamente la terminal del aeropuerto con un edificio en San Diego. Operativo desde **2015**, BTS lo agregó como puerto separado en `keg4-3bc2` (Border Crossing Entry Data) **desde 2017-10**.

Características distintivas:
- Solo `Personal Vehicles` (no Trucks, no Pedestrians, no Buses) — el "vehículo" es el shuttle del aeropuerto.
- Volumen ~3M cruces/año (comparable a un puerto secundario).
- **NO aparece en TransBorder Freight** (no es puerto de carga comercial).
- **NO está en SANDAG Wait Times** (5tga-nezt) — solo cubre los POE tradicionales.

## Decisión

**Excluir CBX completamente del scope del proyecto**:
- En `etl/01_load_dimensions.py` (dim_port loader): filtrar `df[df["Port Name"] != "Cross Border Xpress"]`.
- En `etl/06_load_fact_border_crossing.py`: mismo filtro.
- En el dashboard: no se mencionará ni se contará en totales del corridor.

`dim_port` no contiene CBX. `fact_border_crossing` no tiene filas de CBX.

## Alternativas consideradas

### Alt A — Incluir CBX como puerto separado

- ✓ Cobertura literal "todos los cruces del corredor".
- ✗ Distorsiona estadísticas de pasajeros: los cruces CBX son de un perfil completamente distinto (turistas internacionales pasando por TIJ vs locales/transfronterizos cruzando San Ysidro/Otay).
- ✗ Asimetría brutal: CBX solo tiene `Personal Vehicles` como métrica. Cualquier query "share corridor" requeriría tratamiento especial para CBX (no aparece en wait times, no aparece en freight).
- ✗ Discontinuidad temporal: CBX aparece en BTS desde 2017-10. Cualquier serie histórica antes de esa fecha tendría salto artificial al incluirlo.

### Alt B — Agregarlo a "San Ysidro extendido"

- ✗ Mezclaría dos cruces operativamente distintos. Empeora ambos.
- ✗ El usuario del dashboard que ve "San Ysidro" esperaría el POE tradicional.

### Alt C — Tabla separada `dim_port_special` para CBX y casos similares

- ✗ Over-engineering para un puerto cuyo perfil es radicalmente distinto al resto. Mejor scope-out limpio.

## Consecuencias

### Positivas

- Schema y queries del dashboard tratan uniformemente todos los puertos (Trucks + Personal Vehicles + Pedestrians + Buses + Trains + Train/Bus Passengers en BC; los 8 modos en TransBorder).
- No hay discontinuidades temporales artificiales (CBX entra en 2017-10, lo cual partiría cualquier "tendencia desde 1996").
- Documentación clara: "Frontera Tijuana–San Diego" en el dashboard se refiere a los **3 POE tradicionales**: San Ysidro, Otay Mesa, Tecate. CBX es un cruce especializado de aeropuerto y se excluye.

### Negativas

- Tracker de "tráfico aéreo Tijuana → San Diego" no se puede hacer con esta data. Para análisis de turismo aéreo, se requiere otra fuente (estadísticas TIJ aeropuerto).
- Si el usuario del dashboard pregunta "¿cuántas personas cruzan a San Diego desde Tijuana?", la respuesta del dashboard será 22.6M (corridor sin CBX) en lugar de ~25.6M (corridor + CBX). Mitigación: badge en el dashboard "Excluye CBX (cruce exclusivo aeropuerto)".

### Línea futura

Si en una fase posterior queremos incluir CBX:
- Agregar como `port_canonical_name = 'Cross Border Xpress'`, `is_in_corridor=TRUE`, con campos lat/lon específicos.
- En BC ETL: quitar el filtro `!= 'Cross Border Xpress'`.
- En queries del dashboard: agregar opción "Incluir CBX" para vistas de pasajeros.
- Revisitar este ADR.
