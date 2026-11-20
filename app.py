"""London Local: three small London helpers in one app.

    london.outfit   what to wear for the weather, drawn as an emoji avatar
    london.jog      a jogging route out to a park and back, for a distance
    london.journey  how to get from A to B, and which strikes or delays hit it
    london.find_spot    find a saved (or described) spot, and how far it is
    london.on_this_day  London history on this date, a place's story, saved spots

Saved spots live in spots.json beside this file: place facts only (name,
type, area, public address, coordinates, a few tags, the date posted).

Data sources, all free and keyless:
    Open-Meteo        weather forecast
    Nominatim (OSM)   turning a place name into coordinates
    TfL Unified API   journey planner and live line status
    Overpass (OSM)    places of a type near an area, for described spots
    Wikipedia         "on this day" events, and a short history of a place

Every request goes through `_fetch`, so the tests can replace it.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
SPOTS_PATH = Path(__file__).with_name("spots.json")
LONDON = ZoneInfo("Europe/London")

#: Nominatim answers 403 without a descriptive User-Agent.
USER_AGENT = "pippa-agent-app-london-local/0.1 (https://github.com/Pippa-Labs)"

#: Greater London, as Nominatim's viewbox (left, top, right, bottom).
LONDON_VIEWBOX = "-0.56,51.72,0.34,51.27"
CENTRAL_LONDON = (51.5074, -0.1278)

#: Streets are not straight lines: a walking route is roughly this much longer
#: than the distance as the crow flies.
STREET_FACTOR = 1.3


# ── Errors a person may see ─────────────────────────────────────────────────


class PlaceNotFound(AgentAppError):
    user_message = "Sorry mate, I couldn't find that place in London."
    retryable = False


class CannotReadDay(AgentAppError):
    user_message = "Ah, I can only see 7 days ahead, mate. Try a day name like Saturday, or tomorrow."
    retryable = False


class MissingPlace(AgentAppError):
    user_message = "Where are you off from, and where to, mate? Something like: from Imperial College to Ealing Broadway."
    retryable = False


class BadDistance(AgentAppError):
    user_message = "Let's keep it between 1 and 42 km, mate (42 is a whole marathon!). Try: a 5 km jog from Imperial College."
    retryable = False


class NoJourney(AgentAppError):
    user_message = "Sorry mate, TfL can't find a public transport route between those two."
    retryable = False


class SpotNotFound(AgentAppError):
    user_message = ("I can't open Instagram links, mate. Tell me the place name, or "
                    "describe it: what it is and roughly where.")
    retryable = False


class ServiceBusy(RateLimitedError):
    pass


class ServiceDown(ServiceUnavailableError):
    pass


# ── Parks for the jogging routes ────────────────────────────────────────────
# Centre point, an approximate length of one lap round its paths in km, and
# something worth running past. Lap lengths are rounded estimates.

PARKS = [
    ("Hyde Park", 51.5073, -0.1657, 5.0, "the Serpentine"),
    ("Kensington Gardens", 51.5067, -0.1795, 4.0, "the Round Pond and Kensington Palace"),
    ("Holland Park", 51.5030, -0.2040, 2.0, "the Kyoto Garden"),
    ("Green Park", 51.5040, -0.1425, 2.0, "Buckingham Palace"),
    ("St James's Park", 51.5025, -0.1340, 2.3, "the lake and Horse Guards Parade"),
    ("Regent's Park", 51.5313, -0.1570, 4.5, "the Outer Circle and the boating lake"),
    ("Primrose Hill", 51.5395, -0.1610, 1.5, "the view over the city from the top"),
    ("Battersea Park", 51.4791, -0.1566, 4.0, "the river and the Peace Pagoda"),
    ("Clapham Common", 51.4596, -0.1497, 4.5, "the ponds and the bandstand"),
    ("Wandsworth Common", 51.4530, -0.1700, 3.5, "the lakes"),
    ("Brockwell Park", 51.4500, -0.1060, 2.5, "the hilltop view and the lido"),
    ("Burgess Park", 51.4830, -0.0870, 3.0, "the lake"),
    ("Southwark Park", 51.4950, -0.0550, 2.5, "the boating lake"),
    ("Greenwich Park", 51.4769, -0.0005, 3.5, "the Royal Observatory hill"),
    ("Victoria Park", 51.5362, -0.0400, 4.5, "the lakes and the Chinese pagoda"),
    ("Mile End Park", 51.5190, -0.0330, 3.0, "the Green Bridge and the canal"),
    ("Clissold Park", 51.5610, -0.0880, 2.0, "the deer enclosure"),
    ("Finsbury Park", 51.5700, -0.1030, 3.0, "the boating lake"),
    ("Hampstead Heath", 51.5600, -0.1630, 5.0, "Parliament Hill"),
    ("Alexandra Park", 51.5940, -0.1300, 3.0, "the view from Alexandra Palace"),
    ("Ravenscourt Park", 51.4950, -0.2370, 1.6, "the pond"),
    ("Gunnersbury Park", 51.4960, -0.2900, 3.0, "the boating lake"),
    ("Richmond Park", 51.4425, -0.2760, 11.0, "the deer and the Tamsin Trail"),
    ("Wimbledon Common", 51.4340, -0.2350, 6.0, "the windmill"),
]


# ── Weather words ───────────────────────────────────────────────────────────

WEATHER_CODES = {
    0: ("☀️", "clear sky"), 1: ("🌤️", "mostly clear"), 2: ("⛅", "partly cloudy"),
    3: ("☁️", "overcast"), 45: ("🌫️", "fog"), 48: ("🌫️", "freezing fog"),
    51: ("🌦️", "light drizzle"), 53: ("🌦️", "drizzle"), 55: ("🌧️", "heavy drizzle"),
    56: ("🌧️", "freezing drizzle"), 57: ("🌧️", "freezing drizzle"),
    61: ("🌧️", "light rain"), 63: ("🌧️", "rain"), 65: ("🌧️", "heavy rain"),
    66: ("🌧️", "freezing rain"), 67: ("🌧️", "freezing rain"),
    71: ("🌨️", "light snow"), 73: ("🌨️", "snow"), 75: ("❄️", "heavy snow"),
    77: ("🌨️", "snow grains"), 80: ("🌦️", "light showers"), 81: ("🌧️", "showers"),
    82: ("⛈️", "heavy showers"), 85: ("🌨️", "snow showers"), 86: ("❄️", "heavy snow showers"),
    95: ("⛈️", "thunderstorms"), 96: ("⛈️", "thunderstorms with hail"),
    99: ("⛈️", "thunderstorms with hail"),
}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
THUNDER_CODES = {95, 96, 99}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance in km (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _pretty_day(day: date) -> str:
    # Built by hand: `%-d` raises on Windows.
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


# ── 4 & 5. Saved spots ──────────────────────────────────────────────────────

LINK = re.compile(r"(https?://\S+|www\.\S+|\S*instagr(?:am\.com|\.am)\S*)", re.I)
SPOT_STOPWORDS = {
    "the", "a", "an", "that", "this", "place", "spot", "with", "and", "of", "in",
    "near", "at", "by", "around", "to", "for", "on", "i", "saw", "where", "is", "was",
    "one", "some", "my", "it", "there", "next", "close",
}
AREA = re.compile(r"\b(?:near|in|around|by|at|next to|close to)\s+(.+)$", re.I)
# Words in a description -> (word, OSM key, OSM value, what to call it).
PLACE_TYPES = [
    ("rooftop", "amenity", "bar", "rooftop bar"),
    ("ramen", "cuisine", "ramen", "ramen restaurant"),
    ("sushi", "cuisine", "sushi", "sushi restaurant"),
    ("pizza", "cuisine", "pizza", "pizza place"),
    ("dim sum", "cuisine", "dim_sum", "dim sum restaurant"),
    ("coffee", "amenity", "cafe", "cafe"),
    ("cafe", "amenity", "cafe", "cafe"),
    ("brunch", "amenity", "cafe", "cafe"),
    ("bakery", "shop", "bakery", "bakery"),
    ("pub", "amenity", "pub", "pub"),
    ("bar", "amenity", "bar", "bar"),
    ("restaurant", "amenity", "restaurant", "restaurant"),
    ("museum", "tourism", "museum", "museum"),
    ("gallery", "tourism", "gallery", "gallery"),
    ("viewpoint", "tourism", "viewpoint", "viewpoint"),
    ("view", "tourism", "viewpoint", "viewpoint"),
    ("market", "amenity", "marketplace", "market"),
    ("garden", "leisure", "garden", "garden"),
    ("park", "leisure", "park", "park"),
    ("bookshop", "shop", "books", "bookshop"),
    ("bubble tea", "cuisine", "bubble_tea", "bubble tea shop"),
    ("ice cream", "amenity", "ice_cream", "ice cream shop"),
    ("dessert", "amenity", "ice_cream", "dessert shop"),
    ("noodle", "cuisine", "noodle", "noodle bar"),
    ("dumpling", "cuisine", "dumplings", "dumpling restaurant"),
    ("burger", "cuisine", "burger", "burger place"),
    ("korean", "cuisine", "korean", "Korean restaurant"),
    ("japanese", "cuisine", "japanese", "Japanese restaurant"),
    ("chinese", "cuisine", "chinese", "Chinese restaurant"),
    ("thai", "cuisine", "thai", "Thai restaurant"),
    ("indian", "cuisine", "indian", "Indian restaurant"),
    ("italian", "cuisine", "italian", "Italian restaurant"),
    ("cinema", "amenity", "cinema", "cinema"),
    ("theatre", "amenity", "theatre", "theatre"),
    ("church", "amenity", "place_of_worship", "church"),
    ("library", "amenity", "library", "library"),
    ("hotel", "tourism", "hotel", "hotel"),
]
# The first match wins, so specific kinds ("noodle bar") go before generic ones ("bar").
GENERIC_TYPES = {"bar", "pub", "restaurant", "cafe", "coffee", "view", "park", "garden", "market"}
PLACE_TYPES.sort(key=lambda t: t[0] in GENERIC_TYPES)
UK_WORDS = ("london", "uk", "england", "scotland", "wales", "britain", "edinburgh",
            "manchester", "bristol", "oxford", "cambridge", "brighton", "bath", "york")
# A Wikipedia hit only counts as a place if its short description says so.
PLACE_WORDS = ("london", "england", "scotland", "wales", "building", "restaurant", "park",
               "garden", "museum", "street", "market", "bar", "pub", "station", "church",
               "cathedral", "bridge", "tower", "gallery", "theatre", "landmark", "venue",
               "hotel", "palace", "castle", "square", "district", "skyscraper", "shop")
WALK_KMH = 5.0
SPOT_BUDGET_S = 13.0  # stop adding extras after this, inside the 20 s limit
LONDON_WORDS = ("london", "westminster", "thames", "buckingham palace", "tower bridge")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in SPOT_STOPWORDS}


def _maps_link(destination: str, origin: str = "") -> str:
    params = {"api": "1", "destination": destination, "travelmode": "transit"}
    if origin:
        params["origin"] = origin
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def _spot_line(spot: dict) -> str:
    bits = ", ".join(b for b in (spot.get("type"), spot.get("area")) if b)
    return f"{spot['name']} ({bits})" if bits else spot["name"]


def _latlon(place: dict) -> str:
    return f"{place['lat']:.5f},{place['lon']:.5f}"


class SpotsMixin:
    """find_spot and on_this_day. Mixed into App below."""

    def _find_spot(self, call: Call) -> Result:
        started = self._clock()
        spot_text = LINK.sub(" ", call.inputs.get("spot") or "").strip()
        description = LINK.sub(" ", call.inputs.get("description") or "").strip()
        start = LINK.sub(" ", call.inputs.get("start") or "").strip()
        self._nominatim_calls = 0

        others: list[str] = []
        saved = self._saved_match(spot_text, description)
        if saved:
            found, source = saved[0], "saved"
            others = [_spot_line(s) for s in saved[1:4]]
        elif spot_text or description:
            try:
                if not spot_text:
                    raise PlaceNotFound("no name given")
                found, source = self._geocode(spot_text, 4.0), "lookup"
            except PlaceNotFound:
                # Not on the map by that name: treat everything they said as a description.
                described = f"{spot_text} {description}".strip()
                candidates = self._search_described(described, started)
                found = candidates[0]
                source = found.pop("_source", "description")
                others = [_spot_line(c) for c in candidates[1:3]]
        else:
            raise SpotNotFound("no usable spot name or description")

        has_coords = found.get("lat") is not None and found.get("lon") is not None
        data = {
            "name": found["name"],
            "type": found.get("type") or "",
            "area": found.get("area") or "",
            "address": found.get("address") or "",
            "source": source,
            "other_matches": others,
        }

        origin = None
        if start and has_coords and self._clock() - started < SPOT_BUDGET_S:
            try:
                origin = self._geocode(start, 3.0)
            except AgentAppError:
                origin = None  # the spot is still worth returning
        if origin:
            km = _km_between((origin["lat"], origin["lon"]), (found["lat"], found["lon"]))
            data["start_resolved"] = origin["name"]
            data["distance_km"] = round(km, 1)
            data["walk_minutes"] = max(1, round(km * STREET_FACTOR / WALK_KMH * 60))
            if self._clock() - started < SPOT_BUDGET_S:
                minutes = self._transit_minutes(origin, found, call.context.now)
                if minutes is not None:
                    data["transit_minutes"] = minutes

        destination = (_latlon(found) if has_coords
                       else f"{found['name']} {data['address']}".strip())
        data["directions_url"] = _maps_link(destination, _latlon(origin) if origin else "")
        if source == "maps_search":
            data["directions_url"] = ("https://www.google.com/maps/search/?"
                                      + urlencode({"api": "1", "query": found["name"]}))
            return Result(data=data, narrative=(
                f"🔎 Couldn't pin \"{found['name']}\" down myself, mate, but this Google Maps "
                f"search should find it: {data['directions_url']}"))

        lead = "🤔 Best guess, mate:" if found.pop("_guess", False) else "📍 Found it, mate!"
        narrative = f"{lead} {_spot_line(found)}"
        if data["address"]:
            narrative += f", {data['address']}"
        if "distance_km" in data:
            narrative += f". It's about {data['distance_km']} km from {origin['name']}"
            if "transit_minutes" in data:
                narrative += f", {data['transit_minutes']} min on public transport"
            narrative += f", or about a {data['walk_minutes']} min walk"
        return Result(data=data, narrative=narrative + ".")

    def _saved_match(self, spot_text: str, description: str) -> list[dict]:
        """Saved spots that match, best first. A name match beats a tag match."""
        wanted = _words(f"{spot_text} {description}")
        if not wanted:
            return []
        scored = []
        for spot in self._spots:
            haystack = _words(" ".join([spot.get("name", ""), spot.get("type", ""),
                                        spot.get("area", ""), " ".join(spot.get("tags", []))]))
            score = len(wanted & haystack)
            if spot_text and spot_text.lower() in spot.get("name", "").lower():
                score += 5
            if score:
                scored.append((score, spot))
        if not scored:
            return []
        # A single shared word ("bar") is not enough when the person said more.
        if max(score for score, _ in scored) < 2 and len(wanted) > 1:
            return []
        scored.sort(key=lambda pair: -pair[0])
        return [spot for _, spot in scored]

    def _nominatim(self, query: dict, timeout: float):
        # Nominatim asks for no more than one request a second.
        if self._nominatim_calls:
            self._sleep(1.0)
        self._nominatim_calls += 1
        return self._get_json("https://nominatim.openstreetmap.org/search?" + urlencode(query),
                              "Nominatim", timeout)

    def _geocode(self, place: str, timeout: float) -> dict:
        """A UK place name -> a spot-shaped dict."""
        results = self._nominatim({"q": place, "format": "jsonv2", "limit": 1,
                                   "countrycodes": "gb", "addressdetails": 1}, timeout)
        if not isinstance(results, list) or not results:
            error = PlaceNotFound(f"nominatim found nothing for {place!r}")
            error.user_message = f"Sorry mate, I couldn't find {place} anywhere in the UK."
            raise error
        found = self._place_from(results[0], place)
        if found is None:
            raise ServiceDown("nominatim result had no coordinates")
        return found

    @staticmethod
    def _place_from(first: dict, place: str):
        """One Nominatim result -> a spot-shaped dict, or None without coordinates."""
        try:
            lat, lon = float(first["lat"]), float(first["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        address = first.get("address") or {}
        name = (first.get("name") or "").strip() or place
        parts = [p.strip() for p in (first.get("display_name") or "").split(",") if p.strip()]
        if parts and parts[0] == name:
            parts = parts[1:]
        return {
            "name": name,
            "type": (first.get("type") or "").replace("_", " "),
            "area": (address.get("suburb") or address.get("city_district")
                     or address.get("town") or address.get("city") or ""),
            "address": ", ".join(p for p in parts if p != "United Kingdom")[:120],
            "lat": lat, "lon": lon,
        }

    def _search_described(self, description: str, started: float) -> list[dict]:
        """A description -> up to 3 candidate places, trying three searches in turn.

        1. "ramen near London Bridge": that type of place near that area (Overpass).
        2. "garden with a glass dome": a Wikipedia search, then the map.
        3. Anything else: a free-text OpenStreetMap search.
        """
        for search in (self._search_nearby, self._search_web, self._search_map):
            if self._clock() - started > SPOT_BUDGET_S:
                break
            try:
                found = search(description)
            except AgentAppError:
                found = []  # that search failed or found nothing: try the next
            if found:
                return found
        # Nothing pinned it down: hand Google Maps the words to search with.
        return [{"name": description[:120], "type": "", "area": "", "address": "",
                 "_source": "maps_search"}]

    def _search_web(self, description: str) -> list[dict]:
        """Search Wikipedia with the description, and put the best place hit on the map."""
        query = description
        if not any(w in description.lower() for w in UK_WORDS):
            query += " London"
        reply = self._get_json("https://en.wikipedia.org/w/rest.php/v1/search/page?"
                               + urlencode({"q": query, "limit": 5}), "Wikipedia", 4.0)
        def is_uk_place(page: dict) -> bool:
            words = (page.get("description") or "").lower()
            return (any(w in words for w in PLACE_WORDS)
                    and any(w in words for w in UK_WORDS))

        pages = [p for p in (reply or {}).get("pages") or [] if p.get("title") and is_uk_place(p)]
        for page in pages[:2]:
            try:
                place = self._geocode(page["title"], 3.0)
            except PlaceNotFound:
                continue
            place["name"] = page["title"]
            place["type"] = page.get("description") or place["type"]
            place["_guess"] = True
            others = [{"name": p["title"], "type": p.get("description") or ""}
                      for p in pages if p is not page][:2]
            return [place] + others
        return []

    def _search_map(self, description: str) -> list[dict]:
        """A plain OpenStreetMap search with the words they used."""
        return [self._geocode(description, 3.0)]

    def _search_nearby(self, description: str) -> list[dict]:
        """'ramen near London Bridge' -> up to 3 places of that kind near that area."""
        lowered = description.lower()
        kind = next((t for t in PLACE_TYPES if re.search(rf"\b{t[0]}", lowered)), None)
        area_match = AREA.search(description)
        if not kind or not area_match:
            return []
        area = self._geocode(area_match.group(1).strip(" .?!"), 4.0)
        # The map search, kept to about a kilometre round the area: quick and reliable.
        box = (f"{area['lon'] - 0.015:.5f},{area['lat'] + 0.009:.5f},"
               f"{area['lon'] + 0.015:.5f},{area['lat'] - 0.009:.5f}")
        words = "rooftop bar" if kind[0] == "rooftop" else kind[0]
        try:
            results = self._nominatim({"q": words, "format": "jsonv2", "limit": 3,
                                       "viewbox": box, "bounded": 1, "addressdetails": 1}, 3.0)
        except AgentAppError:
            results = []
        found = [p for p in (self._place_from(r, "") for r in results or []) if p and p["name"]]
        for place in found:
            place["type"] = kind[3]
            place["area"] = place["area"] or area["name"]
        return found or self._search_overpass(kind, area)

    def _search_overpass(self, kind: tuple, area: dict) -> list[dict]:
        """The backup: OpenStreetMap's tag search (finds ramen shops without "ramen" in the name)."""
        word, key, value, label = kind
        query = (f'[out:json][timeout:6];nwr["{key}"="{value}"]["name"]'
                 f'(around:900,{_latlon(area)});out center 25;')
        reply = self._get_json(
            "https://overpass-api.de/api/interpreter?" + urlencode({"data": query}),
            "OpenStreetMap", 4.0)
        found = []
        for element in (reply or {}).get("elements", []):
            tags = element.get("tags") or {}
            centre = element.get("center") or {}
            lat, lon = element.get("lat", centre.get("lat")), element.get("lon", centre.get("lon"))
            if not tags.get("name") or lat is None or lon is None:
                continue
            street = " ".join(b for b in (tags.get("addr:housenumber"), tags.get("addr:street")) if b)
            # OSM rarely tags rooftops: keep every bar, but put roof ones first.
            roof_last = 0 if word != "rooftop" or "roof" in json.dumps(tags).lower() else 1
            found.append((roof_last, _km_between((area["lat"], area["lon"]), (lat, lon)), {
                "name": tags["name"], "type": label, "area": area["name"],
                "address": ", ".join(b for b in (street, tags.get("addr:postcode")) if b),
                "lat": lat, "lon": lon}))
        if not found:
            return []
        found.sort(key=lambda item: (item[0], item[1]))
        return [spot for _, _, spot in found[:3]]

    def _transit_minutes(self, origin: dict, spot: dict, now: datetime):
        """Quickest TfL journey in minutes, or None (outside London, or TfL failed)."""
        local = now.astimezone(LONDON)
        query = urlencode({"date": local.strftime("%Y%m%d"), "time": local.strftime("%H%M"),
                           "timeIs": "Departing"})
        url = (f"https://api.tfl.gov.uk/Journey/JourneyResults/"
               f"{quote(_latlon(origin), safe=',')}/to/{quote(_latlon(spot), safe=',')}?{query}")
        try:
            plan = self._get_json(url, "TfL journey planner", 4.0)
        except AgentAppError:
            return None
        durations = [j.get("duration") for j in (plan or {}).get("journeys") or []
                     if isinstance(j.get("duration"), int)]
        return min(durations) if durations else None

    def _on_this_day(self, call: Call) -> Result:
        started = self._clock()
        today = call.context.now.astimezone(LONDON).date()
        place = LINK.sub(" ", call.inputs.get("place") or "").strip()

        history_error = None
        try:
            history = self._london_history(today, place)
        except AgentAppError as exc:
            history, history_error = [], exc

        dated = []
        for spot in self._spots:
            try:
                dated.append((date.fromisoformat(spot["posted_on"]), spot))
            except (KeyError, TypeError, ValueError):
                continue
        hits = sorted(((d, s) for d, s in dated
                       if (d.month, d.day) == (today.month, today.day) and d.year < today.year),
                      key=lambda pair: pair[0], reverse=True)
        data = {
            "date_checked": f"{today.day} {today:%B}",
            "london_history": history,
            "spots": [f"{_spot_line(s)}, {d.year}" for d, s in hits],
            "years_ago": [today.year - d.year for d, _ in hits],
        }

        def gap(d: date) -> int:
            day = 28 if (d.month, d.day) == (2, 29) else d.day
            days = abs((date(today.year, d.month, day) - today).days)
            return min(days, 365 - days)

        earlier = [(gap(d), -d.toordinal(), d, s) for d, s in dated if d < today]
        if not hits and earlier:
            _, _, d, s = min(earlier, key=lambda item: (item[0], item[1]))
            data["nearest_other_date"] = f"{d.day} {d:%B} {d.year}: {_spot_line(s)}"

        if place and self._clock() - started < 12.0:
            story = self._place_story(place)
            if story:
                data["place_history"] = story

        if history_error and not history and not hits and "place_history" not in data:
            raise history_error

        parts = []
        if "place_history" in data:
            parts.append(f"🏰 {data['place_history']}")
        elif place:
            parts.append(f"Couldn't dig up the history of {place}, sorry mate.")
        if history:
            parts.append(f"📜 Ooh, a bit of history! On {data['date_checked']} in London: "
                         + "; ".join(history[:3]) + ".")
        elif not history_error:
            parts.append(f"Quiet one: nothing in London's history books for {data['date_checked']}, mate.")
        if hits:
            parts.append("📅 And from your own spots: " + "; ".join(data["spots"]) + ".")
        return Result(data=data, narrative=" ".join(parts))

    def _london_history(self, day: date, place: str) -> list[str]:
        """Wikipedia's events on this date that happened in London (or at the place)."""
        url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{day:%m}/{day:%d}"
        reply = self._get_json(url, "Wikipedia", 5.0)
        wanted = list(LONDON_WORDS) + ([place.lower()] if place else [])
        found = []
        for event in (reply or {}).get("events", []):
            text = " ".join((event.get("text") or "").split())
            pages = " ".join(f"{p.get('title', '')} {p.get('description', '')}"
                             for p in event.get("pages") or [])
            if not text or not isinstance(event.get("year"), int):
                continue
            if any(word in f"{text} {pages}".lower() for word in wanted):
                found.append((event["year"], f"{event['year']}: {text[:220]}"))
        found.sort()
        return [line for _, line in found[:6]]

    def _place_story(self, place: str) -> str:
        """A short history of a named place, from its Wikipedia summary. '' if none."""
        try:
            search = self._get_json(
                "https://en.wikipedia.org/w/rest.php/v1/search/title?"
                + urlencode({"q": place, "limit": 1}), "Wikipedia", 4.0)
            pages = (search or {}).get("pages") or []
            if not pages or not pages[0].get("key"):
                return ""
            summary = self._get_json(
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + quote(pages[0]["key"], safe=""), "Wikipedia", 4.0)
        except AgentAppError:
            return ""
        extract = " ".join(((summary or {}).get("extract") or "").split())
        if not extract:
            return ""
        if len(extract) > 450:
            cut = extract[:450].rsplit(". ", 1)[0]
            extract = cut + "." if len(cut) > 100 else extract[:450].rsplit(" ", 1)[0] + "…"
        title = (summary or {}).get("title") or pages[0].get("title") or place
        return f"{title}: {extract}"

