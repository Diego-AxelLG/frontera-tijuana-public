"""ETL Fase 3 — carga de las 5 dimensiones de frontera.*

Idempotente vía TRUNCATE ... RESTART IDENTITY CASCADE en cada función.
Solo afecta tablas en schema `frontera`. NO toca otras DBs.
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PG = dict(
    host=os.environ["PG_HOST"],
    port=int(os.environ["PG_PORT"]),
    user=os.environ["PG_USER"],
    password=os.environ["PG_PASSWORD"],
    dbname=os.environ["PG_DATABASE"],
)


# =====================================================================
# Catálogos canónicos
# =====================================================================

# --- dim_mode -------------------------------------------------------
# (canonical_name, source_dataset)
# Sincronizado en sub-fase 4B con el estado actual del DB:
#   - "Rail" canónico = DISAGMOT 6 (verdadero Rail per docs BTS Apr 2026) + BC "Trains".
#     Verificado: BC tiene 7,005 filas "Trains" → confirma Rail en ambos datasets.
#   - "Rail (legacy code 2)" = bucket fallback por si raw nacional reintroduce DISAGMOT 2.
#     Actualmente vacío. Las 186 filas que tenía eran SANDAG mis-mapeadas, ya migradas.
CANONICAL_MODES = [
    ("Truck",                       "both"),
    ("Rail",                        "both"),                # DISAGMOT 6 + BC Trains
    ("Rail (legacy code 2)",        "transborder"),         # fallback DISAGMOT 2
    ("Personal Vehicle",            "border_crossing"),
    ("Personal Vehicle Passenger",  "border_crossing"),
    ("Pedestrian",                  "border_crossing"),
    ("Bus",                         "border_crossing"),
    ("Bus Passenger",               "border_crossing"),
    ("Train Passenger",             "border_crossing"),
    ("Air",                         "transborder"),
    ("Vessel",                      "transborder"),
    ("Pipeline",                    "transborder"),
    ("Foreign Trade Zone",          "transborder"),
    ("Mail",                        "transborder"),
    ("Other",                       "transborder"),
]

# Mapping de nombres de origen → canónico (lo usará el ETL de facts en Fase 4)
BC_MODE_TO_CANONICAL = {
    "Trucks":                       "Truck",
    "Trains":                       "Rail",
    "Personal Vehicles":            "Personal Vehicle",
    "Personal Vehicle Passengers":  "Personal Vehicle Passenger",
    "Pedestrians":                  "Pedestrian",
    "Buses":                        "Bus",
    "Bus Passengers":               "Bus Passenger",
    "Train Passengers":             "Train Passenger",
}

TB_MODE_TO_CANONICAL = {
    "Truck":                        "Truck",
    "Rail":                         "Rail",
    "Air":                          "Air",
    "Vessel":                       "Vessel",
    "Pipeline":                     "Pipeline",
    "Foreign Trade Zones (FTZs)":   "Foreign Trade Zone",
    "Mail (U.S. Postal Service)":   "Mail",
    "Other":                        "Other",
}

# --- dim_lane_type --------------------------------------------------
# (canonical_name, vehicle_category, is_trusted_traveler)
LANE_TYPES = [
    ("Commercial Fast",     "Commercial", True),
    ("Commercial Standard", "Commercial", False),
    ("POV Standard",        "POV",        False),
    ("POV NEXUS/SENTRI",    "POV",        True),
    ("Pedestrian Standard", "Pedestrian", False),
    ("Pedestrian Ready",    "Pedestrian", True),
]

WAIT_LANE_NAME_TO_CANONICAL = {
    "Commercial_Fast_Lane":                  "Commercial Fast",
    "Commercial_Standard_Lane":              "Commercial Standard",
    "Passenger_vehicle_Standard_Lane":       "POV Standard",
    "Passenger_vehicle_NEXUS_SENTRI_Lane":   "POV NEXUS/SENTRI",
    "Pedestrian_standard_Lane":              "Pedestrian Standard",
    "Pedestrian_Ready_Lanes":                "Pedestrian Ready",
}

# --- dim_port: canonicalización de port_name (para fase 4 fact ETL) -
PORT_NAME_TO_CANONICAL = {
    "Otay Mesa Station":  "Otay Mesa",     # TransBorder
    "Calexico-East":      "Calexico East", # TransBorder
    "Calexico_East":      "Calexico East", # Wait Times
    "Calexico_West":      "Calexico",      # Wait Times (West POE = "Calexico" en BC)
}

CORRIDOR_PORTS = {"San Ysidro", "Otay Mesa", "Tecate"}

# --- dim_commodity_hs2 ---------------------------------------------
# 97 capítulos. Cada entrada: (hs2_code, full_description_punctuated,
#                              short_es, sector_grouping)
# `full_description_punctuated` se usa también como ancla:
# normalize(full) → debe matchear con normalize(commodity) del dataset.
HS2_CATALOG = [
    (1,  "Live animals",                                                                                 "Animales vivos",            "Agro/Alimentos"),
    (2,  "Meat and edible meat offal",                                                                   "Carne",                     "Agro/Alimentos"),
    (3,  "Fish and crustaceans, mollusks and other aquatic invertebrates",                               "Pescados y mariscos",       "Agro/Alimentos"),
    (4,  "Dairy produce; Birds' eggs; Natural honey; Edible products of animal origin, not elsewhere specified or included", "Lácteos y huevos", "Agro/Alimentos"),
    (5,  "Products of animal origin, not elsewhere specified or included",                               "Productos animales",        "Agro/Alimentos"),
    (6,  "Live trees and other plants; Bulbs, roots and the like; Cut flowers and ornamental foliage",   "Plantas y flores",          "Agro/Alimentos"),
    (7,  "Edible vegetables and certain roots and tubers",                                               "Hortalizas",                "Agro/Alimentos"),
    (8,  "Edible fruit and nuts; Peel of citrus fruit or melons",                                        "Frutas y nueces",           "Agro/Alimentos"),
    (9,  "Coffee, tea, mate and spices",                                                                 "Café y especias",           "Agro/Alimentos"),
    (10, "Cereals",                                                                                      "Cereales",                  "Agro/Alimentos"),
    (11, "Products of the milling industry; Malt; Starches; inulin; Wheat gluten",                       "Harinas y almidones",       "Agro/Alimentos"),
    (12, "Oil seeds and oleaginous fruits; Miscellaneous grains; Seeds and fruit; Industrial or medicinal plants; Straw and fodder", "Semillas oleaginosas", "Agro/Alimentos"),
    (13, "Lac; Gums; Resins and other vegetable saps and extract",                                       "Gomas y resinas",           "Agro/Alimentos"),
    (14, "Vegetable plaiting materials; Vegetable products not elsewhere specified or included",         "Materias vegetales",        "Agro/Alimentos"),
    (15, "Animal or vegetable fats and oils and their cleavage products; Prepared edible fats; Animal or vegetable waxes", "Grasas y aceites", "Agro/Alimentos"),
    (16, "Preparations of meat, of fish, or of crustaceans, mollusks or other aquatic invertebrates",    "Preparaciones cárnicas",    "Agro/Alimentos"),
    (17, "Sugars and sugar confectionery",                                                               "Azúcares",                  "Agro/Alimentos"),
    (18, "Cocoa and cocoa preparations",                                                                 "Cacao",                     "Agro/Alimentos"),
    (19, "Preparations of cereals, flour, starch or milk; Bakers' wares",                                "Preparaciones cereales",    "Agro/Alimentos"),
    (20, "Preparations of vegetables, fruit, nuts, or other parts of plants",                            "Preparaciones vegetales",   "Agro/Alimentos"),
    (21, "Miscellaneous edible preparations",                                                            "Preparaciones varias",      "Agro/Alimentos"),
    (22, "Beverages, spirits and vinegar",                                                               "Bebidas",                   "Agro/Alimentos"),
    (23, "Residues and waste from the food industries; Prepared animal feed",                            "Residuos alimentarios",     "Agro/Alimentos"),
    (24, "Tobacco and manufactured tobacco substitutes",                                                 "Tabaco",                    "Agro/Alimentos"),
    (25, "Salt; Sulfur; Earths and stone; Plastering materials, lime and cement",                        "Sales y minerales",         "Energía/Minerales"),
    (26, "Ores, slag and ash",                                                                           "Minerales metalíferos",     "Energía/Minerales"),
    (27, "Mineral fuels, mineral oils and products of their distillation; Bituminous substances; Mineral waxes", "Combustibles minerales", "Energía/Minerales"),
    (28, "Inorganic chemicals; Organic or inorganic compounds of precious metals, of rare-earth metals, of radioactive elements or of isotopes", "Químicos inorgánicos", "Química/Cuero"),
    (29, "Organic chemicals",                                                                            "Químicos orgánicos",        "Química/Cuero"),
    (30, "Pharmaceutical products",                                                                      "Farmacéuticos",             "Médico/Precisión"),
    (31, "Fertilizers",                                                                                  "Fertilizantes",             "Química/Cuero"),
    (32, "Tanning or dyeing extracts; Tannins and their derivatives; Dyes, pigments and other coloring matter; Paints and varnishes; Putty and other mastics; Inks", "Tintes y pinturas", "Química/Cuero"),
    (33, "Essential oils and resinoids; Perfumery, cosmetic or toilet preparations",                     "Cosméticos",                "Química/Cuero"),
    (34, "Soap, organic surface-active agents, washing preparations, lubricating preparations, artificial waxes, prepared waxes, polishing or scouring preparations, candles and similar articles, modeling pastes, dental waxes and dental preparations with a basis of plaster", "Jabones", "Química/Cuero"),
    (35, "Albuminoidal substances; Modified starches; Glues; Enzymes",                                   "Albuminoides",              "Química/Cuero"),
    (36, "Explosives; Pyrotechnic products; Matches; Pyrophoric alloys; Certain combustible preparations", "Explosivos",              "Química/Cuero"),
    (37, "Photographic or cinematographic goods",                                                        "Fotografía",                "Química/Cuero"),
    (38, "Miscellaneous chemical products",                                                              "Químicos varios",           "Química/Cuero"),
    (39, "Plastics and articles thereof",                                                                "Plásticos",                 "Plásticos/Caucho"),
    (40, "Rubber and articles thereof",                                                                  "Caucho",                    "Plásticos/Caucho"),
    (41, "Raw hides and skins (other than furskins) and leather",                                        "Cueros crudos",             "Química/Cuero"),
    (42, "Articles of leather; Saddlery and harness; Travel goods, handbags and similar containers; Articles of animal gut (other than silkworm gut)", "Manufacturas de cuero", "Química/Cuero"),
    (43, "Furskins and artificial fur; Manufactures thereof",                                            "Pieles",                    "Química/Cuero"),
    (44, "Wood and articles of wood; Wood charcoal",                                                     "Madera",                    "Madera/Papel"),
    (45, "Cork and articles of cork",                                                                    "Corcho",                    "Madera/Papel"),
    (46, "Manufactures of straw, of esparto or of other plaiting materials; Basketware and wickerwork",  "Cestería",                  "Madera/Papel"),
    (47, "Pulp of wood or of other fibrous cellulosic material; Waste and scrap of paper or paperboard", "Pasta de papel",            "Madera/Papel"),
    (48, "Paper and paperboard; Articles of paper pulp, of paper or of paperboard",                      "Papel y cartón",            "Madera/Papel"),
    (49, "Printed books, newspapers, pictures and other products of the printing industry; Manuscripts, typescripts and plans", "Libros e impresos", "Madera/Papel"),
    (50, "Silk",                                                                                         "Seda",                      "Textil/Calzado"),
    (51, "Wool, fine or coarse animal hair; Horsehair yarn and woven fabric",                            "Lana",                      "Textil/Calzado"),
    (52, "Cotton",                                                                                       "Algodón",                   "Textil/Calzado"),
    (53, "Other vegetable textile fibers; Paper yarn and woven fabrics of paper yarn",                   "Otras fibras vegetales",    "Textil/Calzado"),
    (54, "Man-made filaments",                                                                           "Filamentos sintéticos",     "Textil/Calzado"),
    (55, "Man-made staple fibers",                                                                       "Fibras sintéticas",         "Textil/Calzado"),
    (56, "Wadding, felt and nonwovens; Special yarns; Twine, cordage, ropes and cables and articles thereof", "Cordelería",           "Textil/Calzado"),
    (57, "Carpets and other textile floor coverings",                                                    "Alfombras",                 "Textil/Calzado"),
    (58, "Special woven fabrics; Tuffed textile fabrics; Lace; Tapestries; Trimmings; Embroidery",       "Tejidos especiales",        "Textil/Calzado"),
    (59, "Impregnated, coated, covered or laminated textile fabrics; Textile articles of a kind suitable for industrial use", "Tejidos impregnados", "Textil/Calzado"),
    (60, "Knitted or crocheted fabrics",                                                                 "Tejidos de punto",          "Textil/Calzado"),
    (61, "Articles of apparel and clothing accessories, knitted or crocheted",                           "Prendas de punto",          "Textil/Calzado"),
    (62, "Articles of apparel and clothing accessories, not knitted or crocheted",                       "Prendas no de punto",       "Textil/Calzado"),
    (63, "Other made-up textile articles; Needle craft sets; Worn clothing and worn textile articles; rags", "Artículos textiles",    "Textil/Calzado"),
    (64, "Footwear, gaiters and the like; Parts of such articles",                                       "Calzado",                   "Textil/Calzado"),
    (65, "Headgear and parts thereof",                                                                   "Sombrerería",               "Textil/Calzado"),
    (66, "Umbrellas, sun umbrellas, walking sticks, seatsticks, whips, riding crops and parts thereof",  "Paraguas",                  "Textil/Calzado"),
    (67, "Prepared feathers and down and articles made of feathers or of down; artificial flowers; articles of human hair", "Plumas y flores", "Textil/Calzado"),
    (68, "Articles of stone, plaster, cement, asbestos, mica or similar materials",                      "Piedra y cemento",          "Construcción"),
    (69, "Ceramic products",                                                                             "Cerámica",                  "Construcción"),
    (70, "Glass and glassware",                                                                          "Vidrio",                    "Construcción"),
    (71, "Natural or cultured pearls, precious or semiprevious stones, precious metals; metals clad with precious metal, and articles thereof; imitation jewelry; coin", "Metales preciosos", "Energía/Minerales"),
    (72, "Iron and steel",                                                                               "Hierro y acero",            "Metales"),
    (73, "Articles of iron or steel",                                                                    "Artículos de acero",        "Metales"),
    (74, "Copper and articles thereof",                                                                  "Cobre",                     "Metales"),
    (75, "Nickel and articles thereof",                                                                  "Níquel",                    "Metales"),
    (76, "Aluminum and articles thereof",                                                                "Aluminio",                  "Metales"),
    (78, "Lead and articles thereof",                                                                    "Plomo",                     "Metales"),
    (79, "Zinc and articles thereof",                                                                    "Zinc",                      "Metales"),
    (80, "Tin and articles thereof",                                                                     "Estaño",                    "Metales"),
    (81, "Other base metals; Cermets; Articles thereof",                                                 "Otros metales base",        "Metales"),
    (82, "Tools, implements, cutlery, spoons and forks, of base metal; Parts thereof of base metal",     "Herramientas",              "Metales"),
    (83, "Miscellaneous articles of base metal",                                                         "Manufacturas metálicas",    "Metales"),
    (84, "Nuclear reactors, boilers, machinery and mechanical appliances; parts thereof",                "Maquinaria mecánica",       "Electrónica/Maquinaria"),
    (85, "Electrical machinery and equipment and parts thereof; Sound recorders and reproducers, television image and sound recorders and reproducers, and parts and accessories of such articles", "Maquinaria eléctrica", "Electrónica/Maquinaria"),
    (86, "Railway or tramway locomotives, rolling stock and parts thereof; railway or tramway track fixtures and fittings and parts thereof; Mechanical (including electromechanical) traffic signaling equipment of all kinds", "Material ferroviario", "Automotriz"),
    (87, "Vehicles, other than railway or tramway rolling stock, and parts and accessories thereof",     "Vehículos",                 "Automotriz"),
    (88, "Aircraft, spacecraft, and parts thereof",                                                      "Aeronaves",                 "Otros"),
    (89, "Ships, boats, and floating structures",                                                        "Embarcaciones",             "Otros"),
    (90, "Optical, photographic, cinematographic, measuring, checking, precision, medical or surgical instruments and apparatus; Parts and accessories thereof", "Instrumentos médicos", "Médico/Precisión"),
    (91, "Clocks and watches and parts thereof",                                                         "Relojería",                 "Otros"),
    (92, "Musical instruments; Parts and accessories of such articles",                                  "Instrumentos musicales",    "Otros"),
    (93, "Arms and ammunition; Parts and accessories thereof",                                           "Armas y municiones",        "Otros"),
    (94, "Furniture; Bedding, mattress supports, cushions and similar stuffed furnishings; Lamps and lighting fittings, not elsewhere specified or included; Illuminated signs, illuminated nameplates and the like; Prefabricated buildings", "Muebles e iluminación", "Manufacturas varias"),
    (95, "Toys, games and sports equipment; Parts and accessories thereof",                              "Juguetes y deportes",       "Manufacturas varias"),
    (96, "Miscellaneous manufactured articles",                                                          "Manufacturas varias",       "Manufacturas varias"),
    (97, "Works of art, collectors' pieces and antiques",                                                "Obras de arte",             "Otros"),
    (98, "Special classification provisions",                                                            "Clasificaciones especiales", "Otros"),
]


# =====================================================================
# Helpers
# =====================================================================

def normalize(text: str) -> str:
    """Lowercase + non-alphanumeric → espacio + colapsa whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def conn():
    c = psycopg2.connect(**PG)
    c.autocommit = False
    return c


