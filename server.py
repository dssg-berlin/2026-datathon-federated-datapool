"""AWO MCP server: exposes the federated directory of social support services as MCP tools.

Each tool calls one SparqlClient method (src/sparql_client.py), which runs the queries in
sparql_queries/ against the live SPARQL endpoint. Run with `python server.py` (stdio), or
with MCP_TRANSPORT=streamable-http to serve http://MCP_HOST:MCP_PORT/mcp (see Dockerfile).
"""

import functools
import inspect
import json
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from src.sparql_client import EndpointError, SparqlClient

sparql = SparqlClient()

# Sent to the client when it connects; MCP clients show it to the model with the tool list.
INSTRUCTIONS = """\
Federated directory of social support services in Germany: facilities, the services they
offer, addresses and provider organisations (mostly Caritas, DHS and AWO).
- The data is German. Search with German terms and keep names and addresses unchanged.
- Report only what the tools return. Never invent phone numbers, email addresses, opening
  hours or facilities; a missing field is "not on record". If nothing matches, say that
  nothing was found in the directory.
- If someone may be in danger or in a crisis, first give the emergency numbers 112
  (emergency), 110 (police) and TelefonSeelsorge 0800 111 0 111 (free, around the clock),
  then search.
"""

mcp = MCPServer("AWO MCP", instructions=INSTRUCTIONS)


def to_json(data) -> str:
    """Serialize a tool result; umlauts stay readable instead of being \\u-escaped."""

    return json.dumps(data, ensure_ascii=False, indent=2)


