-- Resurfaced / returned images
SELECT *
FROM image_rows
WHERE storage_status = 'returned'
ORDER BY lower(title), lower(path);
