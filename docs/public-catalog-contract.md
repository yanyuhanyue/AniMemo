# Bounded public catalogue v1

Wire schema: `animemo.public-catalog/v1`. All reads declare `consistency: "live"`.
The canonical prefix is `/api/v1/`; `/api/` aliases use the same route table,
permissions and implementation. The generated OpenAPI document describes each
success envelope and the existing strict `ApiError` failure shape.

Complete browser exports honor HTTP429 `Retry-After` with a cancellable wait,
at most three consecutive retries per GET; list/detail browsing keeps visible
retryable failures. Export still uses the instance's unchanged rate limits.
The detail envelope includes `next_export_cursor`, signed only for the same
scope's unfiltered `id-asc` entries query. After every page's last complete item
has been written, the exporter re-reads that item with its required revision
and immediately uses the freshly issued cursor. This avoids carrying a page
token through arbitrarily long field reads or rate-limit pauses. All tokens
keep the same 15-minute TTL and authorization checks. Changed or withdrawn last
items abort the file; earlier records retain the documented live-read meaning.

Automatic refresh only runs on the first list page and first facet groups with
no facet search in progress. Later-page navigation remains stable until an
explicit refresh. Complete export uses a browser file writer; unsupported
browsers display unavailable instead of downloading only cached pages.

## Routes and authority

Let `base` be `public/homepage/` or `public/showcase/{public_slug}/`.

| GET suffix | Result |
| --- | --- |
| `{base}entries/` | A filtered, ordered page of bounded entry previews |
| `{base}summary/` | Full authorized statistics, total, matched count and profile |
| `{base}facets/?kind=tags` or `kind=years` | A searchable page of all authorized facet values |
| `{base}entries/{entry_id}/` | One bounded public entry preview and field continuation metadata |
| `{base}entries/{entry_id}/fields/{field}/` | A revision-bound segment of the original field's JSON text |
| `public/showcases/` | A page of approved public journals, statistics and at most three public top picks each |

Homepage reads require the explicitly configured active staff owner, approved
publication and current sharing consent. They include only undeleted `public`
entries, including when the caller is that owner. Missing or invalid homepage
ownership produces a successful empty scope; it does not choose another owner.

Showcase anonymous and other-user reads require an active owner, approved
publication and sharing consent. The authenticated owner can preview all their
undeleted entries before publication. This is a server-selected scope, not an
input permission flag. Every preview uses the same public field whitelist.
Unavailable showcases or entries return 404. Directory owners meet the public
conditions and have at least one currently public undeleted entry.

Every request resolves authority again; authorization is also present in each
data query. A cursor never grants continued access after sharing withdrawal,
deactivation, deletion or a visibility change. Authenticated catalogue responses
and errors are `private, no-store`; all catalogue variants include
`Vary: Authorization, Cookie`.

## Hard limits and pagination

| Boundary | Fixed limit |
| --- | --- |
| Uncompressed success JSON, including envelope and cursor | 524288 UTF-8 bytes |
| Error JSON | 16384 UTF-8 bytes |
| Page size | Default 50; maximum 100 |
| Signed cursor or selection token | 4096 UTF-8 bytes; 900-second lifetime |
| Field segment | Default 4096; maximum 16384 Unicode codepoints, at most 65536 raw UTF-8 bytes |
| Description / review preview | 512 codepoints each |
| Tag preview | First 8 values, at most 160 codepoints each |
| Complete tag-colors preview | At most 2048 codepoints of serialized JSON; otherwise `{}` with `complete: false` |

The actual compact JSON renderer measures the response; an `Accept` indentation
parameter does not enlarge it. A page may contain fewer rows than requested to
fit the byte budget. `next_cursor` starts after the last row actually emitted.
The next request repeats the original conditions and carries that cursor. A
cursor is bound to route, authorized scope, normalized conditions, ordering
version and collation version. Page size may change between requests.

Entry and directory pages are:

```json
{
  "schema": "animemo.public-catalog/v1",
  "consistency": "live",
  "scope": {"kind": "homepage", "owner_id": 1, "visibility": "public", "public_slug": "UUID"},
  "total": 501,
  "matched_count": 501,
  "page_count": 50,
  "results": [],
  "next_cursor": "SIGNED_TOKEN"
}
```

`results` above is abbreviated. `page_count` always equals its actual length;
it is not the number of pages. `total` counts the authorized scope before
filters, and `matched_count` counts the full filtered scope before paging.
`next_cursor: null` is the end. Scope `owner_id` and `public_slug` are null for an
empty homepage and for the directory; `visibility` is `public` or `owner`.

These are live reads. Between requests an edit can change ordering, membership,
counts or the representative source of a facet. Clients should refresh on
conflicting revisions or expired cursors and clear caches when identity or
scope changes. A static dataset can be read completely through its pages; a
multi-request read is not a durable database snapshot.

## Filters, ordering and statistics

