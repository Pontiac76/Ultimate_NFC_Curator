# Design Notes

## One NFC reader per launcher instance

Decision: the NFC launcher/monitor is intentionally **1:1**.

One Python process monitors one NFC reader and controls one Ultimate/U2+ target.

Do **not** expand this into one process managing multiple NFC readers and multiple machines.

Reasoning:

- Multiple readers require a mapping layer: reader A -> Ultimate A, reader B -> Ultimate B, etc.
- USB reader enumeration can change when devices are unplugged/replugged.
- If a reader is swapped, moved, or replaced, the software then needs persistent identity/mapping management.
- That adds operational complexity for very little benefit.
- The intended deployment is small devices capable of running Python, e.g. Raspberry Pi, each placed next to the machine it controls.
- Each device runs one launcher script, watches one NFC reader, and controls one U2+/Ultimate.

If future-me suggests multi-reader support, reject it unless there is a very strong reason. The simple deployment model is:

```text
one reader + one Python launcher + one Ultimate target
```

Scale by adding another small host/device, not by making one launcher multiplex multiple readers.