def tool(fn):
    """Register fn as an MCP tool whose endpoint failures reach the model.

    mcp 2.x shows the model only ToolError messages; any other exception becomes a bare
    "Error executing tool X", so the model could not say why or decide to retry.

    The docstring is the tool description the model reads; its indentation is removed,
    since every character of every description is sent with every request.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EndpointError as exc:
            raise ToolError(str(exc)) from exc

    return mcp.tool(description=inspect.cleandoc(fn.__doc__ or ""))(wrapper)


@tool
def count_facilities(city: str) -> int:
    """
    Count the facilities in one city, for example "How many facilities are in Berlin?".

    Args:
        city: German city name as written in Germany: "München", "Köln" (never "Munich",
            "Cologne"). The whole name must match, so "Berlin" does not include
            "Berlin-Mitte".
    """

    return sparql.count_facilities(city)


@tool
def detect_duplicates() -> str:
    """
    Find duplicate entries in the whole directory (facilities, addresses, services,
    providers): groups of entries that are identical in every field except their IRI.

    Returns:
        JSON per dataset with the duplicate groups, their IRIs and the shared values.
    """

    return to_json(sparql.detect_duplicates())


@tool
def search_facilities(city: str) -> str:
    """
    List every facility in one city with address, phone, email and homepage. Use it when
    the user wants the facilities of a city. For help with a specific need use
    find_social_help or find_nearby instead; for only the number use count_facilities.

    Args:
        city: German city name as written in Germany: "München", "Köln" (never "Munich",
            "Cologne"). The whole name must match, so "Berlin" does not include
            "Berlin-Mitte".
    """

    return to_json(sparql.search_facilities(city))


@tool
def get_facility_details(name: str) -> str:
    """
    Look up facilities by name: contacts, category, service description, address and
    provider (Träger). Use it whenever the user names a facility. To compare two
    facilities, call it once for each name.

    Args:
        name: Full or partial facility name, for example "Drehscheibe".
    """

    return to_json(sparql.get_facility_details(name))


@tool
def find_social_help(need: str, limit: int = 5, postal_code_prefix: str = "") -> str:
    """
    Find offers for a person who needs help with debt, housing, family, old age,
    addiction or migration, each with facility, address, phone and email. Use it for
    requests such as "I have debts, where can I get help?" in any language. If the
    person gives a postal code or a place and wants what is closest, use find_nearby.
    For any other topic (for example a women's shelter, disability or mental health),
    use search_directory or find_nearby with German search terms.

    Args:
        need: Exactly one of these English words, whatever the language of the request:
            "debt", "housing", "family", "elderly", "addiction", "migration".
        limit: Offers to return (1-10, default 5).
        postal_code_prefix: Optional start of a postal code, e.g. "10" or "10997", to
            only return offers there.

    Returns:
        JSON with total_matches and results. A null field is not on record: say so and
        suggest contacting the organisation; never invent a phone number or an address.
        Keep the German names and addresses unchanged.
    """

    return to_json(sparql.find_help(need, limit, postal_code_prefix))


@tool
def check_completeness() -> str:
    """
    Count the missing values per dataset and field (facilities, addresses, services,
    providers) in the whole directory. Use it for "how complete is the data?", "which
    fields are missing most often?" or "how many services have no provider link?". For
    one organisation use data_quality_report; for the records of one city use
    records_to_fix_first.

    Returns:
        JSON with completeness per dataset and per field.
    """

    return to_json(sparql.check_completeness())


@tool
def generate_statistics() -> str:
    """
    Overview statistics of the whole directory: number of facilities, services,
    providers and addresses; facilities per city, category and provider; services per
    category and audience; how many facilities have an address or a provider. For the
    number of facilities in one city use count_facilities.
    """

    return to_json(sparql.generate_statistics())


@tool
def list_providers() -> str:
    """
    List all provider organisations (Träger) in the directory with homepage and address.
    For the data sources the directory is built from, use list_sources.
    """

    return to_json(sparql.list_providers())


@tool
def search_by_postal_code(postal_code: str) -> str:
    """
    List the facilities whose address has exactly this postal code, with address and
    contacts. For facilities near or around a postal code, use find_nearby.

    Args:
        postal_code: Five-digit German postal code, for example "10115".
    """

    return to_json(sparql.search_by_postal_code(postal_code))


@tool
def count_facilities_per_postal_code(
    postal_code_prefix: str = "",
    order: str = "most_facilities",
    limit: int = 20,
) -> str:
    """
    Count facilities per postal code, with inhabitants and facilities per 10,000
    inhabitants. Use it to compare postal codes or to find under-served ones inside a
    city or area. For whole regions or all of Germany use
    facilities_per_inhabitant_by_region.

    Args:
        postal_code_prefix: Optional start of a postal code, e.g. "10" for central Berlin
            or "13353" for one code. Empty means all of Germany.
        order: "most_facilities" (default), "highest_per_10k" or "lowest_per_10k"
            (populated codes without any facility come first).
        limit: Postal codes to return (1-100, default 20).

    Returns:
        JSON with totals for the prefix and results per postal code. Inhabitants are
        estimates (a municipality's population spread evenly over its postal codes); say
        so when you compare codes within one city.
    """

    return to_json(sparql.count_facilities_per_postal_code(postal_code_prefix, order, limit))


@tool
def facilities_per_inhabitant_by_region(
    level: str = "region",
    order: str = "lowest_per_10k",
    postal_code_prefix: str = "",
    limit: int = 10,
    min_inhabitants: int = 10_000,
) -> str:
    """
    Compare German regions by facilities per inhabitant. Use it for "where in Germany
    are there the fewest facilities per inhabitant?", "which regions are best served?"
    or "is the south better covered than the north?". Regions are postal code prefixes,
    named by their towns with the most facilities.

    Args:
        level: "region" (default, two digits such as 48 = Münster, about 95 regions),
            "zone" (first digit) or "area" (three digits, about 680 areas).
        order: "lowest_per_10k" (default, least served first), "highest_per_10k" or
            "most_facilities".
        postal_code_prefix: Optional, limits to one part of Germany, e.g. "8" for the
            regions 80-89. Must be shorter than the level.
        limit: Regions to return (1-100, default 10).
        min_inhabitants: Regions with fewer inhabitants are not ranked (default 10,000).

    Returns:
        JSON with totals, the average and, per region, its places, facilities,
        inhabitants, facilities per 10,000 inhabitants and density relative to the
        average. The directory does not list every service, so say that a low value can
        also mean less published data.
    """

    return to_json(sparql.facilities_per_inhabitant_by_region(
        level, order, postal_code_prefix, limit, min_inhabitants,
    ))


@tool
def records_to_fix_first(
    city: str = "Berlin",
    provider: str | None = None,
    n: int = 10,
    bucket: str | None = None,
    group_by: str | None = None,
    focus: str | None = None,
    explain: bool = False,
) -> str:
    """
    Rank the facility records of one city by how much fixing them matters, for data
    maintainers (not for people looking for help). Use it for "which records should we
    fix first?", "what is wrong with the data in Berlin?", "which records may not belong
    in the directory?", "whom do we ask about missing information?" or "how is the
    priority calculated?" (then set explain). All counts cover only this city.

    Each record has a priority (high, medium, low) and a bucket: complete_key_field
    (missing phone, email, hours or address), repair (a broken value, such as a phone
    number without its leading zero), ask_provider, enrich_from_source, scope_check (may
    not be a service), junk_or_duplicate, needs_research or minor_gaps.

    Args:
        city: City to check, exact match (default "Berlin").
        provider: Part of a provider name, or "unassigned" for facilities without one.
        n: Records to return (default 10).
        bucket: Only this bucket, for example "scope_check".
        group_by: "provider" returns one batch per provider for outreach instead of
            single records.
        focus: "urgent" keeps only services people may urgently depend on (addiction,
            homelessness, crisis, debt).
        explain: true adds the numeric score and its breakdown. Only when the user asks
            how priority is determined or why one record ranks above another.

    Returns:
        JSON with a summary, a projected coverage gain (a projection, not a measurement)
        and ranked records with reasons and next steps, or provider batches. Answer only
        from the result: lead with the conclusion, list every returned record in rank
        order, and take counts from the summary instead of counting rows.
    """

    return to_json(
        sparql.records_to_fix_first(
            city,
            provider=provider,
            n=n,
            bucket=bucket,
            group_by=group_by,
            focus=focus,
            explain=explain,
        )
    )


@tool
def data_quality_report(organisation: str = "") -> str:
    """
    Data quality scorecard per organisation, for data maintainers (not for people
    looking for help). Use it for "how good is the Caritas data?", "which organisations
    have the most gaps?", "what should organisation X improve?" or for a report or
    message to send to an organisation. Counts cover the whole directory.

    Args:
        organisation: Part of an organisation name: the provider linked to a record or,
            without one, the website it came from (e.g. "www.caritas.de"). Empty returns
            an overview of the largest organisations. With a name you get the fill rates
            of phone, email, website, opening hours, category, street and postcode,
            known problems, what to improve, and a draft message. The draft is only a
            draft: never claim it has been sent.

    Returns:
        JSON with the overview or the report, or an error listing known names.
    """

    return to_json(sparql.data_quality_report(organisation))


@tool
def find_nearby(
    location: str,
    radius_km: float = 2.0,
    query: str | None = None,
    accessible_only: bool = False,
    limit: int = 10,
) -> str:
    """
    Find facilities near a place, nearest first, with distance, address, contacts,
    opening hours and a map link. Use it when the user names a place: a postal code,
    coordinates or a landmark ("near 13353", "close to Alexanderplatz"), with or without
    a topic.

    Args:
        location: Five-digit postal code ("13353") or "latitude,longitude" in decimal
            degrees ("52.5446,13.3592"), nothing else. For a landmark or neighbourhood,
            pass its approximate coordinates.
        radius_km: Search radius in kilometres (0.1-50, default 2).
        query: Optional German search terms matched against name, category, services
            and audience. Comma-separated terms match any of them ("Sucht, Alkohol").
        accessible_only: Only facilities marked as wheelchair accessible.
        limit: Maximum number of results (1-50, default 10).
    """

    return to_json(
        sparql.find_nearby(
            location,
            radius_km=radius_km,
            query=query,
            accessible_only=accessible_only,
            limit=limit,
        )
    )


@tool
def search_service_candidates(query: str, limit: int = 5, offset: int = 0) -> str:
    """
    Search the service catalogue (offers) by name, category or audience, and get for
    each service the facility or provider that runs it, with that office's postal code
    and locality. Use it for "which services are there for X?" or "who offers X?".

    Use short German terms ("Schuldner", "Suchtberatung", "Senioren"). Matching is a
    literal substring. Facility names and descriptions are not searched: use
    search_directory for those. Zero results is a catalogue miss, not proof that no such
    service exists.

    Args:
        query: Search text matched in service name, category and audience.
        limit: Results per page (1-25, default 5).
        offset: Skip this many results (next_offset of the previous page).

    Returns:
        JSON with total, a page of candidates (service_id, name, category, audience,
        provider, location) and next_offset. get_entity_card gives every field of an id.
    """

    return to_json(sparql.search_service_candidates(query, limit=limit, offset=offset))


@tool
def get_entity_card(iri: str) -> str:
    """
    Get everything the directory records for one entry by its IRI (facility, service,
    provider or address): contacts, opening hours, accessibility, audience, the fields
    that are missing, and its links (service -> facility -> address and provider) with
    the services a facility offers. Use it when the user gives an IRI, or to check an
    entry another tool returned before passing its details on. If you only have a name,
    look it up with get_facility_details first.

    A null field is not on record: say so, do not fill it in. Copy telephone values
    exactly, without adding or removing digits. The card shows what is recorded, not
    whether it is current.

    Args:
        iri: Full IRI as returned by another tool, for example
            "https://civic-data.de/federated-directory#org-34e986356c61".
    """

    return to_json(sparql.get_entity_card(iri))


@tool
def search_directory(
    query: str,
    provider: str | None = None,
    has_telephone: bool | None = None,
    limit: int = 10,
    offset: int = 0,
) -> str:
    """
    Full-text search over facility records: name, description, category, audience,
    accessibility, opening hours, address and the linked services. Every term must
    match, so "Schuldner Kreuzberg" or "Sucht 10997" narrow the search. Use it for
    questions that combine conditions or need a count, for example "how many debt
    counselling facilities in Kreuzberg have a phone number?". For every facility of one
    postal code use search_by_postal_code.

    total, missing_counts, by_provider and phone_format_counts cover all matches, not
    only the returned page. Matching is literal text, not relevance or distance; zero
    hits is a catalogue miss, not proof that nothing exists. Copy telephone values
    exactly as stored.

    Args:
        query: German search terms (at least one with 3 or more characters).
        provider: Only facilities whose parent provider name contains this text. Few
            facilities have a provider link, so this filter misses most.
        has_telephone: true = only facilities with a phone number on record,
            false = only facilities without one.
        limit: Hits per page (1-25, default 10).
        offset: Skip this many hits (next_offset of the previous page).
    """

    return to_json(
        sparql.search_directory(
            query,
            provider=provider,
            has_telephone=has_telephone,
            limit=limit,
            offset=offset,
        )
    )


# ---- Prompts ------------------------------------------------------------------

# Search terms that work well with find_nearby, by kind of need. The data is
# in German, so German terms match best.
SEARCH_TERM_HINTS = """
- Addiction (alcohol, drugs, gambling, gaming, medication): "Sucht, Alkohol, Drogen,
  Glücksspiel, Mediennutzung, Medikamenten"
- Housing loss, eviction, homelessness: "Wohnungsnot, Wohnungslos, obdach, Notübernachtung"
- Debt: "Schuldner"
- Children, youth, families: "Jugend, Kinder, Familie, Eltern"
- Older people and care: "Senior, Pflege"
- Disability or mental health: "Behinder, psych, Eingliederung"
- Migration and integration: "Migration"
- Unemployment and training: "erwerbslos, arbeitslos, Ausbildung"
- Outreach and drop-in (staff who come to you, or walk in without an appointment):
  "Streetwork, aufsuchend, Kontakt- und Anlaufstelle"
"""

RELEVANCE_RUBRIC = """
Rate how strongly you'd recommend each place as a step for this person, from 1 to 5.
Start from first-step fit, then adjust:
- First-step fit (most important):
  3 = a good place to start for this need (e.g. a counselling centre for drinking),
  2 = addresses the need but usually comes later or suits only some people
      (e.g. supported living, a clinic for a specific group),
  1 = only loosely related.
- Distance: within 1 km +1, 1–3 km +0.5, further +0.
- Reachability: phone or email listed +1, only a source link or address +0.5.
- If the person needs step-free access and it isn't listed as accessible, -1.
Round to the nearest whole number, then cap it: places with first-step fit 2 can
get at most 3/5, and fit 1 at most 2/5. So 4–5/5 always means "a good place to
start". Show it as ★ (e.g. ★★★★☆ 4/5).
The rating reflects fit for their need based on the directory data, not the quality
of the service; say so once.
"""


@mcp.prompt(title="Find help near me")
def find_help(situation: str = "", location: str = "") -> str:
    """
    Explain the kinds of help available for someone's need, recommend 3–5
    nearby places rated by relevance, and list who to contact first.

    Args:
        situation: What the person needs help with, in their own words.
        location: Where they are: a postal code, or a landmark or neighbourhood.
    """

    return f"""You are helping someone find social services in Germany using the directory tools.
Be warm and brief, and use plain language. Reply in the language the person writes in.
Keep the whole answer short enough to read on a phone.

What they need help with: {situation or "not given yet"}
Where they are: {location or "not given yet"}

Before answering:
1. If they mention danger to themselves or others, give these numbers before
   anything else: 112 (emergency), 110 (police), Telefonseelsorge
   0800 111 0 111 or 0800 111 0 222 (free, 24/7).
2. If the need or the location is missing, ask for it in one short question.
   Don't ask for names or other personal details; they aren't needed to search.
3. Turn their need into German search terms and call find_nearby with
   comma-separated terms. Useful terms:
{SEARCH_TERM_HINTS}
   find_nearby takes a five-digit postal code or "latitude,longitude". For a street,
   landmark or neighbourhood, pass its approximate coordinates; if you don't know
   them, ask for the postal code.
   If they need step-free access, set accessible_only.
   If fewer than 3 results come back, widen radius_km (2 → 5) or broaden the terms.
   If the need suggests they may not be able to come in (e.g. homelessness,
   addiction), also search the outreach and drop-in terms.
4. Read each result's name, category, audience and offers to work out who the place
   is for (e.g. women only, pregnant women, people who use illegal drugs, young
   people). For the most promising places, call get_entity_card with their iri: it
   adds the link to the entry in the source directory (fields.source_page), the
   accessibility notes and every linked service.

Then answer in this order:

A. A one-line acknowledgement, then a safety note only if the need calls for one
   (e.g. don't stop heavy daily drinking suddenly because withdrawal can be
   dangerous; act quickly on an eviction notice because deadlines are short).

B. Kinds of help: 2–4 short bullets on the types of service that exist for this
   need (e.g. counselling centre, clinic, supported living, drop-in, outreach),
   what each is for, and which is usually the best first step.

C. Recommended places: 3 to 5, ordered by rating. For each:
   - Name and rating
   - Who it's for, in one line, flagging limits like "women only" or
     "mainly for illegal drugs"
   - Address, distance, and the directions link (map_url)
   - Opening hours, or "not listed"
{RELEVANCE_RUBRIC}

D. Who to contact first: the three best-rated places that can be reached, as a
   compact list of name, phone, email, and when to call. Don't repeat their
   descriptions. Prefer outreach or drop-in services when they fit the need.
   - The directory lists facility contact details only, not named staff or
     outreach workers. Never invent names, phone numbers, or email addresses.
   - If fewer than three places have contact details, say so plainly. For those
     places, give their source link (source_page), which often lists more contact
     information; if there's none, give the address to visit during opening hours.

E. What the directory doesn't cover, in one line, when it matters for this need
   (e.g. self-help groups; coverage depends on which providers list services in
   the area, mostly Caritas and DHS). Then
   offer to look for something else or somewhere else.

If nothing fits, say so honestly rather than suggesting unrelated services."""


@tool
def explain_provenance(facility: str, field: str | None = None) -> str:
    """
    Explain where the values of one entry come from: which sources (Caritas, DHS,
    Sozialplattform, AWO, Destatis) supplied each value, where they disagree, which
    value the directory kept and which values were inherited from a parent
    organisation. Use it for "where does this phone number come from?", "do the sources
    agree?", "which value should I trust?" or "where can I verify this entry?".

    Args:
        facility: Facility name, or the IRI of a facility, service or provider (a short
            ID such as "org-00416a783a9b" also works). If a name matches several
            facilities, the candidates are returned: call again with one candidate's IRI.
        field: Optional, explain only this field: name, alternate_name, telephone,
            email, homepage, opening_hours, detail_page, category, audience,
            accessibility, area_served, street, postal_code, city, description or
            services. Without it, every field with a value is explained except
            description and services.

    Returns:
        JSON with the entry, its source records (with detail pages to verify them) and,
        per field, a status (single_source, sources_agree, sources_differ or
        not_from_own_sources) and each value with its sources and whether it was kept.
    """

    try:
        return to_json(sparql.explain_provenance(facility, field))
    except ValueError as exc:
        # mcp 2.x shows the model only ToolError messages; other errors stay on the server.
        raise ToolError(str(exc)) from exc


@tool
def list_sources() -> str:
    """
    List the data sources the directory combines (Caritas, DHS, Sozialplattform, AWO,
    Destatis) with the area each covers, its number of records and its harvest date.
    Use it for "where does the data come from?", "how current is the data?" or "why has
    provider X no entries in region Y?". For the provider organisations (Träger) in the
    directory use list_providers.
    """

    return to_json(sparql.list_sources())


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    """Liveness probe for the HTTP deployment (Docker HEALTHCHECK); not an MCP endpoint."""

    return PlainTextResponse("ok")


def main() -> None:
    """Start the server: stdio by default, HTTP when MCP_TRANSPORT=streamable-http."""

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    # stdout carries the MCP protocol over stdio, so log to stderr.
    print(f"AWO MCP server starting ({transport})...", file=sys.stderr)

    if transport == "stdio":
        mcp.run()
    elif transport == "streamable-http":
        # Serves http://MCP_HOST:MCP_PORT/mcp. Stateless: every tool is one independent
        # query, so no session survives a restart or needs a sticky load balancer.
        mcp.run(
            "streamable-http",
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
            stateless_http=True,
        )
    else:
        sys.exit(f"Unknown MCP_TRANSPORT {transport!r}: use 'stdio' or 'streamable-http'.")


if __name__ == "__main__":
    main()