`entries/` accepts `search`, `tag`, `tag_ref`, `status`, `year`, `year_ref`,
`quick`, `sort`, `page_size` and `cursor`. `summary/` accepts the same conditions
without paging arguments. Unknown or repeated query keys are rejected.
Individual text inputs are at most 4096 UTF-8 bytes. Oversized facet values
remain selectable through `tag_ref` or `year_ref`; a literal and its reference
cannot be supplied together.

Search follows the public UI's trimmed, case-insensitive title plus Japanese
title search. Tag matching is exact. Year matching uses the original period
prefix, with an empty period treated as `未定档`. The `all` sentinel removes the
corresponding tag, year or status filter. Status values are `completed`,
`watching`, `planned`, `on_hold` and `dropped`.

Quick groups are `all`, `yuri` (真百 / 轻百), `daily` (萌系 / 日常), `school`
(搞笑 / 校园), `original` (原创 / 治愈), and `special` (剧场版 / OVA / 泡面番).
Their members use OR matching. The quick OVA predicate recognizes a tag whose
uppercase value is OVA, or standalone OVA delimited by JavaScript whitespace
or the existing `《` / `》` boundaries in title plus Japanese title.

The four UI sort values remain `date-desc`, `date-asc`, `score-desc`,
`score-asc`. A period matching four digits, a hyphen and one or two digits has
the numeric key `year * 100 + month`; other periods use zero. This retains the
existing treatment of values such as `2026-99`. Date ties use ascending
`zh-CN` title order, then descending update time and ID. Scores sort in the
chosen direction, with null and zero in the final bucket; ties use descending
update time and ID. `id-asc` is an additive ordering for sequential export.
All filtering and complete ordering precede the keyset boundary.

Summary returns `total`, `matched_count`, `stats`, `unscored_count` and
`profile` alongside the common envelope. Profile is null for homepage reads.
`stats` covers the entire authorized scope independently of active filters:

| Statistic | Rule |
| --- | --- |
| total | All authorized undeleted entries |
| completed_count | Status `completed` |
| average_score | Existing Python `sum(float(score)) / count`, rounded to two places; excludes null, includes zero |
| movie_count / short_count | Exact tag 剧场版 / 泡面番 |
| ova_count | Exact tag OVA, or OVA anywhere in uppercased title |
| masterpiece_count | Score at least 9.5 |
| pending_count | Status planned, null score, period containing 待定, or period exactly 未定档 |
| unscored_count (top level) | Null score or score at most zero |

The quick OVA filter and aggregate OVA statistic intentionally retain their
different established rules. The averaging path reads bounded scalar score
batches in the original update-time/ID order and uses Python's built-in sum;
SQL decimal averaging can produce a different result at binary rounding ties.
Other statistics use database aggregates.

Directory accepts only `search`, `page_size` and `cursor`. Search matches
nickname, username or subtitle independently. Its order is descending profile
update time, then ID. Each result contains `nickname`, `username`, `subtitle`,
`avatar_url`, `accent`, `public_slug`, the same `stats` and up to three
`top_picks`. Top picks sort by non-null score descending, update time descending
and ID descending, and use the public preview DTO.

## Complete facets

Facet pages accept `kind`, `search`, `page_size` and `cursor`. Their envelope
adds `kind`, `facet_scope: "authorized"`, `order`, `values`, `next_cursor` and
`complete`. They scan the complete authorized scope, independently of the
entry list's active filters. Search applies to full values in the database.

Each item has `value`, `preview`, `complete`, `selection_token`, `revision` and
`field_url`. A value of at most 160 codepoints is complete; a larger value has
`value: null` and an explicit preview. Its selection token identifies the
authorized source entry and tag position without embedding a giant value.
Following `field_url` reads the complete value as JSON-text segments. Continue
with the same URL and its returned cursor, optionally passing `part_size`.

Exact distinct tag strings, including preserved empty strings, remain distinct.
An empty or literal `all` tag/year selection uses its `selection_token` as
`tag_ref`/`year_ref`, preserving the value separately from the no-filter sentinel.
Presentation uses Chinese collation, with the original first-occurrence order
as a stable tie break. Year facets preserve their period-derived strings and
filter behavior. Their explicit `facets-v1-numeric-then-zh` display order places
unsigned decimal strings in numeric descending order, followed by other values
in Chinese order, with stable source ties. This replaces the previous
non-transitive mixed-number JavaScript comparator; it changes display ordering
without dropping values or changing prefix matching.

## Entry whitelist, revisions and field segments

Original public fields are `id`, `title`, `japanese_title`, `airing_period`,
`studio`, `episodes`, `description`, `poster_url`, `poster`, `baike_url`, `tags`,
`tag_colors`, `personal_score`, `watch_status`, `watch_status_display`, `review`,
`visibility`, `watch_history_count`, `created_at` and `updated_at`.
Scores retain string-or-null encoding. Raw tag order, duplicates and accepted
legacy tag-color JSON shapes survive complete reading.

