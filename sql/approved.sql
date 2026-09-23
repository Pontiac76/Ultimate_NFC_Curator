-- Approved cards
SELECT *
FROM image_rows
WHERE status = 'approved'
ORDER BY lower(title), lower(path);