# =====================================================================
# Loaders
# =====================================================================

def load_dim_date(c) -> int:
    """1996-01-01 → CURRENT_DATE, vía generate_series. SQL puro."""
    with c.cursor() as cur:
        cur.execute("TRUNCATE frontera.dim_date RESTART IDENTITY CASCADE;")
        cur.execute("SET lc_time TO 'C';")  # nombres en inglés, locale-independent
        cur.execute("""
            INSERT INTO frontera.dim_date
                (date_id, full_date, year, month, month_name, day,
                 day_of_week, day_of_week_name, week_of_year, year_month)
            SELECT
                EXTRACT(YEAR  FROM d)::int * 10000
                + EXTRACT(MONTH FROM d)::int * 100
                + EXTRACT(DAY   FROM d)::int                AS date_id,
                d::date,
                EXTRACT(YEAR FROM d)::smallint,
                EXTRACT(MONTH FROM d)::smallint,
                TO_CHAR(d, 'FMMonth'),
                EXTRACT(DAY FROM d)::smallint,
                EXTRACT(ISODOW FROM d)::smallint,
                TO_CHAR(d, 'FMDay'),
                EXTRACT(WEEK FROM d)::smallint,
                TO_CHAR(d, 'YYYY-MM')
            FROM generate_series('1996-01-01'::date, CURRENT_DATE, '1 day'::interval) d
            ON CONFLICT DO NOTHING;
        """)
        cur.execute("SELECT COUNT(*) FROM frontera.dim_date;")
        n = cur.fetchone()[0]
    log(f"  dim_date: {n} filas insertadas (1996-01-01 → CURRENT_DATE)")
    return n


