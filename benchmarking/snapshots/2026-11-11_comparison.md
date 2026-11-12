# Benchmark comparison: before (22 tools) → after (19 tools)

- **before (22 tools)**: 22 tools · server http://localhost:8000/mcp · runs started 2026-11-11T14:06:50+00:00 to 2026-11-11T18:21:09+00:00
- **after (19 tools)**: 19 tools · server http://localhost:8001/mcp · runs started 2026-11-11T18:08:01+00:00 to 2026-11-11T18:35:01+00:00
- tools removed: `compare_facilities`, `get_awo_details`, `search_services`

Each cell is before (22 tools) → after (19 tools). Passed = every check passed; right tool = the first tool call was an expected tool; tool call = right tool with valid, expected arguments; invented contacts = answers with a phone number or email address no tool returned.

## Result

| model | tool profile | cases | passed | right tool | tool call | invented contacts | tokens |
|---|---|---|---|---|---|---|---|
| claude-code-haiku | all | 92 | 76/92 (83%) → 88/92 (96%) | 77/90 (86%) → 87/90 (97%) | 77/90 (86%) → 87/90 (97%) | 1/91 (1%) → 0/92 (0%) | 3,486,319 → 3,166,487 |
| claude-code-opus | all | 92 | 82/92 (89%) → 89/92 (97%) | 87/90 (97%) → 90/90 (100%) | 86/90 (96%) → 89/90 (99%) | 3/90 (3%) → 0/92 (0%) | 4,302,411 → 3,481,755 |
| qwen-2-5-7b-ollama | all | 92 | 59/92 (64%) → 78/92 (85%) | 62/90 (69%) → 83/90 (92%) | 61/90 (68%) → 81/89 (91%) | 4/92 (4%) → 5/92 (5%) | 1,097,006 → 920,738 |

## Per suite

| model | tool profile | suite | cases | passed | right tool |
|---|---|---|---|---|---|
| claude-code-haiku | all | citizen | 40 | 33/40 (82%) → 39/40 (98%) | 33/38 (87%) → 38/38 (100%) |
| claude-code-haiku | all | professional | 8 | 8/8 (100%) | 8/8 (100%) |
| claude-code-haiku | all | smoke | 44 | 35/44 (80%) → 41/44 (93%) | 36/44 (82%) → 41/44 (93%) |
| claude-code-opus | all | citizen | 40 | 33/40 (82%) → 37/40 (92%) | 37/38 (97%) → 38/38 (100%) |
| claude-code-opus | all | professional | 8 | 8/8 (100%) | 8/8 (100%) |
| claude-code-opus | all | smoke | 44 | 41/44 (93%) → 44/44 (100%) | 42/44 (95%) → 44/44 (100%) |
| qwen-2-5-7b-ollama | all | citizen | 40 | 18/40 (45%) → 33/40 (82%) | 19/38 (50%) → 33/38 (87%) |
| qwen-2-5-7b-ollama | all | professional | 8 | 3/8 (38%) → 6/8 (75%) | 4/8 (50%) → 7/8 (88%) |
| qwen-2-5-7b-ollama | all | smoke | 44 | 38/44 (86%) → 39/44 (89%) | 39/44 (89%) → 43/44 (98%) |

## Cases that changed

- **claude-code-haiku / all**: 15 gained, 3 lost.
  - lost: `directory-sucht-with-phone-german` (wrong tool chosen), `search-facilities-koeln-german` (wrong tool chosen), `sources-awo-coverage-german` (wrong tool chosen)
  - gained: `citizen-q19`, `citizen-q22`, `citizen-q29`, `citizen-q39`, `citizen-q47`, `citizen-q48`, `awo-details-accessibility`, `awo-details-by-iri`, `compare-side-by-side`, `nearby-postal-code`, `postal-code-underserved-german`, `search-services-sucht`, `service-candidates-senioren-german`, `service-candidates-with-office`, `statistics-top-city-and-category`
- **claude-code-opus / all**: 8 gained, 1 lost.
  - lost: `citizen-q40` (did not say nothing was found)
  - gained: `citizen-q11`, `citizen-q20`, `citizen-q23`, `citizen-q29`, `citizen-q44`, `provenance-phone`, `search-facilities-koeln-german`, `search-services-sucht`
- **qwen-2-5-7b-ollama / all**: 22 gained, 3 lost.
  - lost: `completeness-missing-phone-german` (wrong tool chosen), `search-facilities-berlin` (invented contact details), `sources-awo-coverage-german` (final answer missing expected content)
  - gained: `citizen-q01`, `citizen-q03`, `citizen-q04`, `citizen-q06`, `citizen-q07`, `citizen-q08`, `citizen-q10`, `citizen-q14`, `citizen-q15`, `citizen-q17`, `citizen-q18`, `citizen-q22`, `citizen-q24`, `citizen-q39`, `citizen-q47`, `handoff-prof-P01`, `handoff-prof-P03`, `handoff-prof-P08`, `awo-details-by-iri`, `postal-code-exact-area`, `service-candidates-senioren-german`, `statistics-top-city-and-category`

## Tools called first by mistake

| model | tool profile | before (22 tools) | after (19 tools) |
|---|---|---|---|
| claude-code-haiku | all | `find_social_help` 6, `get_entity_card` 2, `search_facilities` 2, `facilities_per_inhabitant_by_region` 1, `generate_statistics` 1, `get_facility_details` 1 | `count_facilities` 2, `find_social_help` 1 |
| claude-code-opus | all | `count_facilities` 1, `generate_statistics` 1, `search_service_candidates` 1 | none |
| qwen-2-5-7b-ollama | all | `search_facilities` 19, `count_facilities` 3, `get_entity_card` 2, `search_services` 2, `get_awo_details` 1, `search_directory` 1 | `find_social_help` 2, `(no tool call)` 1, `count_facilities_per_postal_code` 1, `data_quality_report` 1, `get_entity_card` 1, `search_directory` 1 |
