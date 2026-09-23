-- Ordered by type
SELECT *
FROM image_rows
ORDER BY lower(substr(path,-3)), lower(title), lower(path);
