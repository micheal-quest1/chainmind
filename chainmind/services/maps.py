"""Google Maps Platform integration: shortest-path routing + geocoding.

Uses Routes API v2 `computeRoutes` (the current, cheaper, faster replacement
for Directions) with `TRAFFIC_AWARE_OPTIMAL` so the ETA reflects live traffic.
Also exposes `route_matrix` for the fan-out case where the planner must compare
N candidate warehouses at once - one HTTP call instead of N.

Every call is wrapped in the circuit breaker and has a deterministic haversine
fallback, so losing the Maps key degrades accuracy but never breaks the flow.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, asdict
from typing import Any, Iterable

import httpx

from chainmind.core.config import get_settings
from chainmind.core.reliability import resilient

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Reuse one HTTP/2 client: TLS handshakes are the dominant cost otherwise.
_client: httpx.AsyncClient | None = None
_geo_cache: dict[str, tuple[float, float, str]] = {}
_route_cache: dict[tuple, "RouteLeg"] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(6.0, connect=2.5),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass
class RouteLeg:
    origin: tuple[float, float]
    destination: tuple[float, float]
    distance_km: float
    duration_minutes: float
    polyline: str | None
    source: str  # "google-routes-v2" | "haversine-fallback"
    traffic_aware: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["distance_km"] = round(self.distance_km, 2)
        d["duration_minutes"] = round(self.duration_minutes, 1)
        return d


# ------------------------------------------------------------------ fallback
def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _fallback_leg(origin, destination) -> RouteLeg:
    """Great-circle distance x 1.27 road-winding factor, 48 km/h effective."""
    straight = haversine_km(origin, destination)
    road_km = straight * 1.27
    return RouteLeg(
        origin=origin,
        destination=destination,
        distance_km=road_km,
        duration_minutes=(road_km / 48.0) * 60.0,
        polyline=None,
        source="haversine-fallback",
    )


# ------------------------------------------------------------------- geocode
async def geocode(address: str) -> tuple[float, float, str]:
    """Address -> (lat, lng, formatted). Cached; falls back to city lookup."""
    key = address.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]

    s = get_settings()
    if s.maps_live:

        async def call() -> tuple[float, float, str]:
            r = await client().get(
                GEOCODE_URL, params={"address": address, "key": s.maps_api_key, "region": "in"}
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "OK" or not data.get("results"):
                raise RuntimeError(f"geocode status={data.get('status')}")
            top = data["results"][0]
            loc = top["geometry"]["location"]
            return float(loc["lat"]), float(loc["lng"]), top["formatted_address"]

        async def fb() -> tuple[float, float, str]:
            return _city_guess(address)

        out = await resilient("maps.geocode", call, fallback=fb)
    else:
        out = _city_guess(address)

    _geo_cache[key] = out
    return out


_CITY_TABLE = {
    "chennai": (13.0827, 80.2707),
    "madras": (13.0827, 80.2707),
    "bengaluru": (12.9716, 77.5946),
    "bangalore": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "mumbai": (19.0760, 72.8777),
    "pune": (18.5204, 73.8567),
    "delhi": (28.6139, 77.2090),
    "gurgaon": (28.4595, 77.0266),
    "noida": (28.5355, 77.3910),
    "coimbatore": (11.0168, 76.9558),
    "kochi": (9.9312, 76.2673),
    "madurai": (9.9252, 78.1198),
    "kolkata": (22.5726, 88.3639),
    "ahmedabad": (23.0225, 72.5714),
    "jaipur": (26.9124, 75.7873),
    "trichy": (10.7905, 78.7047),
    "salem": (11.6643, 78.1460),
    "vellore": (12.9165, 79.1325),
    "mysuru": (12.2958, 76.6394),
    "mysore": (12.2958, 76.6394),
}


def _city_guess(address: str) -> tuple[float, float, str]:
    low = address.lower()
    for city, (lat, lng) in _CITY_TABLE.items():
        if city in low:
            return lat, lng, f"{address} ({city.title()}, IN)"
    # Unknown destination: default to Chennai so the flow still completes.
    return 13.0827, 80.2707, f"{address} (unresolved, defaulted to Chennai)"


# --------------------------------------------------------------------- route
async def compute_route(
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    traffic: bool = True,
) -> RouteLeg:
    ck = (round(origin[0], 3), round(origin[1], 3), round(destination[0], 3), round(destination[1], 3), traffic)
    if ck in _route_cache:
        return _route_cache[ck]

    s = get_settings()
    if not s.maps_live:
        leg = _fallback_leg(origin, destination)
        _route_cache[ck] = leg
        return leg

    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL" if traffic else "TRAFFIC_UNAWARE",
        "computeAlternativeRoutes": False,
        "languageCode": "en-IN",
        "units": "METRIC",
    }
    headers = {
        "X-Goog-Api-Key": s.maps_api_key or "",
        # Field mask is mandatory on Routes v2 and keeps the response tiny/fast.
        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline",
        "Content-Type": "application/json",
    }

    async def call() -> RouteLeg:
        r = await client().post(ROUTES_URL, json=body, headers=headers)
        r.raise_for_status()
        routes = (r.json() or {}).get("routes") or []
        if not routes:
            raise RuntimeError("routes.empty")
        top = routes[0]
        secs = float(str(top.get("duration", "0s")).rstrip("s") or 0)
        return RouteLeg(
            origin=origin,
            destination=destination,
            distance_km=float(top.get("distanceMeters", 0)) / 1000.0,
            duration_minutes=secs / 60.0,
            polyline=(top.get("polyline") or {}).get("encodedPolyline"),
            source="google-routes-v2",
            traffic_aware=traffic,
        )

    async def fb() -> RouteLeg:
        return _fallback_leg(origin, destination)

    leg = await resilient("maps.routes", call, fallback=fb)
    _route_cache[ck] = leg
    return leg


async def route_matrix(
    origins: Iterable[tuple[float, float]],
    destination: tuple[float, float],
) -> list[RouteLeg]:
    """Compare many origins to one destination.

    One `computeRouteMatrix` call replaces N `computeRoutes` calls - this is the
    single biggest latency win in the warehouse-selection stage.
    """
    origins = list(origins)
    if not origins:
        return []

    s = get_settings()
    if not s.maps_live:
        return [_fallback_leg(o, destination) for o in origins]

    body = {
        "origins": [
            {"waypoint": {"location": {"latLng": {"latitude": o[0], "longitude": o[1]}}}}
            for o in origins
        ],
        "destinations": [
            {"waypoint": {"location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}}}
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    headers = {
        "X-Goog-Api-Key": s.maps_api_key or "",
        "X-Goog-FieldMask": "originIndex,destinationIndex,distanceMeters,duration,condition",
        "Content-Type": "application/json",
    }

    async def call() -> list[RouteLeg]:
        r = await client().post(MATRIX_URL, json=body, headers=headers)
        r.raise_for_status()
        elements = r.json()
        legs: list[RouteLeg | None] = [None] * len(origins)
        for el in elements:
            idx = int(el.get("originIndex", 0))
            if el.get("condition") == "ROUTE_NOT_FOUND":
                continue
            secs = float(str(el.get("duration", "0s")).rstrip("s") or 0)
            legs[idx] = RouteLeg(
                origin=origins[idx],
                destination=destination,
                distance_km=float(el.get("distanceMeters", 0)) / 1000.0,
                duration_minutes=secs / 60.0,
                polyline=None,
                source="google-routes-matrix",
                traffic_aware=True,
            )
        return [legs[i] or _fallback_leg(origins[i], destination) for i in range(len(origins))]

    async def fb() -> list[RouteLeg]:
        return [_fallback_leg(o, destination) for o in origins]

    return await resilient("maps.matrix", call, fallback=fb)


def static_map_url(polyline: str | None, origin, destination) -> str | None:
    """A keyless URL on OUR origin that renders the route.

    The obvious implementation builds a maps.googleapis.com URL with `key=` in
    the query string - but that URL ends up in the shipment JSON returned to
    every API caller, handing the Maps key to anyone who places an order. The
    key never leaves the server now: this returns an internal path that
    `/api/map` proxies, and the browser only ever sees our own domain.
    """
    s = get_settings()
    if not s.maps_api_key:
        return None
    from urllib.parse import urlencode

    q = {"o": f"{origin[0]},{origin[1]}", "d": f"{destination[0]},{destination[1]}"}
    if polyline:
        q["p"] = polyline
    return f"/api/map?{urlencode(q)}"


def build_static_map_request(origin: str, destination: str,
                             polyline: str | None) -> tuple[str, dict[str, str]] | None:
    """Server-side only: the real Google Static Maps URL, key included."""
    s = get_settings()
    if not s.maps_api_key:
        return None
    parts = [
        "size=640x360",
        "scale=2",
        f"markers=color:blue%7Clabel:W%7C{origin}",
        f"markers=color:red%7Clabel:D%7C{destination}",
    ]
    if polyline:
        from urllib.parse import quote

        parts.append(f"path=weight:4%7Ccolor:0x2563ebff%7Cenc:{quote(polyline, safe='')}")
    parts.append(f"key={s.maps_api_key}")
    return f"https://maps.googleapis.com/maps/api/staticmap?{'&'.join(parts)}", {}


# ================================================================================
# Production Google Maps Platform surface
# ================================================================================

ADDRESS_VALIDATION_URL = "https://addressvalidation.googleapis.com/v1:validateAddress"

# Capability probe results, filled at boot. A disabled API must not cost us a
# failed request on every order, so we learn once and route around it.
_api_enabled: dict[str, bool] = {}


def api_enabled(name: str) -> bool:
    return _api_enabled.get(name, True)


def set_api_enabled(name: str, ok: bool) -> None:
    _api_enabled[name] = ok


@dataclass
class ValidatedAddress:
    formatted: str
    lat: float
    lng: float
    complete: bool
    plus_code: str | None
    missing_components: list[str]
    unconfirmed_components: list[str]
    deliverable: str          # "deliverable" | "needs_review" | "undeliverable"
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def validate_address(address: str) -> ValidatedAddress:
    """Google Address Validation API - catch bad addresses *before* dispatch.

    An undeliverable address found at the depot costs a failed delivery, a
    return leg and a refund. Found at intake it costs one API call. This is the
    cheapest risk reduction in the whole pipeline, so it runs on every order.

    Degrades to Geocoding (and then to the city table) when the API is not
    enabled on the project.
    """
    s = get_settings()

    async def degrade() -> ValidatedAddress:
        lat, lng, formatted = await geocode(address)
        unresolved = "unresolved" in formatted
        return ValidatedAddress(
            formatted=formatted,
            lat=lat,
            lng=lng,
            complete=not unresolved,
            plus_code=None,
            missing_components=["street_number"] if unresolved else [],
            unconfirmed_components=[],
            deliverable="needs_review" if unresolved else "deliverable",
            source="geocoding-fallback",
        )

    if not s.maps_live or not api_enabled("address_validation"):
        return await degrade()

    body = {
        "address": {"regionCode": "IN", "addressLines": [address]},
        "enableUspsCass": False,
    }

    async def call() -> ValidatedAddress:
        r = await client().post(
            ADDRESS_VALIDATION_URL, params={"key": s.maps_api_key}, json=body
        )
        if r.status_code == 403:
            set_api_enabled("address_validation", False)
            raise RuntimeError("address_validation_disabled")
        r.raise_for_status()
        result = (r.json() or {}).get("result") or {}
        verdict = result.get("verdict") or {}
        addr = result.get("address") or {}
        geo = result.get("geocode") or {}
        loc = geo.get("location") or {}

        missing = list(addr.get("missingComponentTypes") or [])
        unconfirmed = list(addr.get("unconfirmedComponentTypes") or [])
        complete = bool(verdict.get("addressComplete"))
        granularity = verdict.get("validationGranularity", "OTHER")

        if complete and granularity in ("PREMISE", "SUB_PREMISE", "PREMISE_PROXIMITY"):
            deliverable = "deliverable"
        elif granularity in ("ROUTE", "BLOCK"):
            deliverable = "needs_review"
        else:
            deliverable = "undeliverable" if missing else "needs_review"

        return ValidatedAddress(
            formatted=addr.get("formattedAddress") or address,
            lat=float(loc.get("latitude", 0.0)),
            lng=float(loc.get("longitude", 0.0)),
            complete=complete,
            plus_code=(geo.get("plusCode") or {}).get("globalCode"),
            missing_components=missing,
            unconfirmed_components=unconfirmed,
            deliverable=deliverable,
            source="google-address-validation",
        )

    out = await resilient("maps.address_validation", call, fallback=degrade)
    # A zero location from the validator is unusable; recover coordinates.
    if not out.lat or not out.lng:
        lat, lng, _f = await geocode(address)
        out = ValidatedAddress(**{**out.to_dict(), "lat": lat, "lng": lng})
    return out


@dataclass
class MultiDropRoute:
    """An optimised last-mile run: one vehicle, many drops."""

    ordered_stops: list[int]          # indices into the input list, in drive order
    total_distance_km: float
    total_duration_minutes: float
    legs: list[dict[str, Any]]
    polyline: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_distance_km"] = round(self.total_distance_km, 2)
        d["total_duration_minutes"] = round(self.total_duration_minutes, 1)
        return d


async def optimize_multidrop(
    depot: tuple[float, float],
    stops: list[tuple[float, float]],
    *,
    return_to_depot: bool = True,
) -> MultiDropRoute:
    """Routes API v2 with `optimizeWaypointOrder` - a real TSP solve by Google.

    This is the actual "shortest path" problem in logistics: not depot->customer,
    but depot->N customers->depot in the cheapest order. Google solves it against
    live traffic; our fallback is nearest-neighbour + 2-opt, which is within a
    few percent on small N and costs microseconds.
    """
    if not stops:
        return MultiDropRoute([], 0.0, 0.0, [], None, "empty")

    s = get_settings()
    if not s.maps_live or len(stops) > 25:
        return _fallback_multidrop(depot, stops, return_to_depot)

    body = {
        "origin": {"location": {"latLng": {"latitude": depot[0], "longitude": depot[1]}}},
        "destination": {
            "location": {
                "latLng": {
                    "latitude": (depot if return_to_depot else stops[-1])[0],
                    "longitude": (depot if return_to_depot else stops[-1])[1],
                }
            }
        },
        "intermediates": [
            {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}
            for p in (stops if return_to_depot else stops[:-1])
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "optimizeWaypointOrder": True,
        "languageCode": "en-IN",
        "units": "METRIC",
    }
    headers = {
        "X-Goog-Api-Key": s.maps_api_key or "",
        "X-Goog-FieldMask": (
            "routes.optimizedIntermediateWaypointIndex,routes.distanceMeters,"
            "routes.duration,routes.polyline.encodedPolyline,"
            "routes.legs.distanceMeters,routes.legs.duration"
        ),
        "Content-Type": "application/json",
    }

    async def call() -> MultiDropRoute:
        r = await client().post(ROUTES_URL, json=body, headers=headers)
        r.raise_for_status()
        routes = (r.json() or {}).get("routes") or []
        if not routes:
            raise RuntimeError("multidrop.empty")
        top = routes[0]
        order = list(top.get("optimizedIntermediateWaypointIndex") or range(len(stops)))
        secs = float(str(top.get("duration", "0s")).rstrip("s") or 0)
        legs = [
            {
                "distance_km": round(float(l.get("distanceMeters", 0)) / 1000.0, 2),
                "duration_minutes": round(
                    float(str(l.get("duration", "0s")).rstrip("s") or 0) / 60.0, 1
                ),
            }
            for l in (top.get("legs") or [])
        ]
        return MultiDropRoute(
            ordered_stops=order,
            total_distance_km=float(top.get("distanceMeters", 0)) / 1000.0,
            total_duration_minutes=secs / 60.0,
            legs=legs,
            polyline=(top.get("polyline") or {}).get("encodedPolyline"),
            source="google-routes-v2-optimized",
        )

    async def fb() -> MultiDropRoute:
        return _fallback_multidrop(depot, stops, return_to_depot)

    return await resilient("maps.multidrop", call, fallback=fb)


def _fallback_multidrop(depot, stops, return_to_depot) -> MultiDropRoute:
    """Nearest-neighbour construction followed by 2-opt improvement."""
    n = len(stops)
    remaining = set(range(n))
    order: list[int] = []
    cur = depot
    while remaining:
        nxt = min(remaining, key=lambda i: haversine_km(cur, stops[i]))
        order.append(nxt)
        remaining.discard(nxt)
        cur = stops[nxt]

    def tour_km(seq: list[int]) -> float:
        pts = [depot] + [stops[i] for i in seq] + ([depot] if return_to_depot else [])
        return sum(haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

    improved = True
    best = tour_km(order)
    while improved and n > 3:
        improved = False
        for i in range(n - 1):
            for j in range(i + 2, n):
                cand = order[:i + 1] + order[i + 1:j + 1][::-1] + order[j + 1:]
                c = tour_km(cand)
                if c < best - 1e-9:
                    order, best, improved = cand, c, True
    road = best * 1.27
    pts = [depot] + [stops[i] for i in order] + ([depot] if return_to_depot else [])
    legs = [
        {
            "distance_km": round(haversine_km(pts[i], pts[i + 1]) * 1.27, 2),
            "duration_minutes": round(haversine_km(pts[i], pts[i + 1]) * 1.27 / 48.0 * 60, 1),
        }
        for i in range(len(pts) - 1)
    ]
    return MultiDropRoute(
        ordered_stops=order,
        total_distance_km=road,
        total_duration_minutes=road / 48.0 * 60.0,
        legs=legs,
        polyline=None,
        source="nearest-neighbour-2opt-fallback",
    )


async def probe_apis() -> dict[str, Any]:
    """Boot-time capability probe. We find out what the key can do once, at
    startup, instead of discovering it per-request under load."""
    s = get_settings()
    report: dict[str, Any] = {"maps_key_present": bool(s.maps_api_key)}
    if not s.maps_live:
        return {**report, "mode": "mock"}

    async def t_geocode() -> bool:
        try:
            r = await client().get(
                GEOCODE_URL, params={"address": "Chennai", "key": s.maps_api_key}
            )
            return (r.json() or {}).get("status") == "OK"
        except Exception:
            return False

    async def t_routes() -> bool:
        try:
            leg = await compute_route((13.0827, 80.2707), (12.9716, 77.5946), traffic=False)
            return leg.source.startswith("google")
        except Exception:
            return False

    async def t_addrval() -> bool:
        try:
            r = await client().post(
                ADDRESS_VALIDATION_URL,
                params={"key": s.maps_api_key},
                json={"address": {"regionCode": "IN", "addressLines": ["Chennai 600040"]}},
            )
            return r.status_code == 200
        except Exception:
            return False

    geo, rt, av = await asyncio.gather(t_geocode(), t_routes(), t_addrval())
    set_api_enabled("address_validation", av)
    report.update({"geocoding": geo, "routes_v2": rt, "address_validation": av, "mode": "live"})
    return report