List, detail and top-pick previews add `revision`, `detail_url`, `fields` and
`preset_colors`. `preset_colors` contains only current public preset colors for
the preview's tags; it is presentation metadata. `fields` has exactly
`description`, `review`, `tags`, `tag_colors`, each with `complete`, `kind`,
`length` and `field_url`. `length` is the full field's serialized JSON-text
length in Unicode codepoints. A short preview is the full value only when its
metadata says `complete: true`.

Details return `{schema, consistency, scope, entry, next_export_cursor}`. An optional `revision`
query rejects a changed record with 409. Revision is an opaque current row
version plus public history-count fingerprint. Field requests require the
revision in their provided URL. They return:

```json
{
  "schema": "animemo.public-catalog/v1",
  "consistency": "live",
  "scope": {"kind": "homepage", "owner_id": 1, "visibility": "public", "public_slug": "UUID"},
  "entry_id": 42,
  "field": "review",
  "revision": "OPAQUE_REVISION",
  "encoding": "json-text",
  "offset": 0,
  "fragment": "\"First part",
  "total_length": 10000,
  "next_cursor": "SIGNED_TOKEN",
  "complete": false
}
```

Fragments form the exact JSON text of one field value. Offsets count Unicode
codepoints, not JavaScript UTF-16 units or bytes. A fragment may split a JSON
escape sequence; use an incremental decoder or stream fragments directly to a
file and parse only complete JSON. Preserve the revision and original field
URL when continuing. Changed revisions return 409; discard prior fragments
and reload instead of concatenating different versions. Storage is never
shortened to create a preview.

Public DTOs include the public history count only. Owner storage fields,
internal media keys, external identity/provider metadata and private watch
history contents do not enter these responses or their field endpoints.

## Client behavior and export

Maintained consumers are `ShowcasePage` (homepage, shared view, owner preview)
and `UniversePage` in `CommunityPages` (directory). `usePublicCatalog` and
`lib/publicCatalog` coordinate requests. List cache is at most five pages;
facets retain one page per kind and five cursors; field reading retains at most
three frames and 256 KiB. Query/identity changes cancel work and clear its
dependent state. Facet and detail controls make further reading explicit.

Full public export uses `showSaveFilePicker` and a writable file stream after a
user gesture. It reads summary and `id-asc` pages of 50, reloads each entry's
detail, and streams any incomplete original fields through their JSON-text
continuations. Output is `animemo.public-catalog-export/v1`, with `scope`,
`consistency: "live"`, `exported_at`, `profile` and `records` containing the
original public fields. Preview metadata is excluded. This export format is
separate from the account Data Bundle restore format. A field download is the
original value's complete JSON text.

When the browser cannot provide a file stream, export reports that it is
unavailable. Failure, cancellation, query change or identity change aborts the
writer; only complete success closes it. Multi-request export retains live
consistency and can require a retry if a record changes during field reading.

## Errors, migration and database capability

Every failure body has exactly `code`, `detail`, `correlation_id`.

| Code | Status | Client action |
| --- | --- | --- |
| public_catalog_cursor_invalid | 400 | Discard invalid continuation |
| public_catalog_cursor_mismatch | 400 | Reload using the current route and conditions |
| public_catalog_cursor_expired | 410 | Restart the current read |
| public_catalog_revision_changed | 409 | Discard old field fragments and reload |
| not_found | 404 | Clear unavailable content |
| public_catalog_unavailable | 503 | Show unavailable state |
| public_catalog_budget_exceeded | 500 | Show the bounded failure |
| public_catalog_retired | 410 | Migrate the caller to these endpoints |

The release migration replaces the old `homepage/`, `showcase/{public_slug}/`
and `showcases/` operations with the fixed retired 410 response and
`Link: </api/v1/docs/>; rel="successor-version"`, on both Core prefixes.
The route names remain resolvable and return retirement responses. Maintained
consumers use the bounded endpoints. The retirement response does not read
business records or authenticate old bearer credentials.

Frontend SDK callers use `host.api.get("public/homepage/entries/", {params})`
and process each bounded response before requesting the next. External clients
using the retired paths must migrate; generic path forwarding alone does not
establish that every external consumer has done so. A deployment rollback must
retain a bounded implementation or explicit unavailability.

Production requires PostgreSQL and the migration-created nondeterministic ICU
collation `animemo_public_zh_cn_v1`, locale `zh-Hans-CN`. It preserves ICU-equal
Unicode title comparisons before unique keyset tie breakers. The server checks
provider, locale, determinism and stored/actual collation version; incompatible
or outdated capability returns 503. SQLite development instances also return
explicit 503 for these endpoints. Database ordering, distinct-facet work and
JSON serialization cost still scale with the authorized data; application
materialization and every individual response remain bounded.
