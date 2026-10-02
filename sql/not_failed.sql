-- Not failed
SELECT *
FROM image_rows
WHERE COALESCE(status, '') != 'failed'
  AND COALESCE(storage_status, 'present') != 'deleted'
ORDER BY title COLLATE NOCASE, path COLLATE NOCASE;