# Cartridge Boot / Accelerator Ideas

Design note only; not implemented yet.

Some disk images may need or benefit from a cartridge being mounted before boot, e.g. FastLoad-style cartridges or other helpers for images that cannot be converted cleanly to D71/D81.

Future schema idea:

```sql
ImageBootCartridge
  pk_ID INTEGER PRIMARY KEY AUTOINCREMENT
  fk_Image_ID INTEGER NOT NULL REFERENCES Image(pk_ID)
  cartridge_path TEXT NOT NULL
  enabled INTEGER NOT NULL DEFAULT 1
  notes TEXT NOT NULL DEFAULT ''
```

Possible workflow:

1. Clear existing cartridge by default before reset/reboot.
2. If selected image has an enabled boot cartridge, configure/mount that CRT.
3. Reboot/reset into the desired target mode.
4. Mount disk image and continue launch flow.

Current behavior implemented now:

- TUI reboot/reset paths clear the configured U2 cartridge setting before reset/reboot.
- This prevents a previously launched CRT from unexpectedly surviving into later C64/C128 boots.
