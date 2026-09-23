-- Approved C128 cards
SELECT *
FROM image_rows
WHERE status = 'approved' and machine_mode like 'c128'
ORDER BY lower(title), lower(path);
