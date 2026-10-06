# CATEGORY_SPEC.md - the mandatory per-category specification

| | |
| --- | --- |
| Step | FOUNDATION - STEP 3 (CATEGORY SPEC) |
| Applies to | every category in the global category catalog |
| Owns | the SPEC.md format, the canonical content hash, the validation rules |
| Does not own | the catalog (STEP 2), the project database, LIBRARY, the project strategy (STEP 7) |

## 1. Purpose

Every category carries exactly one **authoritative** specification document:

```
<global categories root>/<category_id>/SPEC.md
```

i.e. on Windows `%LOCALAPPDATA%\ArchitectureAssistant\categories\<category_id>\SPEC.md`
(the entry :data:`architecture_assistant.domain.category.SPEC_ENTRY_NAME`, one of
``REQUIRED_CATEGORY_ENTRIES``). It is human-readable Markdown, it is written and
maintained by a human operator, and it is the description later steps consume:

* the global catalog shows it and refuses to **enable** a category without a valid one;
* STEP 5 will persist the validated identity, version and content hash into the
  project traceability when a project is created after category selection;
* STEP 7 derives the project strategy from this document.

Loading is strictly validating: a missing, malformed or tampered document is
**refused**, never repaired, and nothing is created or rewritten automatically.

## 2. Document format, version 1

A ``SPEC.md`` is UTF-8 Markdown with three parts:

1. a **fenced JSON metadata object** - the first line is exactly `` ```json `` and
   the block ends with a line that is exactly `` ``` ``;
2. **one blank separator line**;
3. the **description** - nonblank Markdown, everything after the separator, with
   every whitespace character preserved (see section 5).

One optional leading UTF-8 byte-order mark is tolerated, and CRLF/CR line endings
are normalized to LF before parsing. Inside the metadata block the JSON itself may
be pretty-printed, compact, key-sorted or key-reversed - it must simply be one
object carrying exactly the five keys of section 3.

## 3. Metadata

| key | type | rule |
| --- | --- | --- |
| ``schema_version`` | integer | must be exactly ``1`` (no bool, no string) |
| ``category_id`` | string | the immutable slug ``^[a-z0-9][a-z0-9_-]*$``, at most 64 characters, matching the folder name and the catalog row id |
| ``display_name`` | string | nonblank human-readable name, equal to the catalog row's name |
| ``version`` | string | nonblank category version, equal to the catalog row's version (e.g. ``"1.0"``) |
| ``spec_hash`` | string | ``sha256:`` + 64 lower-case hex characters (section 4) |

A **missing** key, an **unknown** key, a **duplicated** key or a **wrong type** is
rejected. Nothing else may appear in the object.

## 4. The canonical content hash

``spec_hash`` is computed over a canonical serialization of the semantic content -
the metadata fields and the description, and nothing else:

