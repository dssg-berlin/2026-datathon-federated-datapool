"""SPARQL client for the federated directory of social support services (SoSuSe).

Every query lives in its own file in sparql_queries/; load_query() fills its {{placeholders}}
and SparqlClient runs it and shapes the rows into the result of one MCP tool. See QUERIES.md.
"""

import csv
import functools
import json
import math
import os
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


DEFAULT_ENDPOINT = "https://sosuse.correlaid.org/directory/sparql"

QUERY_DIR = Path(__file__).resolve().parent.parent / "sparql_queries"

# Inhabitants per postal code. The file spreads a municipality's population evenly over its
# postal codes (all 194 Berlin codes have 18996), so per-code figures are estimates.
INHABITANTS_FILE = Path(__file__).resolve().parent.parent / "data" / "einwohner_plz_de.csv"

INHABITANTS_NOTE = (
    "Inhabitants are estimates: the census file spreads each municipality's population "
    "evenly over its postal codes. Compare municipalities rather than single codes of one city."
)

POSTAL_CODE_ORDERS = ("most_facilities", "highest_per_10k", "lowest_per_10k")

MAX_POSTAL_CODE_RESULTS = 100

# Regions are postal code prefixes: 1 digit is a zone (0-9), 2 digits a region (about 95,
# e.g. 48 = Münster), 3 digits an area (about 680).
REGION_LEVELS = {"zone": 1, "region": 2, "area": 3}

# Towns listed per region to name it.
PLACES_PER_REGION = 3

# Regions with fewer inhabitants are not ranked: they are fragments such as the area 256
# (2,660 inhabitants) whose ratio says little.
MIN_REGION_INHABITANTS = 10_000

REGION_NOTE = (
    "Facilities are what the directory lists, so a low density can also mean that providers "
    "of that region publish less data. Inhabitants are census estimates summed over the "
    "region's postal codes. Places are the towns with the most facilities in the region. "
    "relative_to_average is the region's density divided by the average of all regions shown."
)

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

FACILITY_IRI_PREFIX = "https://civic-data.de/federated-directory#"

COORDINATES = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

POSTAL_CODE = re.compile(r"^\d{5}$")

EARTH_RADIUS_KM = 6371.0

# find_nearby searches at most this far, so a bounding-box query stays small.
MAX_RADIUS_KM = 50.0

MAX_NEARBY_RESULTS = 50

NAMESPACES = {
    "schema:": "http://schema.org/",
    "org:": "http://www.w3.org/ns/org#",
    "foaf:": "http://xmlns.com/foaf/0.1/",
    "cdf:": "https://civic-data.de/federated-directory#",
    "dct:": "http://purl.org/dc/terms/",
}

# Dataset name -> RDF class, duplicates query and the columns that completeness and
# duplicate checks look at. The column lists are the headers of the original CSV export
# in data/ (kept in sync by tests/unit/test_sparql_parity.py).
DATASETS = {
    "einrichtung": {
        "class": "org:OrganizationalUnit",
        "duplicates_query": "list_duplicate_facilities",
        "columns": [
            "iri", "schema:name", "schema:alternateName", "schema:telephone",
            "schema:email", "foaf:homepage", "schema:mainEntityOfPage",
            "schema:category", "schema:availableService", "schema:audience",
            "schema:accessibilitySummary", "schema:openingHours",
            "schema:parentOrganization", "cdf:parentOrgClassification",
            "schema:address",
        ],
    },
    "adresse": {
        "class": "schema:PostalAddress",
        "duplicates_query": "list_duplicate_addresses",
        "columns": [
            "iri", "schema:streetAddress", "schema:postalCode",
            "schema:addressLocality", "schema:addressCountry",
            "schema:latitude", "schema:longitude",
        ],
    },
    "angebot": {
        "class": "schema:Service",
        "duplicates_query": "list_duplicate_services",
        "columns": [
            "iri", "schema:name", "schema:category", "schema:codeValue",
            "schema:audience", "schema:areaServed", "schema:provider",
        ],
    },
    "traeger": {
        "class": "schema:Organization",
        "duplicates_query": "list_duplicate_providers",
        "columns": ["iri", "schema:name", "foaf:homepage", "schema:address"],
    },
}

# Fields explain_provenance can explain: name in the answer -> property.
PROVENANCE_FIELDS = {
    "name": "schema:name",
    "alternate_name": "schema:alternateName",
    "telephone": "schema:telephone",
    "email": "schema:email",
    "homepage": "foaf:homepage",
    "opening_hours": "schema:openingHours",
    "detail_page": "schema:mainEntityOfPage",
    "category": "schema:category",
    "audience": "schema:audience",
    "accessibility": "schema:accessibilitySummary",
    "area_served": "schema:areaServed",
    "street": "schema:streetAddress",
    "postal_code": "schema:postalCode",
    "city": "schema:addressLocality",
    "description": "schema:description",
    "services": "schema:availableService",
}

# Long free text: only explained when asked for by name, to keep answers short.
LONG_TEXT_FIELDS = {"description", "services"}

# A directory entity IRI, or its local ID such as "org-00416a783a9b".
ENTITY_IRI = re.compile(r"https://civic-data\.de/federated-directory#[A-Za-z0-9_-]+")
ENTITY_ID = re.compile(r"(?:org|service|traeger|addr)-[0-9a-f]+")

PROVENANCE_LIMITS = [
    "Sources are the records the directory pipeline harvested for this entry (list_sources "
    "has the harvest dates). Values are shown as harvested, not checked as current.",
    "A value without sources was not supplied as-is by this entry's own records: it may be "
    "inherited from a parent organisation (inherited_from), corrected, combined or geocoded. "
    "No source found does not mean the value has no origin.",
    "Several sources describing one entry do not confirm every value, and a match between "
    "records can be wrong.",
]

SOURCE_NOTES = [
    "source_records counts every mapped record of a source: facilities, services, "
    "addresses and organisations.",
    "harvested_at is when the pipeline fetched the source. For a static file it is when the "
    "file was copied, and static_file_committed_at is its repository date; neither proves "
    "when the contents were collected.",
    "Coverage is the area a source declares, not a promise of completeness: no result in an "
    "area does not mean that no support exists there.",
]


# Help needs for find_social_help: need -> German search terms (casefolded). The LLM maps the
# user's request, in any language, to one of these keys; the terms are matched in the German
# service names and categories of the directory.
HELP_NEEDS = {
    "debt": ["schuldner", "insolvenz"],
    "housing": ["wohnungsnot", "obdach", "wohnhilfe"],
    "family": ["familie", "kinder", "jugend"],
    "elderly": ["senioren", "pflege", "sozialstation"],
    "addiction": ["sucht", "drogen", "alkohol"],
    "migration": ["migration", "zugewandert", "flucht"],
}


# ---- Entity cards and linked search (get_entity_card, search_service_candidates,
# search_directory). Ported from the datathon "directory-search" contribution, which
# ran on the CSV sample; here they read the live directory. -------------------------