def load_dim_port(c) -> int:
    """Carga 27 puertos US-Mexico desde Border Crossing CSV (excluye CBX)."""
    bc = pd.read_csv(ROOT / "data_samples" / "border_crossing_full.csv")
    bc["Date_p"] = pd.to_datetime(bc["Date"], errors="coerce")

    mx = bc[bc["Border"] == "US-Mexico Border"]
    mx = mx[mx["Port Name"] != "Cross Border Xpress"]   # excluir CBX

    # Para cada puerto, lat/lon de la fila más reciente
    most_recent_idx = mx.groupby("Port Name")["Date_p"].idxmax()
    rows_for_port = mx.loc[most_recent_idx]

    rows = []
    for _, r in rows_for_port.iterrows():
        name = r["Port Name"]
        rows.append((
            name,                                            # canonical (BC ya está canónico)
            int(r["Port Code"]),
            "Mexico",
            "US-Mexico",
            r["State"],
            float(r["Latitude"]) if pd.notna(r["Latitude"]) else None,
            float(r["Longitude"]) if pd.notna(r["Longitude"]) else None,
            name in CORRIDOR_PORTS,
        ))
    rows.sort(key=lambda x: x[0])

    with c.cursor() as cur:
        cur.execute("TRUNCATE frontera.dim_port RESTART IDENTITY CASCADE;")
        cur.executemany("""
            INSERT INTO frontera.dim_port
                (port_canonical_name, port_code, country, border, state,
                 latitude, longitude, is_in_corridor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s);
        """, rows)
    log(f"  dim_port: {len(rows)} puertos insertados (US-Mexico, sin CBX)")

    # Detectar huérfanos: port_codes en TransBorder pero no en BC US-Mexico
    bc_codes = set(int(x) for x in mx["Port Code"].unique())
    tb_path = ROOT / "data_samples" / "transborder_commodities_distinct.json"
    # tb_ports.json fue generado fuera del script; lo regenero acá si falta
    tb_ports_path = ROOT / "data_samples" / "tb_distinct_ports.json"
    if not tb_ports_path.exists():
        import urllib.request
        url = ("https://opendata.sandag.org/resource/k3a4-5ygm.json?"
               "$select=port_code,port_name,count(*)&$group=port_code,port_name&$order=port_code")
        with urllib.request.urlopen(url) as resp:
            tb_ports_path.write_bytes(resp.read())
    tb_ports = json.loads(tb_ports_path.read_text())
    tb_codes = {int(r["port_code"]): (r.get("port_name"), int(r["count"])) for r in tb_ports}
    orphans = [(code, info) for code, info in tb_codes.items() if code not in bc_codes]
    if orphans:
        log("  ⚠ Huérfanos detectados en TransBorder (port_code sin match en BC US-Mexico):")
        for code, (name, cnt) in orphans:
            log(f"    port_code={code}  port_name={name!r}  filas_TB={cnt}")
    else:
        log("  Sin huérfanos en TransBorder.")
    return len(rows)


