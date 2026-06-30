"""
Service de geolocalização de empresas via OpenStreetMap.

Estratégia em 2 passos:
  1) Nominatim resolve nome do município → coordenadas (centro + bounding box)
  2) Overpass API consulta estabelecimentos de tags relevantes dentro do bbox

OSM é uma base colaborativa. Cobertura varia muito por região: grandes capitais
costumam ter dezenas/centenas de estabelecimentos mapeados; cidades pequenas
podem ter quase nada. Por isso o response inclui o total e a fonte, e o
frontend exibe um aviso de cobertura limitada.
"""
import logging
from typing import Optional

from app.core.cache import cached
from app.core.http import fetch_json

log = logging.getLogger(__name__)


# ============================================================================
# Mapeamento CNAE classe → tags OSM
# ============================================================================
# Cada CNAE classe (4 dígitos) mapeia pra um ou mais filtros do OSM no formato
# "chave"="valor". As consultas Overpass combinam todos os filtros do CNAE.
#
# Referências:
#   https://wiki.openstreetmap.org/wiki/Map_features
#   https://wiki.openstreetmap.org/wiki/Key:amenity
#   https://wiki.openstreetmap.org/wiki/Key:shop

CNAE_TO_OSM_TAGS: dict[str, list[str]] = {
    # --- ALIMENTAÇÃO ---
    # Restaurante/Cafeteria/Lanchonete/Pizzaria/Sorveteria — todos sob 5611
    "5611": [
        '"amenity"="restaurant"',
        '"amenity"="cafe"',
        '"amenity"="fast_food"',
        '"amenity"="bar"',
        '"amenity"="ice_cream"',
    ],
    "4721": ['"shop"="bakery"', '"shop"="confectionery"'],
    "5620": ['"amenity"="events_venue"', '"amenity"="conference_centre"'],
    "4712": ['"shop"="convenience"', '"shop"="supermarket"', '"shop"="grocery"'],

    # --- BELEZA E ESTÉTICA ---
    "9602": ['"shop"="hairdresser"', '"shop"="beauty"'],
    "9609": ['"leisure"="spa"', '"shop"="massage"'],
    "4772": ['"shop"="cosmetics"', '"shop"="perfumery"'],

    # --- SAÚDE ---
    "8630": ['"amenity"="dentist"'],
    "4771": ['"amenity"="pharmacy"'],
    "4774": ['"shop"="optician"'],

    # --- EDUCAÇÃO ---
    "8593": ['"amenity"="language_school"'],
    "8599": ['"amenity"="school"', '"amenity"="college"'],
    "8591": ['"leisure"="sports_centre"', '"amenity"="dancing_school"'],

    # --- FITNESS E BEM-ESTAR ---
    "9313": ['"leisure"="fitness_centre"', '"leisure"="sports_centre"'],

    # --- PETS ---
    "4789": ['"shop"="pet"', '"shop"="florist"', '"shop"="garden_centre"'],
    "7500": ['"amenity"="veterinary"'],

    # --- VAREJO ---
    "4781": ['"shop"="clothes"', '"shop"="boutique"'],
    "4782": ['"shop"="shoes"'],
    "4761": ['"shop"="stationery"', '"shop"="books"'],

    # --- SERVIÇOS ---
    "9601": ['"shop"="laundry"', '"shop"="dry_cleaning"'],
    "4520": ['"shop"="car_repair"', '"craft"="tyres"'],
    "6920": ['"office"="accountant"', '"office"="tax_advisor"'],
    "9521": ['"shop"="mobile_phone"', '"shop"="electronics"'],

    # --- HOSPEDAGEM ---
    "5510": ['"tourism"="hotel"', '"tourism"="guest_house"', '"tourism"="hostel"'],
}


