# NFC Card Audit Design Notes

This is a design note only. It is not implemented yet.

## Goal

Track physical NFC card counts before and after a show without relying on NFC UID identity.

Multiple physical cards may intentionally point to the same image path/payload, so the audit model counts scans by path/payload rather than trying to uniquely identify a card.

## Concepts

An audit has a parent row. Each card tap adds one child scan row. Duplicate paths are allowed and meaningful because they represent multiple physical cards with the same payload.

Pre-show scanning verifies what was packed. Post-show scanning verifies what came back.

## Tables

### CardAuditMode

Lookup table.

```sql
CardAuditMode
  pk_ID INTEGER PRIMARY KEY AUTOINCREMENT
  name TEXT NOT NULL UNIQUE
  label TEXT NOT NULL
```

Seed values:

```text
pre_show
post_show
```

### CardAuditState

Lookup table.

```sql
CardAuditState
  pk_ID INTEGER PRIMARY KEY AUTOINCREMENT
  name TEXT NOT NULL UNIQUE
  label TEXT NOT NULL
```

Seed values:

```text
open
paused
closed
```

### CardAudit

Parent audit session.

```sql
CardAudit
  pk_ID INTEGER PRIMARY KEY AUTOINCREMENT
  name TEXT NOT NULL
  fk_CurrentCardAuditMode_ID INTEGER NOT NULL REFERENCES CardAuditMode(pk_ID)
  fk_CardAuditState_ID INTEGER NOT NULL REFERENCES CardAuditState(pk_ID)
  opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
  closed_at TEXT
  notes TEXT NOT NULL DEFAULT ''
```

The parent tracks the current audit mode. A single audit begins in `pre_show`, may transition to `post_show`, and must never transition back to `pre_show` after that.

### CardAuditScan

One row per scanned/tapped card.

```sql
CardAuditScan
  pk_ID INTEGER PRIMARY KEY AUTOINCREMENT
  fk_CardAudit_ID INTEGER NOT NULL REFERENCES CardAudit(pk_ID)
  fk_CardAuditMode_ID INTEGER NOT NULL REFERENCES CardAuditMode(pk_ID)
  path TEXT NOT NULL
  payload TEXT NOT NULL DEFAULT ''
  scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
```

No unique constraint on `(fk_CardAudit_ID, fk_CardAuditMode_ID, path)`. Duplicate rows are intentional and count duplicate physical cards.

## State transition rules

### New audit

```text
state = open
current_mode = pre_show
opened_at = now
closed_at = NULL
```

### Pause

```text
state = paused
```

No scans should be accepted while paused.

### Resume

```text
state = open
```

### Switch to post-show

Allowed:

```text
pre_show -> post_show
```

Not allowed:

```text
post_show -> pre_show
```

### Close audit

```text
state = closed
closed_at = now
```

A report can be generated after close.

### Reopen audit

Allowed only for post-show continuation:

```text
closed + current_mode = post_show -> open + current_mode = post_show
```

This supports finding a missing card after closing the audit and adding another post-show scan.

Do not allow reopening into pre-show once the audit has moved to post-show.

## Report query

SQLite-compatible pre/post comparison using counts per path:

```sql
WITH
pre AS (
  SELECT cas.path, COUNT(*) AS pre_count
  FROM CardAuditScan cas
  JOIN CardAuditMode cam ON cam.pk_ID = cas.fk_CardAuditMode_ID
  WHERE cas.fk_CardAudit_ID = :audit_id
    AND cam.name = 'pre_show'
  GROUP BY cas.path
),
post AS (
  SELECT cas.path, COUNT(*) AS post_count
  FROM CardAuditScan cas
  JOIN CardAuditMode cam ON cam.pk_ID = cas.fk_CardAuditMode_ID
  WHERE cas.fk_CardAudit_ID = :audit_id
    AND cam.name = 'post_show'
  GROUP BY cas.path
),
paths AS (
  SELECT path FROM pre
  UNION
  SELECT path FROM post
)
SELECT
  paths.path,
  COALESCE(pre.pre_count, 0) AS pre_count,
  COALESCE(post.post_count, 0) AS post_count,
  COALESCE(post.post_count, 0) - COALESCE(pre.pre_count, 0) AS delta
FROM paths
LEFT JOIN pre ON pre.path = paths.path
LEFT JOIN post ON post.path = paths.path
WHERE COALESCE(pre.pre_count, 0) != COALESCE(post.post_count, 0)
ORDER BY delta, paths.path;
```

Interpretation:

```text
delta < 0  missing cards
delta > 0  extra/new cards found after show
delta = 0  balanced, omitted from report
```

## Future optional resolution table

Missing/extra results may eventually need explicit resolution tracking.

```sql
CardAuditResolution
  pk_ID INTEGER PRIMARY KEY AUTOINCREMENT
  fk_CardAudit_ID INTEGER NOT NULL REFERENCES CardAudit(pk_ID)
  path TEXT NOT NULL
  quantity INTEGER NOT NULL DEFAULT 1
  fk_CardAuditResolutionType_ID INTEGER NOT NULL
  notes TEXT NOT NULL DEFAULT ''
```

Possible resolution types:

```text
rewritten
known_missing
ignore
unregistered
found_later
```
