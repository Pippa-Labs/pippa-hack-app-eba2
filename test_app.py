"""Tests for London Local, run through LocalRunner so the manifest is enforced.

No test touches the network: `_fetch` is replaced on the instance with a stub
that answers by URL, using small payloads shaped like the real APIs' replies.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner, RateLimitedError, ServiceUnavailableError

from app import (App, BadDistance, CannotReadDay, MissingPlace, NoJourney, PlaceNotFound,
                 SpotNotFound)

# Friday 20 November 2026, 10:00 in London (GMT, so the same as UTC).
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)

IMPERIAL = [{"lat": "51.4989595", "lon": "-0.1756407", "name": "Imperial College London"}]
EALING = [{"lat": "51.5149803", "lon": "-0.3004067", "name": "Ealing Broadway"}]
BRIGHTON = [{"lat": "50.8225", "lon": "-0.1372", "name": "Brighton"}]

DAYS = ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23",
        "2026-11-24", "2026-11-25", "2026-11-26"]


def forecast(code=63, t_min=5.0, t_max=9.0, f_min=2.0, f_max=6.0, rain=80,
             rain_mm=3.0, wind=15.0, uv=0.5, rain_hour=15):
    """An Open-Meteo reply where every day has the same weather."""
    hours = [f"{d}T{h:02d}:00" for d in DAYS for h in range(24)]
    chances = [rain if int(t[11:13]) >= rain_hour else 0 for t in hours]
    return {
        "daily": {
            "time": DAYS,
            "weather_code": [code] * 7,
            "temperature_2m_min": [t_min] * 7,
            "temperature_2m_max": [t_max] * 7,
            "apparent_temperature_min": [f_min] * 7,
            "apparent_temperature_max": [f_max] * 7,
            "precipitation_probability_max": [rain] * 7,
            "precipitation_sum": [rain_mm] * 7,
            "wind_speed_10m_max": [wind] * 7,
            "uv_index_max": [uv] * 7,
        },
        "hourly": {"time": hours, "precipitation_probability": chances},
    }


JOURNEY = {
    "journeys": [
        {
            "duration": 50,
            "fare": {"totalCost": 330},
            "legs": [
                {"mode": {"id": "walking"}, "duration": 12,
                 "instruction": {"summary": "Walk to South Kensington Station"}},
                {"mode": {"id": "tube"}, "duration": 20,
                 "instruction": {"summary": "District line to Ealing Broadway"},
                 "routeOptions": [{"lineIdentifier": {"id": "district"}}]},
                {"mode": {"id": "walking"}, "duration": 6,
                 "instruction": {"summary": "Walk to The Broadway"}},
            ],
        },
        {
            "duration": 54,
            "legs": [
                {"mode": {"id": "bus"}, "duration": 13,
                 "instruction": {"summary": "52 bus to Notting Hill Gate"},
                 "routeOptions": [{"lineIdentifier": {"id": "52"}}]},
                {"mode": {"id": "tube"}, "duration": 18,
                 "instruction": {"summary": "Central line to Ealing Broadway"},
                 "routeOptions": [{"lineIdentifier": {"id": "central"}}]},
            ],
        },
    ]
}

ROUTE_STATUS = [
    {"id": "district", "name": "District", "lineStatuses": [
        {"statusSeverity": 5, "statusSeverityDescription": "Part Closure",
         "reason": "District Line: No service between Turnham Green and Ealing Broadway due to strike action."}]},
    {"id": "central", "name": "Central", "lineStatuses": [
        {"statusSeverity": 10, "statusSeverityDescription": "Good Service"}]},
    {"id": "52", "name": "52", "lineStatuses": [
        {"statusSeverity": 10, "statusSeverityDescription": "Good Service"}]},
]

NETWORK_STATUS = ROUTE_STATUS[:2] + [
    {"id": "piccadilly", "name": "Piccadilly", "lineStatuses": [
        {"statusSeverity": 4, "statusSeverityDescription": "Planned Closure",
         "reason": "TUBE STRIKE: no Piccadilly line service until Saturday evening."}]},
    {"id": "jubilee", "name": "Jubilee", "lineStatuses": [
        {"statusSeverity": 9, "statusSeverityDescription": "Minor Delays",
         "reason": "Jubilee Line: Minor delays due to a signal failure."}]},
]

GOOD_STATUS = [
    {"id": line, "name": line.title(), "lineStatuses": [
        {"statusSeverity": 10, "statusSeverityDescription": "Good Service"}]}
    for line in ("district", "central", "52")
]


def app_with(routes: dict) -> App:
    """An App whose network answers from `routes`: URL fragment -> reply.

    A reply is JSON-able data, or an exception to raise. The first matching
    fragment wins, and a list of replies is used up one per request.
    """
    app = App()
    app._sleep = lambda seconds: None
    queues = {k: (list(v) if isinstance(v, tuple) else v) for k, v in routes.items()}

    def fetch(url: str, timeout: float = 6.0) -> bytes:
        for fragment, reply in queues.items():
            if fragment in url:
                if isinstance(reply, list) and reply and isinstance(reply[0], tuple):
                    reply = reply.pop(0)[0]
                if isinstance(reply, BaseException):
                    raise reply
                return json.dumps(reply).encode("utf-8")
        raise AssertionError(f"unexpected request: {url}")

    app._fetch = fetch
    return app


def run(app: App, outcome: str, inputs: dict, *, explicit: bool = False):
    return LocalRunner(app).call(
        outcome, inputs, granted_secrets={}, now=NOW, request_id="t1", explicit=explicit,
    ).result


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.com", code, "error", {}, None)


# ── The manifest ────────────────────────────────────────────────────────────


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_eba2"
    assert [o.id for o in app.manifest.outcomes] == [
        "london.outfit", "london.jog", "london.journey",
        "london.find_spot", "london.on_this_day"]


# ── 1. What to wear ─────────────────────────────────────────────────────────


def test_a_cold_wet_day_gets_a_coat_and_an_umbrella():
    app = app_with({"open-meteo": forecast()})
    data = run(app, "london.outfit", {}).data
    assert data["place"] == "London"
    assert data["day"] == "Friday 20 November"
    assert "☂️" in data["avatar"] and "🧥" in data["avatar"]
    assert "umbrella" in data["outfit"]
    assert data["rain_chance_percent"] == 80
    assert data["rain_timing"] == "rain likely from 15:00"


def test_heavy_rain_and_wind_means_a_raincoat_not_an_umbrella():
    app = app_with({"open-meteo": forecast(code=65, rain=95, rain_mm=12.0, wind=45.0)})
    data = run(app, "london.outfit", {}).data
    assert "raincoat" in data["outfit"]
    assert "👢" in data["avatar"]
    assert "☂️" not in data["avatar"]


def test_a_warm_sunny_day_gets_shorts_and_sun_cream():
    app = app_with({"open-meteo": forecast(code=0, t_min=18, t_max=28, f_min=18,
                                           f_max=29, rain=0, rain_mm=0.0, uv=7)})
    data = run(app, "london.outfit", {}).data
    assert "🩳" in data["avatar"] and "🧴" in data["avatar"]
    assert "☂️" not in data["avatar"]
    assert data["rain_timing"] == "staying dry"


def test_a_weekday_name_picks_the_coming_day():
    app = app_with({"open-meteo": forecast()})
    assert run(app, "london.outfit", {"day": "Saturday"}).data["day"] == "Saturday 21 November"
    assert run(app, "london.outfit", {"day": "tomorrow"}).data["day"] == "Saturday 21 November"


def test_a_day_past_the_forecast_is_refused():
    app = app_with({"open-meteo": forecast()})
    with pytest.raises(CannotReadDay):
        run(app, "london.outfit", {"day": "2026-12-25"})
    with pytest.raises(CannotReadDay):
        run(app, "london.outfit", {"day": "some time soon"})


def test_a_named_area_is_looked_up():
    app = app_with({"nominatim": [{"lat": "51.539", "lon": "-0.142", "name": "Camden Town"}],
                    "open-meteo": forecast()})
    assert run(app, "london.outfit", {"place": "Camden"}).data["place"] == "Camden Town"


def test_an_unknown_place_says_which_place():
    app = app_with({"nominatim": []})
    with pytest.raises(PlaceNotFound) as failure:
        run(app, "london.outfit", {"place": "Narnia"})
    assert failure.value.user_message == "I could not find Narnia in London."


def test_a_weather_timeout_is_a_retryable_error():
    app = app_with({"open-meteo": TimeoutError("timed out")})
    with pytest.raises(ServiceUnavailableError):
        run(app, "london.outfit", {})


# ── 2. Jogging route ────────────────────────────────────────────────────────


def test_a_5k_from_imperial_goes_to_kensington_gardens():
    app = app_with({"nominatim": IMPERIAL})
    data = run(app, "london.jog", {"start": "Imperial College", "distance_km": 5}).data
    assert data["park"] == "Kensington Gardens"
    assert data["distance_km"] == 5.0
    assert abs(data["total_km"] - 5.0) < 0.2
    assert "Kensington Gardens" in data["route"]
    assert "Hyde Park" in data.get("other_parks", "")


def test_no_distance_means_5k():
    app = app_with({"nominatim": IMPERIAL})
    assert run(app, "london.jog", {"start": "Imperial College"}).data["distance_km"] == 5.0


def test_a_jog_with_no_start_asks_for_one():
    with pytest.raises(MissingPlace) as failure:
        run(app_with({}), "london.jog", {"distance_km": 5})
    assert "starting from" in failure.value.user_message


def test_a_marathon_and_a_half_is_refused():
    with pytest.raises(BadDistance):
        run(app_with({"nominatim": IMPERIAL}), "london.jog",
            {"start": "Imperial College", "distance_km": 100})


def test_somewhere_far_from_london_is_refused():
    with pytest.raises(PlaceNotFound) as failure:
        run(app_with({"nominatim": BRIGHTON}), "london.jog",
            {"start": "Brighton", "distance_km": 5})
    assert "Brighton" in failure.value.user_message


# ── 3. Journey and strikes ──────────────────────────────────────────────────


def journey_app(route_status=ROUTE_STATUS, journey=JOURNEY):
    return app_with({
        "nominatim": [(IMPERIAL,), (EALING,)],
        "Journey/JourneyResults": journey,
        "Line/Mode/": NETWORK_STATUS,
        "/Status": route_status,
    })


def test_a_strike_on_the_best_route_is_reported():
    data = run(journey_app(), "london.journey",
               {"origin": "Imperial College", "destination": "Ealing Broadway"}).data
    assert data["origin"] == "Imperial College London"
    assert data["destination"] == "Ealing Broadway"
    assert data["best_route_minutes"] == 50
    assert "District line" in data["best_route"]
    assert data["best_route_affected"] is True
    assert "strike action" in data["line_problems"]
    # TfL's "District Line:" prefix is not repeated after the line's name.
    assert "District: Part Closure: No service" in data["line_problems"]
    assert "Central line" in data["other_routes"]
    assert data["fare"] == "£3.30"
    # Strikes on other lines are mentioned; plain delays elsewhere are not.
    assert "Piccadilly" in data["strikes_elsewhere"]
    assert "Jubilee" not in data["strikes_elsewhere"]


def test_good_service_is_said_plainly():
    data = run(journey_app(route_status=GOOD_STATUS), "london.journey",
               {"origin": "Imperial College", "destination": "Ealing Broadway"}).data
    assert data["best_route_affected"] is False
    assert data["line_problems"].startswith("✅")


def test_a_journey_needs_both_ends():
    with pytest.raises(MissingPlace):
        run(app_with({}), "london.journey", {"origin": "Imperial College", "destination": ""})


def test_no_route_found_is_said_plainly():
    with pytest.raises(NoJourney):
        run(journey_app(journey={"journeys": []}), "london.journey",
            {"origin": "Imperial College", "destination": "Ealing Broadway"})


def test_tfl_rate_limit_is_retryable():
    with pytest.raises(RateLimitedError):
        run(journey_app(journey=http_error(429)), "london.journey",
            {"origin": "Imperial College", "destination": "Ealing Broadway"})


def test_picked_by_name_it_answers_the_same():
    inputs = {"origin": "Imperial College", "destination": "Ealing Broadway"}
    picked = run(journey_app(), "london.journey", inputs, explicit=True)
    assert picked.data == run(journey_app(), "london.journey", inputs).data


# ── 4. Find a saved spot ────────────────────────────────────────────────────

SAVED = [
    {"name": "Sky Garden", "type": "viewpoint", "area": "City of London",
     "address": "1 Sky Garden Walk, EC3M 8AF", "lat": 51.5111, "lon": -0.0835,
     "tags": ["rooftop", "glass dome", "views"], "posted_on": "2024-11-20"},
    {"name": "Kanada-Ya", "type": "ramen restaurant", "area": "Covent Garden",
     "address": "64 St Giles High Street, WC2H 8LE", "lat": 51.5155, "lon": -0.1270,
     "tags": ["ramen", "noodles"], "posted_on": "2023-11-18"},
]

KINGS_CROSS = [{"lat": "51.5308", "lon": "-0.1238", "name": "King's Cross",
                "type": "station", "display_name": "King's Cross, London, United Kingdom",
                "address": {"suburb": "King's Cross"}}]


def spot_app(routes: dict, spots=SAVED) -> App:
    app = app_with(routes)
    app._spots = spots
    return app


def test_a_saved_spot_is_found_by_name():
    data = run(spot_app({}), "london.find_spot", {"spot": "Sky Garden"}).data
    assert data["name"] == "Sky Garden" and data["source"] == "saved"
    assert "travelmode=transit" in data["directions_url"]


def test_a_saved_spot_is_found_by_description():
    data = run(spot_app({}), "london.find_spot", {"description": "that glass dome rooftop"}).data
    assert data["name"] == "Sky Garden"


def test_an_unsaved_name_is_looked_up():
    reply = [{"lat": "51.5081", "lon": "-0.0759", "name": "Tower of London",
              "type": "castle", "display_name": "Tower of London, Tower Hill, London, United Kingdom",
              "address": {"suburb": "Tower Hill"}}]
    data = run(spot_app({"nominatim": reply}), "london.find_spot",
               {"spot": "Tower of London"}).data
    assert data["source"] == "lookup" and data["area"] == "Tower Hill"


def test_a_description_finds_nearby_candidates():
    area = [{"lat": "51.5055", "lon": "-0.0860", "name": "London Bridge",
             "display_name": "London Bridge, London, United Kingdom", "address": {}}]
    overpass = {"elements": [
        {"type": "node", "lat": 51.5040, "lon": -0.0870,
         "tags": {"name": "Hakata Ramen", "cuisine": "ramen", "addr:street": "Bermondsey Street"}},
        {"type": "way", "center": {"lat": 51.5070, "lon": -0.0900},
         "tags": {"name": "Tonkotsu Bankside", "cuisine": "ramen"}},
    ]}
    data = run(spot_app({"nominatim": area, "overpass-api": overpass}, spots=[]),
               "london.find_spot", {"description": "ramen near London Bridge"}).data
    assert data["source"] == "description"
    assert data["name"] == "Hakata Ramen"
    assert any("Tonkotsu" in other for other in data["other_matches"])


def test_only_a_link_is_refused_and_never_fetched():
    with pytest.raises(SpotNotFound) as failure:
        run(spot_app({}), "london.find_spot",
            {"spot": "https://www.instagram.com/p/abc123/", "description": ""})
    assert "Instagram links" in failure.value.user_message


def test_a_routing_timeout_still_returns_the_spot():
    data = run(spot_app({"nominatim": KINGS_CROSS,
                         "Journey/JourneyResults": TimeoutError("slow")}),
               "london.find_spot", {"spot": "Sky Garden", "start": "King's Cross"}).data
    assert data["name"] == "Sky Garden"
    assert data["start_resolved"] == "King's Cross"
    assert data["distance_km"] > 3 and data["walk_minutes"] > 30
    assert "transit_minutes" not in data


def test_a_start_adds_transit_minutes():
    data = run(spot_app({"nominatim": KINGS_CROSS,
                         "Journey/JourneyResults": {"journeys": [{"duration": 24}, {"duration": 31}]}}),
               "london.find_spot", {"spot": "Sky Garden", "start": "King's Cross"},
               explicit=True).data
    assert data["transit_minutes"] == 24
    assert "origin=" in data["directions_url"]


# ── 5. On this date ─────────────────────────────────────────────────────────


def test_spots_from_this_date_in_earlier_years():
    data = run(spot_app({}), "london.on_this_day", {}).data
    assert data["date_checked"] == "20 November"
    assert data["spots"] == ["Sky Garden (viewpoint, City of London), 2024"]
    assert data["years_ago"] == [2]


def test_no_spot_on_this_date_gives_the_nearest():
    data = run(spot_app({}, spots=SAVED[1:]), "london.on_this_day", {}).data
    assert data["spots"] == [] and data["years_ago"] == []
    assert data["nearest_other_date"].startswith("18 November 2023: Kanada-Ya")
