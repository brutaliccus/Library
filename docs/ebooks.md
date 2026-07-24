# Ebook pipeline (DIY organizer)

Ebooks do **not** use LibraForge or a second library app. After download they run a
local organizer that mirrors ABS/Folder Forge layout as closely as Kavita allows.

## Flow

1. **Download** lands in `/ebooks/unorganized/req_{id}_{slug}/`  
   (host: `/mnt/eBooks/unorganized/…`).
2. **Convert** MOBI/AZW → EPUB when Calibre `ebook-convert` is available.
3. **Metadata** — request catalog volume → ISBN (OL / Google / ISBNdb) →
   title+author (Hardcover). Score must be ≥ `EBOOK_MIN_SCORE` (default `0.70`)
   or the request is **quarantined**.
4. **Embed** — Calibre `ebook-meta` writes title/author/series into OPF (Kavita
   series grouping depends on series tags).
5. **Organize** — single primary file into:
   - `{author}/{series}/{title}/` or
   - `{author}/{series} [{edition}]/{title}/` when edition is known, or
   - `{author}/{title}/` when there is no series  
   Filename = sanitized title + `.epub` (or best remaining format).
6. **Finalize** — wipe staging tree, `Kavita` library scan, mark completed.

Statuses reused from the audiobook pipeline (no M4B):  
`metadata_forge` → `folder_forge` → `finalizing` → `completed` | `quarantined`.

## Kavita: exclude `unorganized`

Staging is a **non-dot** folder named `unorganized` under the ebook library root.
Kavita must **not** index it (Library → folder / exclude settings). On this
deployment that exclusion is already configured; if you rebuild the library,
re-add `unorganized` to the ignore/exclude list so quarantine drops never appear
as series.

After a successful organize, Library Site calls Kavita’s scan API so the new
`Author/…/Title` path shows up in My Library.

## Admin review

Quarantined ebooks keep files under `unorganized`. Admin → Requests offers:

- **Staging files** — browse / delete entries
- **Continue pipeline** — skip the confidence gate, organize + scan
- **Reject / delete** — wipe the request’s `unorganized/req_{id}_*` tree

There is no LibraForge Manual Review link for ebooks.

## Env knobs

| Variable | Default | Meaning |
|----------|---------|---------|
| `EBOOK_PIPELINE_ENABLED` | `true` | Staging + organizer (when false: legacy Author/Title drop) |
| `EBOOK_MIN_SCORE` | `0.70` | Quarantine below this confidence |
| `EBOOK_DIR` | `/ebooks` | Container path (host via `EBOOK_HOST_DIR`) |
