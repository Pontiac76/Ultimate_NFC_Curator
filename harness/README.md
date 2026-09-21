# U2+ API Test Harness

Small scripts for manual testing during development.

## Mount an image

```bash
./harness/u2_mount_image.py /C128/Games/128-Robots.d71 --catalog
./harness/u2_mount_image.py /C128/Games/Sonic_the_Hedgehog/Sonic_the_Hedgehog_R5.d81 --catalog
./harness/u2_mount_image.py /nibs/Elite.g64 --catalog
```

Paths may omit `/Usb0` or `/Usb1`; the helper tries last-known USB port first, then the other.

## Type keys into the C128

```bash
./harness/u2_type_keys.py 'CATALOG' --enter
```

The keyboard injection uses C128 keyboard buffer `$034A` and count `$00D0`.