# RDF class -> entity type name used in the tool results.
ENTITY_TYPES = {
    "org:OrganizationalUnit": "facility",
    "schema:Service": "service",
    "schema:Organization": "provider",
    "schema:PostalAddress": "address",
}

# Entity type -> {result field: property}. Every field is reported, None when not recorded.
ENTITY_FIELDS = {
    "facility": {
        "name": "schema:name",
        "alternate_name": "schema:alternateName",
        "description": "schema:description",
        "category": "schema:category",
        "service_description": "schema:availableService",
        "audience": "schema:audience",
        "telephone": "schema:telephone",
        "email": "schema:email",
        "homepage": "foaf:homepage",
        "source_page": "schema:mainEntityOfPage",
        "opening_hours": "schema:openingHours",
        "accessibility": "schema:accessibilitySummary",
        "provider_classification": "cdf:parentOrgClassification",
        "last_modified": "dct:modified",
    },
    "service": {
        "name": "schema:name",
        "category": "schema:category",
        "code": "schema:codeValue",
        "audience": "schema:audience",
        "area_served": "schema:areaServed",
        "opening_hours": "schema:openingHours",
    },
    "provider": {
        "name": "schema:name",
        "homepage": "foaf:homepage",
    },
    "address": {
        "street": "schema:streetAddress",
        "postal_code": "schema:postalCode",
        "locality": "schema:addressLocality",
        "country": "schema:addressCountry",
        "latitude": "schema:latitude",
        "longitude": "schema:longitude",
    },
}

# Entity type -> {relationship: (property, allowed target types)}.
ENTITY_LINKS = {
    "facility": {
        "parent_organization": ("schema:parentOrganization", ("provider",)),
        "address": ("schema:address", ("address",)),
    },
    "service": {
        "provider": ("schema:provider", ("facility", "provider")),
    },
    "provider": {
        "address": ("schema:address", ("address",)),
    },
    "address": {},
}

# Linked entities are expanded this many levels below the requested one
# (service -> facility -> address/provider).
MAX_CARD_DEPTH = 2

MAX_LINKED_SERVICES = 50

MAX_PAGE_SIZE = 25

# The SPARQL prefilter of search_directory needs one term of at least this length, so a
# search never scans the whole directory for a one- or two-letter substring.
MIN_DIRECTORY_TERM_LENGTH = 3

# search_directory counts these fields as missing across all matches.
DIRECTORY_MISSING_FIELDS = ("telephone", "email", "opening_hours", "accessibility")

PHONE_STATUSES = ("missing", "scientific_notation", "no_leading_zero", "as_recorded")


def present(value: str | None) -> str | None:
    """Keep non-blank values unchanged; whitespace-only values count as not recorded."""

    return value if value is not None and value.strip() else None


def telephone_quality(value: str | None) -> dict:
    """
    Classify a stored telephone value by syntax only. Nothing is repaired or verified:
    a number that looks fine may still be wrong, and none is dialled.
    """

    first = (split_values(value) or [None])[0]

    if first is None:
        return {"status": "missing", "verified": False}

    digits = first.strip()

    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+", digits):
        return {
            "status": "scientific_notation",
            "verified": False,
            "note": "Stored as a number in scientific notation; do not reconstruct it.",
        }

    if digits.isdigit() and not digits.startswith("0"):
        return {
            "status": "no_leading_zero",
            "verified": False,
            "note": "Digits only and no leading 0: the area code may be incomplete. "
            "Copy it as stored; never add a 0 or a country code.",
        }

    return {"status": "as_recorded", "verified": False}


def url_quality(value: str | None) -> dict:
    """Classify a stored URL by syntax only. An absolute http(s) URL is not checked live."""

    first = (split_values(value) or [None])[0]

    if first is None:
        return {"status": "missing", "verified": False}

    if any(character.isspace() for character in first):
        return {"status": "malformed", "verified": False}

    try:
        parsed = urllib.parse.urlsplit(first)
    except ValueError:
        return {"status": "malformed", "verified": False}

    if not parsed.scheme:
        status = "relative_unresolved" if first.startswith(("/", "#", "?")) else "missing_scheme"
    elif parsed.scheme not in ("http", "https"):
        status = "unsupported_scheme"
    elif not parsed.hostname:
        status = "malformed"
    else:
        status = "absolute_http_unverified"

    return {"status": status, "verified": False}


def valid_entity_iri(iri: str) -> bool:
    """True for an IRI under cdf: that is safe to put into IRI("...") in a query."""

    return iri.startswith(FACILITY_IRI_PREFIX) and not re.search(r"[\s<>\"{}|\\^`]", iri)


def clamp_page(limit, offset, default_limit: int) -> tuple[int, int]:
    """Page size within 1..MAX_PAGE_SIZE and a non-negative offset."""

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default_limit

    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0

    return min(max(limit, 1), MAX_PAGE_SIZE), max(offset, 0)


def page_info(total: int, limit: int, offset: int, returned: int) -> dict:
    """Paging fields shared by the search tools."""

    end = offset + returned

    return {
        "total": total,
        "returned": returned,
        "limit": limit,
        "offset": offset,
        "next_offset": end if end < total else None,
        "truncated": returned < total,
    }


def escape_literal(value: str) -> str:
    """Escape a Python string for use inside a double-quoted SPARQL literal."""

    replacements = {
        "\\": "\\\\",
        '"': '\\"',
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }

    return "".join(replacements.get(char, char) for char in value)


def expand(prefixed: str) -> str:
    """Expand a prefixed name such as "schema:name" to a full IRI."""

    for prefix, namespace in NAMESPACES.items():
        if prefixed.startswith(prefix):
            return namespace + prefixed[len(prefix):]

    return prefixed


def column_variable(column: str) -> str:
    """Variable name a duplicates query uses for a column ("schema:postalCode" -> "postal_code")."""

    local = column.split(":", 1)[1]

    return re.sub(r"(?<!^)(?=[A-Z])", "_", local).lower()


def rows(result: dict) -> list[dict]:
    """
    Flatten SPARQL JSON bindings to {variable: value} dicts.

    Unbound variables and empty GROUP_CONCAT results become None.
    """

    return [
        {
            variable: (binding[variable]["value"] or None)
            if variable in binding
            else None
            for variable in result["head"]["vars"]
        }
        for binding in result["results"]["bindings"]
    ]


def percentage(part: int, whole: int) -> float:
    """part / whole in percent, rounded to 2 decimals (0.0 when whole is 0)."""

    return round(part / whole * 100, 2) if whole else 0.0


def to_float(value: str | None) -> float | None:
    """Convert a coordinate literal to float (None stays None)."""

    return float(value) if value is not None else None


def split_values(value: str | None) -> list[str]:
    """Split a "; "-joined GROUP_CONCAT value into a list (None -> [])."""

    return [part.strip() for part in (value or "").split(";") if part.strip()]


def casefold_de(text: str) -> str:
    """Casefold like the queries do: lower case and ß -> ss."""

    return text.casefold().replace("ß", "ss")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""

    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )

    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