def load_dim_mode(c) -> int:
    with c.cursor() as cur:
        cur.execute("TRUNCATE frontera.dim_mode RESTART IDENTITY CASCADE;")
        cur.executemany("""
            INSERT INTO frontera.dim_mode (mode_canonical_name, source_dataset)
            VALUES (%s,%s);
        """, CANONICAL_MODES)
    log(f"  dim_mode: {len(CANONICAL_MODES)} modos canónicos insertados")
    log("  Mapping BC → canonical:")
    for src, dst in BC_MODE_TO_CANONICAL.items():
        log(f"    BC '{src}' → '{dst}'")
    log("  Mapping TB → canonical:")
    for src, dst in TB_MODE_TO_CANONICAL.items():
        log(f"    TB '{src}' → '{dst}'")
    return len(CANONICAL_MODES)


def load_dim_commodity_hs2(c) -> tuple[int, list, dict]:
    """Carga 97 capítulos HS-2. Verifica match contra descripciones reales del dataset."""
    # Leer descripciones reales
    distinct_path = ROOT / "data_samples" / "transborder_commodities_distinct.json"
    raw = json.loads(distinct_path.read_text())
    dataset_descs = [r["commodity"] for r in raw]

    # Indexar dataset por normalize → variantes
    dataset_by_norm = defaultdict(list)
    for d in dataset_descs:
        dataset_by_norm[normalize(d)].append(d)

    # Indexar catálogo por normalize → hs2_code
    catalog_by_norm = {}
    for hs2_code, full, _, _ in HS2_CATALOG:
        catalog_by_norm[normalize(full)] = hs2_code

    # Verificar matches: cada norm del dataset debe encontrarse en catálogo
    unmatched = []
    matched_codes = set()
    for norm_key, variants in dataset_by_norm.items():
        if norm_key in catalog_by_norm:
            matched_codes.add(catalog_by_norm[norm_key])
        else:
            unmatched.append((norm_key, variants))

    # Insertar catálogo (los 97 entries, independiente de match con dataset)
    rows = [(code, full, short, sector) for code, full, short, sector in HS2_CATALOG]
    with c.cursor() as cur:
        cur.execute("TRUNCATE frontera.dim_commodity_hs2 RESTART IDENTITY CASCADE;")
        cur.executemany("""
            INSERT INTO frontera.dim_commodity_hs2
                (hs2_code, hs2_description_full, hs2_description_short, sector_grouping)
            VALUES (%s,%s,%s,%s);
        """, rows)
    log(f"  dim_commodity_hs2: {len(rows)} capítulos insertados")
    log(f"  Capítulos del catálogo matcheados con dataset: {len(matched_codes)}/{len(HS2_CATALOG)}")
    if unmatched:
        log(f"  ⚠ {len(unmatched)} descripciones del dataset SIN match en catálogo:")
        for norm, variants in unmatched:
            log(f"    norm={norm[:90]!r}  variants={variants}")
    not_in_dataset = set(c[0] for c in HS2_CATALOG) - matched_codes
    if not_in_dataset:
        log(f"  ℹ Capítulos del catálogo sin presencia en dataset (esperable para HS-99): {sorted(not_in_dataset)}")
    return len(rows), unmatched, dataset_by_norm