class App(SpotsMixin):
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))
        # Replaced in tests so they never wait.
        self._sleep = time.sleep
        self._clock = time.monotonic
        self._spots = json.loads(SPOTS_PATH.read_text("utf-8"))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def handle(self, call: Call) -> Result:
        if call.outcome_id == "london.outfit":
            return self._outfit(call)
        if call.outcome_id == "london.jog":
            return self._jog(call)
        if call.outcome_id == "london.journey":
            return self._journey(call)
        if call.outcome_id == "london.find_spot":
            return self._find_spot(call)
        if call.outcome_id == "london.on_this_day":
            return self._on_this_day(call)
        raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

    # ── Network ─────────────────────────────────────────────────────────────

    def _fetch(self, url: str, timeout: float = 6.0) -> bytes:
        """Every outbound request goes through here."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _get_json(self, url: str, service: str, timeout: float):
        """Fetch and parse JSON, turning network failures into errors people can read.

        Never passes on the provider's error text: only the status code.
        """
        try:
            raw = self._fetch(url, timeout=timeout)
        except urllib.error.HTTPError as exc:  # must come first: it IS a URLError
            if exc.code == 429:
                raise ServiceBusy(f"{service} rate limited")
            raise ServiceDown(f"{service} returned {exc.code}")
        except TimeoutError as exc:  # not a URLError
            raise ServiceDown(f"{service} timed out: {exc}")
        except urllib.error.URLError as exc:
            raise ServiceDown(f"could not reach {service}: {exc.reason}")
        try:
            return json.loads(raw)
        except ValueError:
            raise ServiceDown(f"{service} sent something that is not JSON")

    def _locate(self, place: str, timeout: float = 4.0) -> tuple[float, float, str]:
        """A London place name -> (lat, lon, short name)."""
        query = urlencode({
            "q": place, "format": "jsonv2", "limit": 1, "countrycodes": "gb",
            "viewbox": LONDON_VIEWBOX, "bounded": 1,
        })
        results = self._get_json(
            "https://nominatim.openstreetmap.org/search?" + query, "Nominatim", timeout)
        if not isinstance(results, list) or not results:
            error = PlaceNotFound(f"nominatim found nothing for {place!r}")
            error.user_message = f"Sorry mate, I couldn't find {place} in London."
            raise error
        first = results[0]
        try:
            lat, lon = float(first["lat"]), float(first["lon"])
        except (KeyError, TypeError, ValueError):
            raise ServiceDown("nominatim result had no coordinates")
        name = (first.get("name") or "").strip() or place
        return lat, lon, name

    # ── 1. What to wear ─────────────────────────────────────────────────────

    def _outfit(self, call: Call) -> Result:
        place = (call.inputs.get("place") or "").strip()
        now = call.context.now.astimezone(LONDON)
        day = self._which_day((call.inputs.get("day") or "").strip(), now.date())

        if not place or place.lower() in ("london", "central london"):
            lat, lon, name = CENTRAL_LONDON[0], CENTRAL_LONDON[1], "London"
        else:
            lat, lon, name = self._locate(place)

        query = urlencode({
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "daily": ",".join([
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "apparent_temperature_max", "apparent_temperature_min",
                "precipitation_probability_max", "precipitation_sum",
                "wind_speed_10m_max", "uv_index_max",
            ]),
            "hourly": "precipitation_probability",
            "timezone": "Europe/London", "forecast_days": 7,
        })
        forecast = self._get_json(
            "https://api.open-meteo.com/v1/forecast?" + query, "Open-Meteo", 6.0)

        daily = forecast.get("daily") or {}
        days = daily.get("time") or []
        key = day.isoformat()
        if key not in days:
            raise ServiceDown(f"open-meteo had no forecast for {key}")
        i = days.index(key)

        def value(field: str, default: float = 0.0) -> float:
            series = daily.get(field) or []
            v = series[i] if i < len(series) else None
            return float(v) if v is not None else default

        code = int(value("weather_code"))
        t_low, t_high = value("temperature_2m_min"), value("temperature_2m_max")
        f_low, f_high = value("apparent_temperature_min"), value("apparent_temperature_max")
        rain_chance = int(round(value("precipitation_probability_max")))
        rain_mm = value("precipitation_sum")
        wind = value("wind_speed_10m_max")
        uv = value("uv_index_max")

        rain_from = self._rain_starts(forecast.get("hourly") or {}, day, now)
        sky_emoji, sky_words = WEATHER_CODES.get(code, ("🌡️", "mixed weather"))
        items = self._clothes(code, f_low, f_high, rain_chance, rain_mm, wind, uv)

        avatar = f"{sky_emoji} 🧍 " + "".join(emoji for emoji, _ in items)
        outfit = ", ".join(f"{emoji} {words}" for emoji, words in items)
        rain_timing = ""
        if rain_from:
            rain_timing = f"rain likely from {rain_from}"
        elif rain_chance < 30 and rain_mm < 1.0:
            rain_timing = "staying dry"
        elif rain_chance >= 30 or rain_mm >= 1.0:
            rain_timing = "some rain possible"

        data = {
            "place": name,
            "day": _pretty_day(day),
            "conditions": sky_words,
            "avatar": avatar,
            "outfit": outfit,
            "temperature_low_c": round(t_low, 1),
            "temperature_high_c": round(t_high, 1),
            "feels_like_low_c": round(f_low, 1),
            "feels_like_high_c": round(f_high, 1),
            "rain_chance_percent": rain_chance,
            "rain_mm": round(rain_mm, 1),
            "wind_kmh": round(wind, 1),
        }
        if rain_timing:
            data["rain_timing"] = rain_timing

        narrative = (
            f"Alright mate! For {name}, {_pretty_day(day)}: {avatar}. {sky_words.capitalize()}, "
            f"{round(t_low)} to {round(t_high)}°C (feels like {round(f_low)} to {round(f_high)}°C), "
            f"{rain_chance}% chance of rain"
            + (f", {rain_timing}" if rain_timing else "") + f". Wear: {outfit}."
        )
        if rain_chance >= 50:
            narrative += " Don't forget your brolly! ☂️"
        elif rain_chance <= 20 and f_high >= 18:
            narrative += " Lovely day for it! 😎"
        elif f_high < 5:
            narrative += " Bit nippy out there, wrap up warm!"
        return Result(data=data, narrative=narrative)

    @staticmethod
    def _which_day(text: str, today: date) -> date:
        words = text.lower().strip(" .?!")
        if words in ("", "today", "now", "tonight", "this morning", "this afternoon",
                     "this evening", "later"):
            return today
        if words in ("tomorrow", "tmrw", "tomorrow morning", "tomorrow afternoon"):
            return today + timedelta(days=1)
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                wanted = datetime.strptime(words, fmt).date()
                break
            except ValueError:
                continue
        else:
            wanted = None
            for n, name in enumerate(WEEKDAYS):
                if name in words or words == name[:3]:
                    ahead = (n - today.weekday()) % 7
                    if "next" in words and ahead == 0:
                        ahead = 7
                    wanted = today + timedelta(days=ahead)
                    break
        if wanted is None:
            raise CannotReadDay(f"could not read {text!r} as a day")
        if not 0 <= (wanted - today).days <= 6:
            raise CannotReadDay(f"{wanted} is outside the 7-day forecast")
        return wanted

    @staticmethod
    def _rain_starts(hourly: dict, day: date, now: datetime) -> str:
        """The first hour on that day (from now on, if it is today) with rain likely."""
        times = hourly.get("time") or []
        chances = hourly.get("precipitation_probability") or []
        for stamp, chance in zip(times, chances):
            if not stamp.startswith(day.isoformat()) or chance is None:
                continue
            hour = int(stamp[11:13])
            if day == now.date() and hour < now.hour:
                continue
            if chance >= 50:
                return f"{hour:02d}:00"
        return ""

    @staticmethod
    def _clothes(code, f_low, f_high, rain_chance, rain_mm, wind, uv) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        feel = f_high
        windy = wind >= 40
        wet = rain_chance >= 50 or rain_mm >= 1.0
        very_wet = rain_mm >= 5.0 or (wet and windy)

        if feel < 3:
            items += [("🧥", "heavy winter coat"), ("🧣", "scarf"), ("🧤", "gloves"), ("🧢", "warm hat")]
        elif feel < 8:
            items += [("🧥", "warm coat"), ("🧣", "scarf")]
            if f_low < 3:
                items.append(("🧤", "gloves"))
        elif feel < 13:
            items += [("🧶", "jumper"), ("🧥", "light jacket")]
        elif feel < 18:
            items += [("🧶", "light jumper or long sleeves")]
        elif feel < 23:
            items += [("👕", "t-shirt"), ("🧥", "light layer for the evening")]
        else:
            items += [("👕", "t-shirt"), ("🩳", "shorts or a light dress")]

        if code in SNOW_CODES:
            items.append(("🥾", "boots with good grip"))
        elif very_wet:
            items += [("🧥", "waterproof raincoat with a hood"), ("👢", "waterproof boots")]
        if wet and not windy:
            items.append(("☂️", "umbrella"))
        elif 30 <= rain_chance < 50 and not windy:
            items.append(("☂️", "small umbrella, just in case"))
        if windy:
            items.append(("💨", "too windy for an umbrella, wear a hood"))
        if code in THUNDER_CODES:
            items.append(("⛈️", "thunder possible, stay off open ground"))
        if uv >= 5:
            items += [("🕶️", "sunglasses"), ("🧴", "sun cream")]
        elif uv >= 3 and feel >= 18:
            items.append(("🕶️", "sunglasses"))
        if not any(emoji in ("👢", "🥾") for emoji, _ in items):
            items.append(("👟", "comfy trainers"))
        return items

    # ── 2. Jogging route ────────────────────────────────────────────────────

    def _jog(self, call: Call) -> Result:
        start = (call.inputs.get("start") or "").strip()
        if not start:
            error = MissingPlace("no starting point")
            error.user_message = "Where are you setting off from, mate? Say something like: a 5 km jog from Imperial College."
            raise error
        distance = call.inputs.get("distance_km")
        target = float(distance) if distance not in (None, "") else 5.0
        if not 1.0 <= target <= 42.2:
            raise BadDistance(f"distance {target} out of range")

        lat, lon, name = self._locate(start)
        here = (lat, lon)

        options = []
        for park, plat, plon, lap, sight in PARKS:
            radius = lap / (2 * math.pi)
            straight = _km_between(here, (plat, plon))
            out = max(0.0, straight * STREET_FACTOR - radius)
            inside = target - 2 * out
            if inside < 0.5:
                continue  # too far to get there and back
            laps = inside / lap
            # About one lap is nicest, and less of the run on streets is better.
            score = 0.6 * abs(laps - 1.0) + 2 * out / target + (0.5 if laps > 3 else 0.0)
            options.append((score, park, out, inside, laps, sight))
        options.sort()

        nearest = min(PARKS, key=lambda p: _km_between(here, (p[1], p[2])))
        nearest_km = _km_between(here, (nearest[1], nearest[2])) * STREET_FACTOR
        if nearest_km > 20:
            error = PlaceNotFound(f"{start!r} is {nearest_km:.0f} km from the nearest park")
            error.user_message = f"{start}'s a bit far out, mate. I only know running routes in London's parks."
            raise error

        if not options:
            half = target / 2
            route = (f"No park is close enough for {target:g} km there and back. Run {half:.1f} km "
                     f"out along the streets and turn round. The nearest park, {nearest[0]}, "
                     f"is about {nearest_km:.1f} km away.")
            data = {"start": name, "distance_km": round(target, 1), "park": "",
                    "route": route, "total_km": round(target, 1)}
            return Result(data=data, narrative=f"Right then! No park close enough, so here's a street run from {name}: {route}")

        best = options[0]
        route = self._describe_run(name, *best[1:])
        data = {
            "start": name,
            "distance_km": round(target, 1),
            "park": best[1],
            "route": route,
            "total_km": round(2 * best[2] + best[3], 1),
        }
        others = [f"{o[1]} ({o[2]:.1f} km away, then {o[3]:.1f} km inside)" for o in options[1:3]]
        if others:
            data["other_parks"] = "; ".join(others)
        return Result(data=data, narrative=f"🏃 Lace up, mate! {target:g} km from {name}: {route} Enjoy the run!")

    @staticmethod
    def _describe_run(start, park, out, inside, laps, sight) -> str:
        if laps < 0.8:
            loop = f"run about {inside:.1f} km inside, part of a lap, past {sight}"
        elif laps <= 1.3:
            loop = f"run one lap of about {inside:.1f} km, past {sight}"
        else:
            loop = f"run about {inside:.1f} km inside, roughly {laps:.1f} laps, past {sight}"
        if out < 0.2:
            return (f"🌳 You're right by {park}: {loop}, and finish where you started. "
                    "Distances are estimates.")
        return (f"🏃 Head to {park} (about {out:.1f} km), {loop}, then the same "
                f"{out:.1f} km back to {start}. Distances are estimates.")

    # ── 3. Journey and strikes ──────────────────────────────────────────────

    def _journey(self, call: Call) -> Result:
        origin = (call.inputs.get("origin") or "").strip()
        destination = (call.inputs.get("destination") or "").strip()
        if not origin or not destination:
            raise MissingPlace("origin or destination missing")

        a_lat, a_lon, a_name = self._locate(origin, timeout=3.0)
        # Nominatim asks for no more than one request a second.
        self._sleep(1.0)
        b_lat, b_lon, b_name = self._locate(destination, timeout=3.0)

        now = call.context.now.astimezone(LONDON)
        query = urlencode({"date": now.strftime("%Y%m%d"), "time": now.strftime("%H%M"),
                           "timeIs": "Departing"})
        url = (f"https://api.tfl.gov.uk/Journey/JourneyResults/"
               f"{quote(f'{a_lat:.5f},{a_lon:.5f}', safe=',')}/to/"
               f"{quote(f'{b_lat:.5f},{b_lon:.5f}', safe=',')}?{query}")
        try:
            plan = self._get_json(url, "TfL journey planner", 6.0)
        except ServiceDown as exc:
            if " returned 404" in str(exc) or " returned 400" in str(exc):
                raise NoJourney(f"tfl found no journey {origin!r} -> {destination!r}")
            raise
        journeys = (plan or {}).get("journeys") or []
        if not journeys:
            raise NoJourney(f"tfl returned no journeys {origin!r} -> {destination!r}")

        summaries, lines_per_journey = [], []
        for journey in journeys[:3]:
            text, lines = self._describe_journey(journey)
            summaries.append(text)
            lines_per_journey.append(lines)

        all_lines: list[str] = []
        for lines in lines_per_journey:
            for line in lines:
                if line not in all_lines:
                    all_lines.append(line)

        problems = self._line_problems(all_lines)
        best_lines = set(lines_per_journey[0])
        affected = any(line in best_lines for line in problems)
        strikes = self._strikes_elsewhere(set(all_lines))
        strike_free = ""
        if affected:
            for i, lines in enumerate(lines_per_journey[1:], start=1):
                if not any(line in problems for line in lines):
                    strike_free = summaries[i]
                    break

        if problems:
            line_problems = " | ".join(f"⚠️ {text}" for text in problems.values())
        else:
            line_problems = "✅ No strikes or delays reported on the lines these routes use."

        data = {
            "origin": a_name,
            "destination": b_name,
            "best_route": summaries[0],
            "best_route_minutes": int(journeys[0].get("duration") or 0),
            "best_route_affected": affected,
            "line_problems": line_problems,
        }
        if len(summaries) > 1:
            data["other_routes"] = " || ".join(summaries[1:])
        fare = (journeys[0].get("fare") or {}).get("totalCost")
        if isinstance(fare, (int, float)) and fare > 0:
            data["fare"] = f"£{fare / 100:.2f}"
        if strikes:
            data["strikes_elsewhere"] = strikes
        if strike_free:
            data["strike_free_route"] = strike_free

        if problems:
            hit = ", ".join(text.split(":", 1)[0] for text in problems.values())
            news = f"⚠️ Heads up, mate, trouble on: {hit}." + (" That hits your best route." if affected else "")
        else:
            news = "✅ Good news, no strikes or delays on these lines!"
        if strike_free:
            news += f" ✅ Dodge it this way instead: {strike_free}."
        elif affected:
            news += " Every route uses an affected line, so give yourself extra time, mate."
        narrative = f"🚇 Right, {a_name} to {b_name}: {summaries[0]}. {news}"
        return Result(data=data, narrative=narrative)

    @staticmethod
    def _describe_journey(journey: dict) -> tuple[str, list[str]]:
        steps, lines = [], []
        for leg in journey.get("legs") or []:
            mode = ((leg.get("mode") or {}).get("id") or "").lower()
            minutes = leg.get("duration") or 0
            summary = ((leg.get("instruction") or {}).get("summary") or "").strip()
            if mode == "walking":
                steps.append(f"🚶 walk {minutes} min")
                continue
            emoji = {"tube": "🚇", "bus": "🚌", "dlr": "🚈", "overground": "🚆",
                     "elizabeth-line": "🚆", "national-rail": "🚆", "tram": "🚊",
                     "river-bus": "⛴️", "cable-car": "🚡"}.get(mode, "🚆")
            steps.append(f"{emoji} {summary or mode} ({minutes} min)")
            for option in leg.get("routeOptions") or []:
                line = ((option.get("lineIdentifier") or {}).get("id") or "").strip()
                if line and line not in lines:
                    lines.append(line)
        total = journey.get("duration") or 0
        return f"{total} min: " + " → ".join(steps), lines

    def _line_problems(self, lines: list[str]) -> dict[str, str]:
        """line id -> what is wrong with it, for every line not running normally."""
        if not lines:
            return {}
        url = "https://api.tfl.gov.uk/Line/" + quote(",".join(lines[:20]), safe=",") + "/Status"
        try:
            statuses = self._get_json(url, "TfL line status", 3.0)
        except (ServiceDown, ServiceBusy):
            return {"status": "Could not check live line status just now."}
        return self._problems_from(statuses, lambda line_id: True)

    def _strikes_elsewhere(self, on_route: set[str]) -> str:
        """Strike notices on the tube and rail network, beyond the lines on the route."""
        url = "https://api.tfl.gov.uk/Line/Mode/tube,elizabeth-line,overground,dlr/Status"
        try:
            statuses = self._get_json(url, "TfL line status", 3.0)
        except (ServiceDown, ServiceBusy):
            return ""
        problems = self._problems_from(statuses, lambda line_id: line_id not in on_route,
                                       strikes_only=True)
        return " | ".join(f"🪧 {text}" for text in problems.values())

    @staticmethod
    def _problems_from(statuses, keep, strikes_only: bool = False) -> dict[str, str]:
        problems: dict[str, str] = {}
        if not isinstance(statuses, list):
            return problems
        for line in statuses:
            line_id = line.get("id") or ""
            if not keep(line_id):
                continue
            name = line.get("name") or line_id
            # TfL starts most reasons with "District Line: ...", which we already say.
            prefix = re.compile(r"^\s*" + re.escape(name) + r"(\s+line)?\s*:\s*", re.I)
            seen, parts = set(), []
            for status in line.get("lineStatuses") or []:
                if status.get("statusSeverity") == 10:  # Good Service
                    continue
                reason = prefix.sub("", " ".join((status.get("reason") or "").split()))
                label = status.get("statusSeverityDescription") or "Disruption"
                if strikes_only and "strike" not in reason.lower():
                    continue
                if reason in seen:
                    continue
                seen.add(reason)
                if len(reason) > 200:
                    reason = reason[:197].rsplit(" ", 1)[0] + "..."
                parts.append(f"{label}: {reason}" if reason else label)
            if parts:
                problems[line_id] = f"{name}: " + " / ".join(parts)
        return problems