def bounding_box(latitude: float, longitude: float, radius_km: float) -> str:
    """
    The "south,west,north,east" box around a point that contains every point within
    radius_km, as list_facilities_by_bbox.rq expects it.
    """

    lat_delta = math.degrees(radius_km / EARTH_RADIUS_KM)
    lon_delta = lat_delta / max(math.cos(math.radians(latitude)), 1e-6)

    # Round outward to 6 decimals so the box never cuts into the circle.
    def down(value: float) -> str:
        return f"{math.floor(value * 1e6) / 1e6:.6f}"

    def up(value: float) -> str:
        return f"{math.ceil(value * 1e6) / 1e6:.6f}"

    return ",".join(
        [
            down(latitude - lat_delta),
            down(longitude - lon_delta),
            up(latitude + lat_delta),
            up(longitude + lon_delta),
        ]
    )


def accessibility_flag(summary: str | None) -> bool | None:
    """Read the accessibility sentence as True, False, or None when unknown."""

    text = (summary or "").casefold()

    if "nicht barrierefrei" in text:
        return False

    if "barrierefrei" in text:
        return True
def entity_iri(text: str) -> str | None:
    """
    Full directory IRI for an entity IRI or local ID ("org-00416a783a9b").

    None means the text is not an IRI (it is a name). Other IRI-like text raises
    ValueError, so it never reaches a query.
    """

    text = text.strip()

    if ENTITY_IRI.fullmatch(text):
        return text

    local = text.removeprefix("cdf:")
    if ENTITY_ID.fullmatch(local):
        return NAMESPACES["cdf:"] + local

    if re.match(r"[a-z][a-z0-9+.-]*://|cdf:", text, re.IGNORECASE):
        raise ValueError(
            f"{text!r} is not a directory IRI: expected "
            "https://civic-data.de/federated-directory#<id> or an ID such as org-00416a783a9b"
        )

    return None


def source_page(value: str | None) -> str | None:
    """
    The facility's page in its source directory.

    Older Caritas records store a relative redirect ("/search.ashx?...&u=<url>"),
    so the real URL is taken from its "u" parameter.
    """

    if not value:
        return None

    if value.startswith(("http://", "https://")):
        return value

    if value.startswith("/search.ashx"):
        target = urllib.parse.parse_qs(urllib.parse.urlparse(value).query).get("u", [""])[0]
        return target.split("?")[0] or None

    return None


def map_link(latitude: float | None, longitude: float | None, fallback: str) -> str | None:
    """Google Maps link for coordinates, or a text search when they are missing."""

    query = f"{latitude},{longitude}" if latitude is not None and longitude is not None else fallback

    if not query:
        return None

    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query)


def format_address(street: str | None, postal_code: str | None, city: str | None) -> str | None:
    """ "Street, Postal code City", leaving out missing parts."""

    locality = " ".join(part for part in [postal_code, city] if part)

    return ", ".join(part for part in [street, locality] if part) or None


def matches_terms(texts: list[str | None], query: str | None) -> bool:
    """
    True when any comma-separated term in query appears in the texts.

    Matching is a casefolded substring, like the queries. An empty query matches.
    """

    terms = [casefold_de(term.strip()) for term in (query or "").split(",") if term.strip()]

    if not terms:
        return True

    text = casefold_de(" ".join(value for value in texts if value))

    return any(term in text for term in terms)


@functools.cache
def load_inhabitants() -> dict[str, int]:
    """Read data/einwohner_plz_de.csv into {postal code: inhabitants}."""

    with open(INHABITANTS_FILE, newline="", encoding="utf-8") as f:
        return {row["plz"]: int(row["population"]) for row in csv.DictReader(f)}


def per_10k(facilities: int, inhabitants: int | None) -> float | None:
    """Facilities per 10,000 inhabitants; None when the population is unknown or zero."""

    if not inhabitants:
        return None

    return round(facilities / inhabitants * 10_000, 2)


def rank_by_order(rows: list[dict], order: str) -> list[dict]:
    """Sort postal code or region rows for one of POSTAL_CODE_ORDERS."""

    if order == "most_facilities":
        return sorted(rows, key=lambda row: -row["facilities"])

    # Rows without a known population have no density and are left out.
    ranked = [row for row in rows if row["facilities_per_10k_inhabitants"] is not None]
    if order == "highest_per_10k":
        ranked.sort(key=lambda row: -row["facilities_per_10k_inhabitants"])
    else:
        # Fewest facilities per head first; among equals, the most inhabitants first.
        ranked.sort(key=lambda row: (row["facilities_per_10k_inhabitants"], -row["inhabitants"]))

    return ranked
def provenance_status(values: list[dict]) -> str:
    """How the sources of one field relate to each other (status in explain_provenance)."""

    by_source = {}
    for value in values:
        for source in value["sources"]:
            by_source.setdefault(source, set()).add(value["value"])

    if not by_source:
        return "not_from_own_sources"
    if len(by_source) == 1:
        return "single_source"
    if len({frozenset(supplied) for supplied in by_source.values()}) == 1:
        return "sources_agree"
    return "sources_differ"


def same_ignoring_case(values: list[str]) -> bool:
    """True when the values only differ in upper/lower case or spacing."""

    return len({" ".join(value.casefold().split()) for value in values}) == 1


def load_query(query_name: str, /, **params: str) -> str:
    """
    Load sparql_queries/<query_name>.rq and fill its {{placeholders}}.

    Values are escaped as SPARQL string literals.
    """

    template = (QUERY_DIR / f"{query_name}.rq").read_text(encoding="utf-8")

    missing = sorted(set(PLACEHOLDER.findall(template)) - set(params))
    if missing:
        raise ValueError(f"query '{query_name}' needs parameter(s) {missing}")

    return PLACEHOLDER.sub(
        lambda match: escape_literal(str(params[match.group(1)])),
        template,
    )


