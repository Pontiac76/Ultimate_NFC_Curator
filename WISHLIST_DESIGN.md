# Wishlist Design Notes

## Files

- `wishlist.txt`
  - Current active wishlist shown by Curator with Shift-W.
  - Managed by the TUI.
  - `x` in wishlist selector removes a line after confirmation.

- `wishlist_all.txt`
  - Long-term pool/history of all titles that have ever been suggested/wishlisted.
  - Not managed by the TUI.
  - Do not delete from this file automatically.
  - Whenever an AI/session suggests new wishlist titles and writes/adds them to `wishlist.txt`, append those same new titles to `wishlist_all.txt` if not already present.
  - Future AI/context sessions may read this file to suggest/refill `wishlist.txt` with more candidate searches.

## TUI Flow

Shift-W from normal curator:

1. Opens wishlist selector from `wishlist.txt`.
2. Enter selects a title.
3. Clears `a64_inbox/` immediately with no resume/new prompt.
4. Opens A64 search form with only:
   - `Name = selected wishlist title`
   - all other fields blank.
5. After download, enters A64 inbox as normal.

Shift-W is intentionally disabled inside A64 inbox.

## Future Responsibilities

When asked to refresh/regenerate the active wishlist:

1. Read `wishlist_all.txt`.
2. Consider what has already been approved/promoted/tested in `curator.db` if useful.
3. Generate/update `wishlist.txt` with a manageable current batch.
4. Append any newly suggested titles to `wishlist_all.txt` as well, de-duplicating case-insensitively.
5. Preserve `wishlist_all.txt` as historical source/pool; never prune it automatically.

## Current Caveats

- Wishlist entries are plain text, one title per line.
- No priority/status fields yet.
- No automatic removal after successful promotion.
- No integration with Everything/local archive provider yet.
