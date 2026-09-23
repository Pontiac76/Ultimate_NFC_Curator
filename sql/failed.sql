-- Failed / needs review
SELECT *
FROM image_rows
WHERE status = 'failed'
ORDER BY lower(title), lower(path);