```python
payload = {
    "schema_version": 1,
    "category_id": "software",
    "display_name": "Software",
    "version": "1.0",
    "description": DESCRIPTION,
}
canonical = json.dumps(
    payload, sort_keys=True, ensure_ascii=False,
    separators=(",", ":"), allow_nan=False,
)
spec_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Deliberately **excluded**: the declared ``spec_hash`` itself and the document
framing (the fences and the separator line). Consequently

* metadata key order, JSON formatting (indented or compact) and Unicode escaping
  do **not** change the hash;
* one optional BOM and the line-ending style (LF, CRLF or CR) do **not** change it;
* **any** change of the description does - including padding and trailing
  whitespace, because the description is taken verbatim (section 5);
* content that cannot be encoded as UTF-8 is **refused**: an escaped lone
  surrogate such as ``"\ud800"`` - which ``json.loads`` turns back into a real
  surrogate - is a malformed spec, never hashed and never leaked as a raw
  ``UnicodeEncodeError``.

This hash is **not** the STEP 2 ``registry_hash``, which covers the catalog row
only (id, display name, version and the three canonical relative entry paths).
The two hashes are independent and are never merged.

## 5. What exactly is "the description"?

Everything strictly after the blank separator line, up to the end of the file,
taken verbatim. If the file ends with a newline the description ends with that
newline; if it does not, it does not. Adding or removing even one whitespace
character therefore changes ``spec_hash``, and the document must be re-hashed.

## 6. Example (with a verified hash)

``categories/software/SPEC.md``:

````text
```json
{
  "schema_version": 1,
  "category_id": "software",
  "display_name": "Software",
  "version": "1.0",
  "spec_hash": "sha256:66cd6777d32d94f089b47675e3dad44cc3fe2a6838b80a196bfffa7e13f41593"
}
```

# Software

Specifies a software project: its goals, users, constraints,
architecture concerns and expected results.
````

The declared hash above is the hash of the canonical payload of that exact
document (canonical string):

```text
{"category_id":"software","description":"# Software\n\nSpecifies a software project: its goals, users, constraints,\narchitecture concerns and expected results.\n","display_name":"Software","schema_version":1,"version":"1.0"}
```

It was verified independently with plain ``json``/``hashlib`` and equals
``sha256:66cd6777d32d94f089b47675e3dad44cc3fe2a6838b80a196bfffa7e13f41593``.

## 7. Authoring procedure

1. Create the category content folder under the global categories root. Its name
   **is** the immutable id: `%LOCALAPPDATA%\ArchitectureAssistant\categories\<category_id>\`.
2. Write the future description to a scratch file (e.g. `SPEC_body.md`) - exactly,
   with every whitespace character that should be hashed.
3. Compute the content hash:

   ```powershell
   $env:PYTHONPATH = "src"
   python -c "import pathlib, architecture_assistant.domain.category_spec as s; print(s.compute_spec_hash(schema_version=1, category_id='software', display_name='Software', version='1.0', description=pathlib.Path('SPEC_body.md').read_text(encoding='utf-8')))"
   ```

   An independent double-check (must print the same value):

   ```powershell
   python -c "import hashlib, json, pathlib; d = pathlib.Path('SPEC_body.md').read_text(encoding='utf-8'); s = json.dumps({'schema_version':1,'category_id':'software','display_name':'Software','version':'1.0','description':d}, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False); print('sha256:' + hashlib.sha256(s.encode('utf-8')).hexdigest())"
   ```
4. Assemble `SPEC.md`: the `json` fence with the five metadata keys, one blank
   line, then the body verbatim (as bytes - no editor newline translation).
5. Validate before relying on it (never trust a hand-computed hash):

   ```powershell
   python -c "import pathlib; from architecture_assistant.domain.category_spec import parse_category_spec; parse_category_spec(pathlib.Path(r'%LOCALAPPDATA%\ArchitectureAssistant\categories\software\SPEC.md').read_text(encoding='utf-8'), category_id='software', display_name='Software', version='1.0'); print('ok')"
   ```
6. Register the category (if new) and enable it through the **audited** catalog
   API - every mutation needs an operator and a reason. The GUI shows the row as
   `ENABLED`, or it prints the exact reason it cannot be offered.

## 8. Readiness rules (where SPEC.md is enforced)

A category is `ENABLED` - and therefore selectable for a new project - only when

* its referenced entries exist (STEP 2 rule): `SPEC.md`, `LIBRARY`, `SUPERVISOR_RULES.md`;
* `spec_path` is exactly `SPEC.md` (a noncanonical path is never ready);
* `SPEC.md` is a regular, readable file that decodes as UTF-8, and its resolved
  path stays inside the category folder and the global categories root;
* the document parses, its recomputed hash equals the declared one, its content
  is UTF-8-encodable (an escaped lone surrogate is a content failure, not a
  crash) and its identity agrees with the catalog row (id, display name, version);
* no other registered category uses the same display name (exact, case-sensitive).

Validation is centralized in `CategoryCatalogUseCase` and runs on **every**
readiness entry point: `categories()`, `get()`, `selectable()`,
`register(enabled=True)`, `enable()` - including the *already enabled* branch -
and `update_metadata()` on an enabled row. Reads never mutate the catalog, and a
validation failure before a mutation leaves both the row and the audit trail
untouched.

| situation | presented as |
| --- | --- |
| enabled and valid | `ENABLED` (selectable) |
| enabled but broken on disk (SPEC changed, removed or tampered after enablement) | `INVALID` + diagnostic, never selectable |
| disabled (valid or not) | `DISABLED`, with a diagnostic when something is wrong |
| unregistered | absent (`get` returns `None`) |

Nothing is hidden and nothing is silently repaired.

## 9. Compatibility and controlled updates

* Validation applies to **selection**, not to history. Catalog rows, the
  append-only audit trail, existing projects and their saved category selections
  are preserved.
* A category whose SPEC.md is still a STEP 2 placeholder - or whose `spec_path` is
  noncanonical - becomes **unselectable until an operator corrects it**. The
  diagnostic names the reason; the row stays visible.
* To change a display name or version: **disable** the category (audited), revise
  `SPEC.md` and its hash, **update** the row (audited), then **enable** it again.
  Renaming one of two duplicate display names follows the same order. An **id is
  never renamed in place** - it is the primary key and the folder name.
* The filesystem content and the SQLite row are not one atomic resource, so
  readiness is always evaluated on a **fresh read**. STEP 5 will revalidate and
  persist the validated snapshot (identity, version, SPEC hash) when it records
  project traceability.

## 10. What STEP 3 deliberately does not do

* create, rewrite, scaffold or repair a `SPEC.md` - not even a missing hash;
* open, read or create a **project database** (SPEC loading touches files only,
  and the catalog remains a database of its own);
* change the catalog schema, the STEP 2 `registry_hash`, the
  `CategorySelection` contract or any other STEP 2 behaviour except adding the
  validation described above;
* implement the category LIBRARY (STEP 4), the project creation flow (STEP 5) or
  `PROJECT_STRATEGY.md` (STEP 7).

## 11. Where the rules live

| concern | module |
| --- | --- |
| document format, canonical serialization, hash, identity binding, `CategorySpec` | `src/architecture_assistant/domain/category_spec.py` |
| read-only port and its typed storage errors | `src/architecture_assistant/ports/category_spec.py` |
| the one filesystem reader (fresh, contained, UTF-8) | `src/architecture_assistant/infrastructure/category_spec.py` |
| readiness diagnostics, `load_spec()`, display-name uniqueness | `src/architecture_assistant/application/category_catalog.py` |
| wiring | `src/architecture_assistant/composition/category_catalog.py` |
| tests | `tests/test_category_spec.py`, `tests/test_category_catalog.py`, `tests/test_category_catalog_no_project_db.py`, `tests/test_registry_hash.py` |

Manual verification of a single document, with no catalog and no database:

```powershell
$env:PYTHONPATH = "src"
python -c "import pathlib; from architecture_assistant.domain.category_spec import parse_category_spec; s = parse_category_spec(pathlib.Path('SPEC.md').read_text(encoding='utf-8'), category_id='software', display_name='Software', version='1.0'); print(s.spec_hash)"
```