def load_dim_lane_type(c) -> int:
    with c.cursor() as cur:
        cur.execute("TRUNCATE frontera.dim_lane_type RESTART IDENTITY CASCADE;")
        cur.executemany("""
            INSERT INTO frontera.dim_lane_type
                (lane_canonical_name, vehicle_category, is_trusted_traveler)
            VALUES (%s,%s,%s);
        """, LANE_TYPES)
    log(f"  dim_lane_type: {len(LANE_TYPES)} carriles insertados")
    log("  Mapping SANDAG → canonical:")
    for src, dst in WAIT_LANE_NAME_TO_CANONICAL.items():
        log(f"    SANDAG '{src}' → '{dst}'")
    return len(LANE_TYPES)


# =====================================================================
# Validaciones obligatorias
# =====================================================================

VALIDATIONS = [
    ("1. Puertos en corridor", "SELECT COUNT(*) FROM frontera.dim_port WHERE is_in_corridor = TRUE;", lambda v: v == 3),
    ("2. Capítulos HS-2",       "SELECT COUNT(*) FROM frontera.dim_commodity_hs2;",                   lambda v: 95 <= v <= 99),
    ("3. Filas dim_date",       "SELECT COUNT(*) FROM frontera.dim_date;",                            lambda v: 10000 < v < 12000),
    ("4. Modos canónicos",      "SELECT COUNT(*) FROM frontera.dim_mode;",                            lambda v: v == 14),
    ("5. Tipos de carril",      "SELECT COUNT(*) FROM frontera.dim_lane_type;",                       lambda v: v == 6),
]