# ============================================================================
# Resolução de coordenadas via Nominatim
# ============================================================================
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# User-Agent obrigatório por política do Nominatim
OSM_HEADERS = {
    "User-Agent": "Terreno-MVP/0.1 (https://github.com/jprogram2026-dev/terreno)"
}


@cached("osm:geo")
async def geocode_municipio(muni_nome: str, uf_sigla: str) -> Optional[dict]:
    """
    Resolve nome do município → coordenadas (centro + bounding box) via Nominatim.
    Resultado é cacheado porque coordenadas geográficas são estáticas.
    """
    try:
        url = f"{NOMINATIM_URL}?q={muni_nome},{uf_sigla},Brasil&format=json&limit=1"
        data = await fetch_json(url, headers=OSM_HEADERS, timeout=15)
        if not data:
            log.info("OSM: município '%s/%s' não encontrado no Nominatim", muni_nome, uf_sigla)
            return None
        first = data[0]
        # bounding box do Nominatim: [south, north, west, east]
        bbox = first.get("boundingbox", [])
        if len(bbox) != 4:
            return None
        return {
            "center": [float(first["lat"]), float(first["lon"])],
            "bbox": [float(bbox[0]), float(bbox[2]), float(bbox[1]), float(bbox[3])],  # [s, w, n, e]
            "display_name": first.get("display_name"),
        }
    except Exception as e:
        log.warning("Falha no Nominatim para %s/%s: %s", muni_nome, uf_sigla, e)
        return None


# ============================================================================
# Consulta Overpass — busca estabelecimentos
# ============================================================================
@cached("osm:places")
async def fetch_places(muni_nome: str, uf_sigla: str, cnae_classe: str) -> dict:
    """
    Retorna estabelecimentos do CNAE no município, via OpenStreetMap.

    Response shape:
        {
          "places":  [{"lat": -15.6, "lon": -56.1, "nome": "X"}, ...],
          "center":  [-15.6, -56.1],         // pra centralizar mapa
          "bbox":    [s, w, n, e],           // bounds do município
          "total":   42,
          "coverage_note": "..."             // string opcional explicativa
        }
    """
    geo = await geocode_municipio(muni_nome, uf_sigla)
    if not geo:
        return {"places": [], "center": None, "bbox": None, "total": 0,
                "coverage_note": "Município não localizado no OpenStreetMap"}

    tags = CNAE_TO_OSM_TAGS.get(cnae_classe, [])
    if not tags:
        return {"places": [], "center": geo["center"], "bbox": geo["bbox"], "total": 0,
                "coverage_note": "Setor sem mapeamento OSM definido"}

    # Monta query Overpass. Usa node + way (alguns estabelecimentos são polígonos)
    # `out center` retorna o centroide pra ways/relations.
    s, w, n, e = geo["bbox"]
    parts = []
    for tag in tags:
        parts.append(f'node[{tag}]({s},{w},{n},{e});')
        parts.append(f'way[{tag}]({s},{w},{n},{e});')
    query = f"""
[out:json][timeout:25];
({"".join(parts)});
out center 500;
"""

    try:
        data = await fetch_json(
            OVERPASS_URL,
            method="POST",
            data={"data": query},
            headers=OSM_HEADERS,
            timeout=30,
        )
    except Exception as ex:
        log.warning("Falha no Overpass para %s/%s/%s: %s", muni_nome, uf_sigla, cnae_classe, ex)
        return {"places": [], "center": geo["center"], "bbox": geo["bbox"], "total": 0,
                "coverage_note": "Falha ao consultar OpenStreetMap"}

    places = []
    for el in data.get("elements", []):
        # node tem lat/lon direto; way/relation tem center.lat/lon
        if "lat" in el and "lon" in el:
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        nome = (el.get("tags") or {}).get("name")
        places.append({"lat": lat, "lon": lon, "nome": nome})

    return {
        "places": places,
        "center": geo["center"],
        "bbox": geo["bbox"],
        "total": len(places),
        "coverage_note": None,
    }