class SparqlClient:
    """Query the federated directory of social support services over SPARQL."""

    def __init__(self, endpoint: str | None = None, timeout: float = 30):
        self.endpoint = endpoint or os.environ.get(
            "SPARQL_ENDPOINT", DEFAULT_ENDPOINT
        )
        self.timeout = timeout

    def query(self, sparql: str) -> dict:
        """Run a SPARQL query and return the parsed JSON result."""

        url = self.endpoint + "?" + urllib.parse.urlencode({"query": sparql})
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/sparql-results+json"},
        )

        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def select(self, query_name: str, /, **params: str) -> list[dict]:
        """Run sparql_queries/<query_name>.rq and return its rows."""

        return rows(self.query(load_query(query_name, **params)))

    def value_counts(self, query_name: str) -> dict:
        """Run a count_*_per_* query and return {value: count}."""

        return {
            row["value"]: int(row["count"])
            for row in self.select(query_name)
        }

    def count_facilities(self, city: str) -> int:
        """Count facilities in a city."""

        result = self.select("count_facilities_by_city", city=city.casefold())

        return int(result[0]["count"]) if result else 0

    def search_facilities(self, city: str) -> list[dict]:
        """Find facilities in a city."""

        return [
            {
                "iri": row["facility"],
                "name": row["name"],
                "street": row["street"],
                "postal_code": row["postal_code"],
                "city": row["city"],
                "telephone": row["telephone"],
                "email": row["email"],
                "homepage": row["homepage"],
            }
            for row in self.select("list_facilities_by_city", city=city.casefold())
        ]

    def get_facility_details(self, name: str) -> list[dict]:
        """Find facilities by name and return their details."""

        results = []

        for row in self.select("list_facility_details_by_name", name=name.casefold()):
            result = {
                "iri": row["facility"],
                "name": row["name"],
                "alternate_name": row["alternate_name"],
                "telephone": row["telephone"],
                "email": row["email"],
                "homepage": row["homepage"],
                "category": row["category"],
                "service_description": row["service_description"],
            }

            if row["address"]:
                result["address"] = {
                    "street": row["street"],
                    "postal_code": row["postal_code"],
                    "city": row["city"],
                    "country": row["country"],
                    "latitude": to_float(row["latitude"]),
                    "longitude": to_float(row["longitude"]),
                }

            if row["provider"]:
                result["provider"] = {
                    "iri": row["provider"],
                    "name": row["provider_name"],
                    "homepage": row["provider_homepage"],
                }

            results.append(result)

        return results

    def search_services(self, query: str) -> list[dict]:
        """Search the structured service catalogue."""

        return [
            {
                "iri": row["service"],
                "schema:name": row["name"],
                "schema:category": row["category"],
                "schema:audience": row["audience"],
            }
            for row in self.select("list_services_by_query", query=query.casefold())
        ]

    def find_help(self, need: str, limit: int = 5, postal_code_prefix: str = "") -> dict:
        """Find services for a need (one of HELP_NEEDS), with where to go and how to call."""

        need = need.strip().casefold()
        if need not in HELP_NEEDS:
            return {
                "error": f"Unknown need '{need}'.",
                "valid_needs": list(HELP_NEEDS),
            }

        limit = max(1, min(int(limit), 10))
        prefix = postal_code_prefix.strip()

        found = self.select("list_help_services_by_terms", terms="|".join(HELP_NEEDS[need]))
        if prefix:
            found = [row for row in found if (row["postal_code"] or "").startswith(prefix)]

        # Entries with a phone number first: a person in need can act on those.
        found.sort(key=lambda row: row["telephone"] is None)

        return {
            "need": need,
            "total_matches": len(found),
            "showing": min(limit, len(found)),
            "results": [
                {
                    "service": row["name"],
                    "category": row["category"],
                    "facility": row["facility_name"],
                    "street": row["street"],
                    "postal_code": row["postal_code"],
                    "city": row["city"],
                    "telephone": row["telephone"],
                    "email": row["email"],
                }
                for row in found[:limit]
            ],
        }

    def detect_duplicates(self) -> dict:
        """
        Detect duplicate entities in all datasets.

        A duplicate group is two or more entities whose values are
        identical in every column except the IRI.
        """

        duplicates = {}

        for dataset_name, dataset in DATASETS.items():
            groups = [
                {
                    "occurrences": int(row["occurrences"]),
                    "iris": row["iris"].split(" "),
                    "row": {
                        column: row[column_variable(column)]
                        for column in dataset["columns"][1:]
                    },
                }
                for row in self.select(dataset["duplicates_query"])
            ]

            if groups:
                duplicates[dataset_name] = groups

        return duplicates

    def dataset_sizes(self) -> dict:
        """Number of entities per dataset."""

        counts = {
            row["class"]: int(row["count"])
            for row in self.select("count_entities_per_class")
        }

        return {
            dataset_name: counts.get(expand(dataset["class"]), 0)
            for dataset_name, dataset in DATASETS.items()
        }

    def check_completeness(self) -> dict:
        """
        Check data completeness for all datasets.

        A value is missing when the property is absent or only whitespace.
        """

        sizes = self.dataset_sizes()

        filled = {
            (row["class"], row["property"]): int(row["filled"])
            for row in self.select("count_filled_properties_per_class")
        }

        completeness = {}

        for dataset_name, dataset in DATASETS.items():
            total_rows = sizes[dataset_name]
            class_iri = expand(dataset["class"])
            columns = {}

            for column in dataset["columns"]:
                if column == "iri":
                    missing = 0
                else:
                    missing = total_rows - filled.get((class_iri, expand(column)), 0)

                columns[column] = {
                    "total": total_rows,
                    "missing": missing,
                    "missing_percentage": percentage(missing, total_rows),
                }

            total_cells = total_rows * len(dataset["columns"])
            total_missing = sum(column["missing"] for column in columns.values())

            completeness[dataset_name] = {
                "rows": total_rows,
                "columns": len(dataset["columns"]),
                "total_cells": total_cells,
                "missing_cells": total_missing,
                "missing_percentage": percentage(total_missing, total_cells),
                "columns_detail": columns,
            }

        return completeness

    def generate_statistics(self) -> dict:
        """Generate statistics for the directory."""

        links = self.select("count_facility_links")[0]
        total = int(links["total"])
        with_address = int(links["with_address"])
        with_provider = int(links["with_provider"])

        return {
            "dataset_sizes": self.dataset_sizes(),
            "facilities_by_city": self.value_counts("count_facilities_per_city"),
            "facilities_by_category": self.value_counts("count_facilities_per_category"),
            "facilities_by_provider": self.value_counts("count_facilities_per_provider"),
            "facility_relationships": {
                "with_address": with_address,
                "without_address": total - with_address,
                "with_provider": with_provider,
                "without_provider": total - with_provider,
            },
            "services_by_category": self.value_counts("count_services_per_category"),
            "services_by_audience": self.value_counts("count_services_per_audience"),
        }

    def list_providers(self) -> list[dict]:
        """List all provider organisations."""

        return [
            {
                "iri": row["provider"],
                "name": row["name"],
                "homepage": row["homepage"],
                "address": {
                    "street": row["street"],
                    "postal_code": row["postal_code"],
                    "city": row["city"],
                },
            }
            for row in self.select("list_providers")
        ]

    def search_by_postal_code(self, postal_code: str) -> list[dict]:
        """Find facilities by German postal code."""

        return [
            {
                "iri": row["facility"],
                "name": row["name"],
                "street": row["street"],
                "postal_code": row["postal_code"],
                "city": row["city"],
                "telephone": row["telephone"],
                "email": row["email"],
                "homepage": row["homepage"],
            }
            for row in self.select("list_facilities_by_postal_code", postal_code=postal_code)
        ]

    def count_facilities_per_postal_code(
        self,
        postal_code_prefix: str = "",
        order: str = "most_facilities",
        limit: int = 20,
    ) -> dict:
        """
        Facilities per postal code, joined with inhabitants (data/einwohner_plz_de.csv).

        Covers every postal code of the census file plus every code that has facilities,
        so codes without any facility show up with 0.
        """

        order = order.strip().casefold()
        if order not in POSTAL_CODE_ORDERS:
            return {
                "error": f"Unknown order '{order}'.",
                "valid_orders": list(POSTAL_CODE_ORDERS),
            }

        limit = max(1, min(int(limit), MAX_POSTAL_CODE_RESULTS))
        prefix = postal_code_prefix.strip()

        counts, malformed = self.postal_code_counts()
        inhabitants = load_inhabitants()

        found = [
            {
                "postal_code": code,
                "facilities": counts.get(code, 0),
                "inhabitants": inhabitants.get(code),
                "facilities_per_10k_inhabitants": per_10k(counts.get(code, 0), inhabitants.get(code)),
            }
            for code in sorted(set(counts) | set(inhabitants))
            if code.startswith(prefix)
        ]

        total_facilities = sum(row["facilities"] for row in found)
        total_inhabitants = sum(row["inhabitants"] or 0 for row in found)
        ranked = rank_by_order(found, order)

        return {
            "postal_code_prefix": prefix,
            "order": order,
            "postal_codes": len(found),
            "postal_codes_without_facilities": sum(row["facilities"] == 0 for row in found),
            "facilities": total_facilities,
            "inhabitants": total_inhabitants,
            "facilities_per_10k_inhabitants": per_10k(total_facilities, total_inhabitants),
            "facilities_with_malformed_postal_code": malformed,
            "showing": min(limit, len(ranked)),
            "results": ranked[:limit],
            "note": INHABITANTS_NOTE,
        }

    def postal_code_counts(self) -> tuple[dict[str, int], int]:
        """{five-digit postal code: facilities}, plus the number of facilities whose code is malformed."""

        counts = self.value_counts("count_facilities_per_postal_code")

        # Values such as "(auf Anfrage)" or "8371" are not postal codes; only their total is reported.
        valid = {code: count for code, count in counts.items() if POSTAL_CODE.match(code)}

        return valid, sum(counts.values()) - sum(valid.values())

    def facilities_per_inhabitant_by_region(
        self,
        level: str = "region",
        order: str = "lowest_per_10k",
        postal_code_prefix: str = "",
        limit: int = 10,
        min_inhabitants: int = MIN_REGION_INHABITANTS,
    ) -> dict:
        """
        Facilities per 10,000 inhabitants per postal region (a postal code prefix of 1-3 digits).

        Sums count_facilities_per_postal_code over the region's codes, so the totals match
        that tool; regions without any facility show up with 0. Regions with fewer than
        min_inhabitants count towards the totals but are not ranked.
        """

        level = level.strip().casefold()
        if level not in REGION_LEVELS:
            return {"error": f"Unknown level '{level}'.", "valid_levels": list(REGION_LEVELS)}

        order = order.strip().casefold()
        if order not in POSTAL_CODE_ORDERS:
            return {"error": f"Unknown order '{order}'.", "valid_orders": list(POSTAL_CODE_ORDERS)}

        digits = REGION_LEVELS[level]
        prefix = postal_code_prefix.strip()
        if prefix and not prefix.isdigit():
            return {"error": f"postal_code_prefix '{prefix}' must be digits, e.g. '8' or '48'."}
        if len(prefix) >= digits:
            finer = [name for name, size in REGION_LEVELS.items() if size > len(prefix)]
            return {
                "error": (
                    f"postal_code_prefix '{prefix}' is already a single {level}. Use a finer "
                    "level, or count_facilities_per_postal_code for single postal codes."
                ),
                "valid_levels": finer,
            }

        limit = max(1, min(int(limit), MAX_POSTAL_CODE_RESULTS))

        counts, malformed = self.postal_code_counts()
        inhabitants = load_inhabitants()

        facilities = Counter()
        people = Counter()
        for code in set(counts) | set(inhabitants):
            if code.startswith(prefix):
                facilities[code[:digits]] += counts.get(code, 0)
                people[code[:digits]] += inhabitants.get(code, 0)

        towns = defaultdict(Counter)
        for row in self.select("count_facilities_per_postal_code_and_city"):
            code = row["postal_code"]
            if POSTAL_CODE.match(code) and code.startswith(prefix):
                towns[code[:digits]][row["city"]] += int(row["count"])

        total_facilities = sum(facilities.values())
        total_inhabitants = sum(people.values())
        average = per_10k(total_facilities, total_inhabitants)

        found = []
        for region in sorted(facilities):
            density = per_10k(facilities[region], people[region])
            found.append({
                "postal_code_prefix": region,
                "places": [
                    town for town, _ in sorted(towns[region].items(), key=lambda item: (-item[1], item[0]))
                ][:PLACES_PER_REGION],
                "facilities": facilities[region],
                "inhabitants": people[region] or None,
                "facilities_per_10k_inhabitants": density,
                "inhabitants_per_facility": (
                    round(people[region] / facilities[region]) if facilities[region] and people[region] else None
                ),
                "relative_to_average": (
                    round(density / average, 2) if density is not None and average else None
                ),
            })

        big_enough = [row for row in found if (row["inhabitants"] or 0) >= min_inhabitants]
        ranked = rank_by_order(big_enough, order)

        return {
            "level": level,
            "postal_code_prefix": prefix,
            "order": order,
            "regions": len(found),
            "regions_without_facilities": sum(row["facilities"] == 0 for row in found),
            "regions_below_min_inhabitants": len(found) - len(big_enough),
            "min_inhabitants": min_inhabitants,
            "facilities": total_facilities,
            "inhabitants": total_inhabitants,
            "facilities_per_10k_inhabitants": average,
            "facilities_with_malformed_postal_code": malformed,
            "showing": min(limit, len(ranked)),
            "results": ranked[:limit],
            "note": REGION_NOTE,
        }

    def facility_repository(self, city: str):
        """Facilities of a city plus their addresses and providers, shaped for src/fix_first.py."""

        import pandas as pd

        from src.fix_first import FixFirstRepository

        columns = DATASETS["einrichtung"]["columns"]
        # column -> query variable (the query joins multi-valued fields with "; ")
        variables = {
            "iri": "facility",
            "schema:name": "name",
            "schema:alternateName": "alternate_name",
            "schema:telephone": "telephone",
            "schema:email": "email",
            "foaf:homepage": "homepage",
            "schema:mainEntityOfPage": "main_page",
            "schema:category": "category",
            "schema:availableService": "service_description",
            "schema:audience": "audience",
            "schema:accessibilitySummary": "accessibility",
            "schema:openingHours": "opening_hours",
            "schema:parentOrganization": "provider",
            "cdf:parentOrgClassification": "parent_classification",
            "schema:address": "address",
        }

        facilities = self.select("list_facility_quality_fields_by_city", city=city.casefold())

        address_by_iri = {
            row["address"]: {
                "schema:streetAddress": row["street"],
                "schema:postalCode": row["postal_code"],
                "schema:addressLocality": row["city"],
            }
            for row in facilities
            if row["address"]
        }
        provider_by_iri = {
            provider["iri"]: {
                "iri": provider["iri"],
                "schema:name": provider["name"],
                "foaf:homepage": provider["homepage"],
            }
            for provider in self.list_providers()
        }
        einrichtung = pd.DataFrame(
            [{column: row[variables[column]] for column in columns} for row in facilities],
            columns=columns,
            dtype=object,
        )

        return FixFirstRepository(einrichtung, address_by_iri, provider_by_iri)

    def records_to_fix_first(self, city: str, **options) -> dict:
        """Rank the facilities of a city by how much fixing their record matters."""

        from src.fix_first import records_to_fix_first

        result = records_to_fix_first(self.facility_repository(city), **options)
        result["summary"] = {"city": city, **result["summary"]}

        return result

    def data_quality_report(self, organisation: str = "") -> dict:
        """Data quality overview of all organisations, or the full report of one."""

        from src.quality_report import build_quality_report

        return build_quality_report(self.select("list_facility_quality_flags"), organisation)

    def compare_facilities(self, facility_1: str, facility_2: str) -> list[dict]:
        """Compare two facilities by name."""

        return [
            {
                "search_name": name,
                "matches": self.get_facility_details(name),
            }
            for name in [facility_1, facility_2]
        ]

    def resolve_location(self, location: str) -> dict | None:
        """
        Turn a location into search coordinates.

        Accepts "latitude,longitude" or a five-digit postal code (the average position
        of the addresses with that code). Returns None when it can't be resolved.
        """

        match = COORDINATES.match(location)

        if match:
            latitude, longitude = float(match.group(1)), float(match.group(2))

            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                return {
                    "latitude": latitude,
                    "longitude": longitude,
                    "matched_by": "coordinates",
                }

            return None

        postal_code = location.strip()

        if not POSTAL_CODE.match(postal_code):
            return None

        result = self.select("get_postal_code_location_by_postal_code", postal_code=postal_code)

        if not result or int(result[0]["addresses"]) == 0:
            return None

        return {
            "latitude": float(result[0]["latitude"]),
            "longitude": float(result[0]["longitude"]),
            "matched_by": "postal_code",
            "matched_addresses": int(result[0]["addresses"]),
        }

    def find_nearby(
        self,
        location: str,
        radius_km: float = 2.0,
        query: str | None = None,
        accessible_only: bool = False,
        limit: int = 10,
    ) -> dict:
        """Find facilities within radius_km of a location, nearest first."""

        radius_km = min(max(float(radius_km), 0.1), MAX_RADIUS_KM)
        limit = min(max(int(limit), 1), MAX_NEARBY_RESULTS)

        origin = self.resolve_location(location)

        if origin is None:
            return {
                "location": location,
                "error": (
                    "Could not resolve the location. Use 'latitude,longitude' "
                    "(e.g. '52.5446,13.3592') or a five-digit postal code."
                ),
                "results": [],
            }

        bbox = bounding_box(origin["latitude"], origin["longitude"], radius_km)
        results = []

        for row in self.select("list_facilities_by_bbox", bbox=bbox):
            latitude, longitude = to_float(row["latitude"]), to_float(row["longitude"])
            accessible = accessibility_flag(row["accessibility"])

            if accessible_only and accessible is not True:
                continue

            if not matches_terms(
                [
                    row["name"], row["alternate_name"], row["category"], row["audience"],
                    row["service_description"], row["offers"], row["offer_categories"],
                ],
                query,
            ):
                continue

            distance = haversine_km(origin["latitude"], origin["longitude"], latitude, longitude)

            if distance > radius_km:
                continue

            results.append(
                {
                    "iri": row["facility"],
                    "name": row["name"],
                    "distance_km": round(distance, 2),
                    "address": format_address(row["street"], row["postal_code"], row["city"]),
                    "category": row["category"],
                    "audience": split_values(row["audience"]),
                    "offers": split_values(row["offers"]),
                    "accessible": accessible,
                    "telephone": row["telephone"],
                    "email": row["email"],
                    "homepage": row["homepage"],
                    "opening_hours": row["opening_hours"],
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )

        results.sort(key=lambda result: (result["distance_km"], result["iri"]))

        return {
            "location": location,
            "origin": origin,
            "radius_km": radius_km,
            "total_found": len(results),
            "results": results[:limit],
        }

    def get_awo_details(self, iri: str) -> dict | None:
        """
        Address, map link, contacts, opening hours, offers and source link for one
        facility, by IRI. Returns None for an unknown or malformed IRI.
        """

        iri = iri.strip()

        if not iri.startswith(FACILITY_IRI_PREFIX) or re.search(r"[\s<>\"{}|\\^`]", iri):
            return None

        result = self.select("get_facility_details_by_iri", iri=iri)

        if not result:
            return None

        row = result[0]
        latitude, longitude = to_float(row["latitude"]), to_float(row["longitude"])
        address = format_address(row["street"], row["postal_code"], row["city"])

        offers = []
        for offer in split_values(row["offers"]):
            name, _, category = (part.strip() for part in offer.partition("|"))
            if name or category:
                offers.append(
                    {
                        "name": name or category,
                        "category": category if name and category != name else None,
                    }
                )

        return {
            "iri": row["facility"],
            "name": row["name"],
            "alternate_name": row["alternate_name"],
            "category": row["category"],
            "address": address,
            "map_url": map_link(
                latitude, longitude, ", ".join(part for part in [row["name"], address] if part)
            ),
            "telephone": row["telephone"],
            "email": row["email"],
            "homepage": row["homepage"],
            "source_url": source_page(row["source_url"]),
            "opening_hours": row["opening_hours"],
            "accessibility": row["accessibility"],
            "offers": offers,
            "provider": row["provider_name"],
            "provider_type": row["provider_type"],
        }

    # ---- get_entity_card ------------------------------------------------------------

    def entity_properties(self, iri: str) -> tuple[list[str], dict[str, list[str]], set[str]]:
        """
        (entity types, {property IRI: values in query order}, IRIs among the values) of one
        entity. Types is empty when the IRI is not in the directory.
        """

        types: list[str] = []
        values: dict[str, list[str]] = {}
        links: set[str] = set()

        for row in self.select("list_entity_properties_by_iri", iri=iri):
            if row["type"] not in types:
                types.append(row["type"])

            if row["property"] is None or row["value"] is None:
                continue

            bucket = values.setdefault(row["property"], [])
            if row["value"] not in bucket:
                bucket.append(row["value"])

            if row["value_is_iri"] == "true":
                links.add(row["value"])

        return types, values, links

    def get_entity_card(self, iri: str) -> dict:
        """
        Every recorded field of one facility, service, provider or address, with missing
        fields listed, syntax-only quality notes and typed links to related entities.
        """

        iri = iri.strip() if isinstance(iri, str) else ""

        if not valid_entity_iri(iri):
            return {
                "status": "invalid_iri",
                "id": iri,
                "error": f"Expected a full directory IRI starting with {FACILITY_IRI_PREFIX}.",
            }

        return self._entity_card(iri, depth=0, seen=frozenset())

    def _entity_card(self, iri: str, depth: int, seen: frozenset) -> dict:
        types, values, links = self.entity_properties(iri)

        if not types:
            return {"status": "not_found", "id": iri}

        kinds = sorted(
            {ENTITY_TYPES[prefixed] for prefixed in ENTITY_TYPES if expand(prefixed) in types}
        )

        if len(kinds) != 1:
            return {
                "status": "unsupported_type" if not kinds else "ambiguous_type",
                "id": iri,
                "rdf_types": types,
            }

        kind = kinds[0]
        seen = seen | {iri}

        fields = {
            name: present("; ".join(values.get(expand(prop), []))) for name, prop in ENTITY_FIELDS[kind].items()
        }

        quality = {}
        if "telephone" in fields:
            quality["telephone"] = telephone_quality(fields["telephone"])
        for name in ("homepage", "source_page"):
            if name in fields:
                quality[name] = url_quality(fields[name])

        relationships = {}
        for name, (prop, expected) in ENTITY_LINKS[kind].items():
            targets = [value for value in values.get(expand(prop), []) if value in links]
            relationships[name] = self._relationship(prop, targets, expected, depth, seen)

        card = {
            "status": "ok",
            "entity_type": kind,
            "id": iri,
            "fields": fields,
            "field_properties": ENTITY_FIELDS[kind],
            "missing_fields": [name for name, value in fields.items() if value is None],
            "quality": quality,
            "relationships": relationships,
            "verification": "source_record_only",
        }

        if kind in ("facility", "provider"):
            card["contact_scope"] = f"{kind}_record"

        if depth == 0 and kind in ("facility", "provider"):
            services = self.select("list_linked_services_by_iri", iri=iri)
            card["linked_services_total"] = len(services)
            card["linked_services"] = [
                {"id": row["service"], "name": row["name"], "category": row["category"]}
                for row in services[:MAX_LINKED_SERVICES]
            ]

        return card

    def _relationship(self, prop: str, targets: list[str], expected: tuple, depth: int, seen: frozenset) -> dict:
        """One typed link of a card: missing, dangling, type_mismatch, cycle, resolved or multiple."""

        def one(target: str) -> dict:
            relation = {"status": "resolved", "target_id": target, "target_type": None, "entity": None}

            if target in seen:
                relation["status"] = "cycle"
                return relation

            if depth >= MAX_CARD_DEPTH or not valid_entity_iri(target):
                relation["status"] = "not_expanded"
                return relation

            card = self._entity_card(target, depth + 1, seen)

            if card["status"] == "not_found":
                relation["status"] = "dangling"
            elif card["status"] != "ok":
                relation["status"] = card["status"]
            elif card["entity_type"] not in expected:
                relation["status"] = "type_mismatch"
                relation["target_type"] = card["entity_type"]
            else:
                relation["target_type"] = card["entity_type"]
                relation["entity"] = card

            return relation

        if not targets:
            return {"status": "missing", "property": prop, "target_id": None}

        if len(targets) == 1:
            return {"property": prop, **one(targets[0])}

        return {"status": "multiple", "property": prop, "targets": [one(target) for target in targets]}

    # ---- search_service_candidates ----------------------------------------------------

    def search_service_candidates(self, query: str, limit: int = 5, offset: int = 0) -> dict:
        """
        Services whose name, category or audience contains query, each with the facility or
        provider it links to and that entity's locality. Paged; totals cover all matches.
        """

        limit, offset = clamp_page(limit, offset, default_limit=5)
        needle = casefold_de(query or "")
        found = self.select("list_service_candidates_by_query", query=needle)

        provider_types = {expand(prefixed): kind for prefixed, kind in ENTITY_TYPES.items()}
        candidates = []

        for row in found[offset:offset + limit]:
            provider_type = provider_types.get(row["provider_type"])

            if row["provider"] is None:
                provider_status = "missing"
            elif provider_type is None:
                provider_status = "dangling"
            else:
                provider_status = "resolved"

            if provider_status != "resolved":
                location = {"status": "unknown", "postal_code": None, "locality": None, "scope": None}
            else:
                known = row["postal_code"] is not None or row["city"] is not None
                location = {
                    "status": "known" if known else "missing",
                    "postal_code": row["postal_code"],
                    "locality": row["city"],
                    "scope": "facility_address" if provider_type == "facility" else "provider_organization_address",
                }

            matched_fields = [
                field
                for field in ("name", "category", "audience")
                if needle in casefold_de(row[field] or "")
            ]

            candidates.append(
                {
                    "service_id": row["service"],
                    "name": row["name"] or row["category"],
                    "name_is_fallback": row["name"] is None,
                    "category": row["category"],
                    "audience": row["audience"],
                    "provider": {
                        "status": provider_status,
                        "id": row["provider"],
                        "type": provider_type,
                        "name": row["provider_name"],
                        "links_to_several": int(row["provider_count"] or 0) > 1,
                    },
                    "location": location,
                    "matched_fields": matched_fields,
                }
            )

        return {
            "status": "ok",
            "query": query,
            **page_info(len(found), limit, offset, len(candidates)),
            "candidates": candidates,
        }

    # ---- search_directory ---------------------------------------------------------------

    def search_directory(
        self,
        query: str,
        provider: str | None = None,
        has_telephone: bool | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict:
        """
        Facilities where every query term occurs in the facility's text, its address or a
        linked service. Optional provider-name and telephone filters. Paged hits; total,
        missing-field counts, provider counts and phone-format counts cover all matches.
        """

        limit, offset = clamp_page(limit, offset, default_limit=10)
        terms = list(dict.fromkeys(casefold_de(term) for term in (query or "").split()))
        prefilter = max(terms, key=len, default="")

        if len(prefilter) < MIN_DIRECTORY_TERM_LENGTH:
            return {
                "status": "error",
                "query": query,
                "error": (
                    f"Give at least one search term of {MIN_DIRECTORY_TERM_LENGTH} or more "
                    "characters, for example 'Schuldner' or 'Sucht Kreuzberg'."
                ),
            }

        provider_term = casefold_de(provider) if provider else None
        matches = []

        for row in self.select("list_directory_facilities_by_term", term=prefilter):
            if provider_term is not None and provider_term not in casefold_de(row["provider_name"] or ""):
                continue

            if has_telephone is not None and (present(row["telephone"]) is not None) != bool(has_telephone):
                continue

            searchable = [
                ("facility", field, row[field])
                for field in (
                    "name", "alternate_name", "description", "category", "service_description",
                    "audience", "accessibility", "opening_hours",
                )
            ]
            searchable += [
                ("address", field, row[column])
                for field, column in (("street", "street"), ("postal_code", "postal_code"), ("locality", "city"))
            ]
            for service in (row["services"] or "").split(" || "):
                parts = (service.split(" | ") + ["", "", "", ""])[:4]
                searchable += [
                    ("service", field, value or None)
                    for field, value in zip(("name", "category", "audience", "area_served"), parts)
                ]

            texts = [(source, field, casefold_de(value)) for source, field, value in searchable if value]

            if not all(any(term in text for _, _, text in texts) for term in terms):
                continue

            matched_fields = []
            for source, field, text in texts:
                matched_terms = [term for term in terms if term in text]
                entry = {"source": source, "field": field, "matched_terms": matched_terms}
                if matched_terms and entry not in matched_fields:
                    matched_fields.append(entry)

            matches.append((row, matched_fields))

        def parent(row: dict) -> dict:
            if row["provider"] is None:
                return {"status": "missing", "id": None, "name": None, "label": "no provider link"}
            return {"status": "resolved", "id": row["provider"], "name": row["provider_name"],
                    "label": row["provider_name"] or row["provider"]}

        hits = [
            {
                "iri": row["facility"],
                "name": row["name"],
                "street": row["street"],
                "postal_code": row["postal_code"],
                "locality": row["city"],
                "telephone": row["telephone"],
                "has_email": present(row["email"]) is not None,
                "parent_provider": parent(row),
                "matched_fields": matched_fields,
            }
            for row, matched_fields in matches[offset:offset + limit]
        ]

        by_provider: dict = {}
        for row, _ in matches:
            entry = parent(row)
            key = entry["id"]
            by_provider.setdefault(key, {**entry, "count": 0})["count"] += 1

        phone_counts = {status: 0 for status in PHONE_STATUSES}
        for row, _ in matches:
            phone_counts[telephone_quality(row["telephone"])["status"]] += 1

        return {
            "status": "ok",
            "query": query,
            "terms": terms,
            "provider": provider,
            "has_telephone": has_telephone,
            "record_unit": "facility_records",
            **page_info(len(matches), limit, offset, len(hits)),
            "hits": hits,
            "summary_scope": "all_matching_facility_records",
            "missing_counts": {
                field: sum(present(row[column]) is None for row, _ in matches)
                for field, column in zip(
                    DIRECTORY_MISSING_FIELDS, ("telephone", "email", "opening_hours", "accessibility")
                )
            },
            "by_provider": sorted(by_provider.values(), key=lambda entry: -entry["count"]),
            "phone_format_counts": phone_counts,
            "phone_format_verification": "unverified_syntax_only",
        }

    def facility_matches(self, name: str) -> list[dict]:
        """Facilities whose name equals `name` (any case), or else contains it; at most 11."""

        rows = self.select("list_facilities_by_name", name=name.strip().casefold())

        return [row for row in rows if row["exact"] == "1"] or rows

    def explain_provenance(self, facility: str, field: str | None = None) -> dict:
        """
        Explain where the values of one directory entry come from.

        `facility` is an entity IRI or ID, or a facility name that must match exactly one
        facility (otherwise the candidates are returned). Every value of a field is listed
        with the sources whose records supplied it and whether the directory kept it.
        """

        if not facility.strip():
            raise ValueError("facility must be a facility name, IRI or ID")

        if field is not None and field not in PROVENANCE_FIELDS:
            raise ValueError(f"unknown field {field!r}: use one of {list(PROVENANCE_FIELDS)}")

        iri = entity_iri(facility)

        if iri is None:
            matches = self.facility_matches(facility)

            if len(matches) != 1:
                return {
                    "found": False,
                    "query": facility,
                    "candidates": [
                        {
                            "iri": row["facility"],
                            "name": row["name"],
                            "alternate_name": row["alternate_name"],
                            "street": row["street"],
                            "city": row["city"],
                        }
                        for row in matches[:10]
                    ],
                    "more_candidates": len(matches) > 10,
                    "hint": "Several facilities match: call again with one candidate's iri."
                    if matches else "No facility name contains this text.",
                }

            iri = matches[0]["facility"]

        directory = self.select("list_directory_values_by_iri", iri=iri)

        if not directory:
            return {
                "found": False,
                "query": facility,
                "iri": iri,
                "hint": "No directory entry has this IRI. IRIs can change between "
                "directory snapshots: search for the facility by name instead.",
            }

        wanted = [field] if field else [
            name for name in PROVENANCE_FIELDS if name not in LONG_TEXT_FIELDS
        ]
        field_of = {expand(PROVENANCE_FIELDS[name]): name for name in wanted}
        values = {name: {} for name in wanted}  # field -> value -> what we know about it

        for row in directory:
            name = field_of.get(row["predicate"])
            if name is None or row["value"] is None:
                continue

            value = values[name].setdefault(
                row["value"], {"value": row["value"], "in_directory": True, "sources": []}
            )

            if row["traced"] != "true" and "inherited_from" not in value:
                value["inherited_from"] = {
                    "iri": row["parent"],
                    "name": row["parent_name"],
                    "source": row["parent_source_label"],
                    "record": row["parent_record"],
                } if row["parent_record"] else None

        records = {}

        for row in self.select("list_value_sources_by_iri", iri=iri):
            source = row["source_label"] or row["source"]
            record = records.setdefault(row["record"], {
                "source": source,
                "record": row["record"],
                "record_name": None,
                "detail_page": None,
            })

            if row["predicate"] == expand("schema:name"):
                record["record_name"] = record["record_name"] or row["value"]
            if row["predicate"] == expand("schema:mainEntityOfPage"):
                record["detail_page"] = record["detail_page"] or row["value"]

            name = field_of.get(row["predicate"])
            if name is None or row["value"] is None:
                continue

            value = values[name].setdefault(
                row["value"], {"value": row["value"], "in_directory": False, "sources": []}
            )
            if source not in value["sources"]:
                value["sources"].append(source)

        fields = []

        for name in wanted:
            items = sorted(
                values[name].values(),
                key=lambda value: (not value["in_directory"], -len(value["sources"]), value["value"]),
            )

            if not items:
                if field:
                    fields.append({"field": name, "status": "no_value", "values": []})
                continue

            for value in items:
                value["sources"].sort()

            result = {"field": name, "status": provenance_status(items), "values": items}

            if result["status"] == "sources_differ":
                result["differs_only_in_case"] = same_ignoring_case(
                    [value["value"] for value in items if value["sources"]]
                )

            fields.append(result)

        types = {expand(prefixed): name for prefixed, name in ENTITY_TYPES.items()}

        return {
            "found": True,
            "iri": iri,
            "type": next((types[row["type"]] for row in directory if row["type"] in types), None),
            "name": next(
                (row["value"] for row in directory if row["predicate"] == expand("schema:name")),
                None,
            ),
            "sources": sorted(records.values(), key=lambda record: (record["source"], record["record"])),
            "fields": fields,
            "limits": PROVENANCE_LIMITS,
        }

    def list_sources(self) -> dict:
        """List the federated sources with coverage, record counts and harvest dates."""

        latest = {}

        for row in self.select("list_sources"):
            # One row per harvest observation, oldest first: the newest one wins.
            latest[row["source"]] = row

        return {
            "sources": [
                {
                    "source": row["label"],
                    "coverage": row["coverage"],
                    "source_records": int(row["records"]),
                    "harvested_at": row["fetched_at"],
                    "static_file_committed_at": row["static_committed_at"],
                }
                for row in latest.values()
            ],
            "notes": SOURCE_NOTES,
        }
