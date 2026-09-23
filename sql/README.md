# SQL view files

The curator TUI's `V` view picker loads `*.sql` files from this directory.

- The first line may be a SQL comment beginning with `--`; that text becomes the menu title.
- Use `SELECT` queries. The result order is preserved in the TUI.
- Prefer querying `image_rows`, the flat view over the normalized schema.
- Broken SQL leaves the previous view active and shows an error popup.

Example:

```sql
-- CRT images
SELECT *
FROM image_rows
WHERE file_type = 'crt'
ORDER BY lower(title), lower(path);
```
