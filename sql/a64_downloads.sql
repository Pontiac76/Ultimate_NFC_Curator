-- A64 intake/download candidates.
-- Shows files downloaded from Assembly64 into the local inbox, plus promotion state.
SELECT
  title,
  local_path AS path,
  local_path AS payload,
  'local' AS mode,
  file_type AS type,
  file_type,
  original_filename,
  group_name,
  year,
  a64_id,
  a64_category,
  entry_index,
  size_bytes,
  local_path,
  a64_status,
  promoted_path
FROM a64_download_rows
ORDER BY
  CASE a64_status
    WHEN 'downloaded' THEN 0
    WHEN 'seen' THEN 1
    WHEN 'tested' THEN 2
    WHEN 'failed' THEN 3
    WHEN 'promoted' THEN 4
    WHEN 'deleted' THEN 5
    WHEN 'discarded' THEN 5
    ELSE 9
  END,
  title COLLATE NOCASE,
  a64_id,
  entry_index;