def run_validations(c):
    log("\n" + "=" * 60)
    log("VALIDACIONES OBLIGATORIAS")
    log("=" * 60)
    all_ok = True
    with c.cursor() as cur:
        for label, sql, ok_fn in VALIDATIONS:
            cur.execute(sql)
            v = cur.fetchone()[0]
            ok = ok_fn(v)
            mark = "OK" if ok else "FAIL"
            log(f"  [{mark}] {label}: {v}")
            all_ok = all_ok and ok
        # Validación 6: distribución por sector
        cur.execute("""
            SELECT sector_grouping, COUNT(*) AS n
            FROM frontera.dim_commodity_hs2
            GROUP BY sector_grouping
            ORDER BY n DESC;
        """)
        log("  [6] Distribución por sector_grouping:")
        for sector, n in cur.fetchall():
            log(f"      {sector:25s} {n}")
    return all_ok


# =====================================================================
# Main
# =====================================================================

def main():
    log(f"Conectando a {PG['host']}:{PG['port']}/{PG['dbname']} como {PG['user']}")
    with conn() as c:
        log("\n--- Cargando dimensiones ---")
        load_dim_date(c)
        load_dim_port(c)
        load_dim_mode(c)
        n_hs2, unmatched, dataset_by_norm = load_dim_commodity_hs2(c)
        load_dim_lane_type(c)
        c.commit()
        log("\nCommit OK.")

        ok = run_validations(c)
    log("\n" + ("✔ Todas las validaciones pasaron." if ok else "✖ Falló al menos una validación."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
