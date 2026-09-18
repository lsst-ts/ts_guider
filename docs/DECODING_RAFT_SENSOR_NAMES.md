# Decoding raft and sensor names from GDS metadata

How LSST focal-plane naming maps to `StampMetadata.sensor_index` in
`guiderGDS`, and how that relates to emulator FITS filenames.

---

## Focal plane layout

LSSTCam has a 5×5 grid of raft bays, named `R00` through `R44`
(row × column). The four corner rafts are the guider/wavefront rafts:

```text
        col 0   col 1   col 2   col 3   col 4
row 0   [R00]   R01     R02     R03     [R04]
row 1   R10     R11     R12     R13     R14
row 2   R20     R21     R22     R23     R24
row 3   R30     R31     R32     R33     R34
row 4   [R40]   R41     R42     R43     [R44]
```

Each corner raft has **2 guider sensors** (`SG0` and `SG1`) → **8
total** across the camera.

---

## GDS Location: bay / board / sensor

The SDK encodes every sensor as a `Location(bay, board, sensor)` triple:

| Field | Meaning | Range |
|-------|---------|-------|
| **bay** | Raft position: `row × 5 + col` | 0–24 (`MAX_BAYS=25`) |
| **board** | 0 = science, **1 = guider**, 2 = wavefront | 0–2 (`MAX_BOARDS=3`) |
| **sensor** | Which sensor on that board | 0–2 (`MAX_SENSORS=3`) |

### Example files (`MC_O_20260429_000027_*`)

| Filename | Raft | `gds_stampinfo` shows | bay | board | sensor |
|----------|------|------------------------|-----|-------|--------|
| `R00_SG0` | R00 | `00/1/0` | 0 | 1 | 0 |
| `R00_SG1` | R00 | `00/1/1` | 0 | 1 | 1 |
| `R04_SG0` | R04 | `04/1/0` | 4 | 1 | 0 |
| `R04_SG1` | R04 | `04/1/1` | 4 | 1 | 1 |
| `R40_SG0` | R40 | `40/1/0` | 20 | 1 | 0 |
| `R40_SG1` | R40 | `40/1/1` | 20 | 1 | 1 |
| `R44_SG0` | R44 | `44/1/0` | 24 | 1 | 0 |
| `R44_SG1` | R44 | `44/1/1` | 24 | 1 | 1 |

`gds_stampinfo` displays **bay** in raft naming form (`00`, `04`,
`40`, `44`), not the linear bay number.

---

## The packed index (`StampMetadata.sensor_index`)

`Location.index()` packs the triple into a single `uint8_t`:

```text
index = bay × 9 + board × 3 + sensor
        (MAX_BOARDS × MAX_SENSORS = 9)
```

### Verify against emulator output

| File | bay | board | sensor | **index** | **You saw** |
|------|-----|-------|--------|-----------|-------------|
| `R00_SG0` | 0 | 1 | 0 | 0×9 + 1×3 + 0 = **3** | `sensor=3` |
| `R00_SG1` | 0 | 1 | 1 | 0×9 + 1×3 + 1 = **4** | |
| `R04_SG0` | 4 | 1 | 0 | 4×9 + 1×3 + 0 = **39** | |
| `R04_SG1` | 4 | 1 | 1 | 4×9 + 1×3 + 1 = **40** | |
| `R40_SG0` | 20 | 1 | 0 | 20×9 + 1×3 + 0 = **183** | `sensor=183` |
| `R40_SG1` | 20 | 1 | 1 | 20×9 + 1×3 + 1 = **184** | `sensor=184` |
| `R44_SG0` | 24 | 1 | 0 | 24×9 + 1×3 + 0 = **219** | |
| `R44_SG1` | 24 | 1 | 1 | 24×9 + 1×3 + 1 = **220** | |

`sensor=183` and `sensor=184` match `R40_SG0` and `R40_SG1` exactly.

---

## Decoding back to raft / sensor name

```python
def decode_gds_index(index: int) -> str:
    bay = index // 9
    board = (index % 9) // 3
    sensor = index % 3
    raft_row = bay // 5
    raft_col = bay % 5
    return f"R{raft_row}{raft_col}/SG{sensor}"


# Examples:
# decode_gds_index(3)   -> 'R00/SG0'
# decode_gds_index(183) -> 'R40/SG0'
# decode_gds_index(184) -> 'R40/SG1'
```

---

## Multi-sensor emulation

Feed all eight FITS files to `gds_emulator` at once (use `600` ms to
match the FITS cadence from `gds_stampinfo`):

```bash
gds_emulator gds-emu 600 \
  guider_fits_emulator/MC_O_20260429_000027_R00_SG0_guider.fits \
  guider_fits_emulator/MC_O_20260429_000027_R00_SG1_guider.fits \
  guider_fits_emulator/MC_O_20260429_000027_R04_SG0_guider.fits \
  guider_fits_emulator/MC_O_20260429_000027_R04_SG1_guider.fits \
  guider_fits_emulator/MC_O_20260429_000027_R40_SG0_guider.fits \
  guider_fits_emulator/MC_O_20260429_000027_R40_SG1_guider.fits \
  guider_fits_emulator/MC_O_20260429_000027_R44_SG0_guider.fits \
  guider_fits_emulator/MC_O_20260429_000027_R44_SG1_guider.fits
```

With `guiderGDS.LocationSet.any()`, the subscriber receives interleaved
stamps from all eight sensors. Each stamp's `metadata.sensor_index`
identifies the source (3, 4, 39, 40, 183, 184, 219, 220).

**`metadata.stamp_index`** groups stamps across sensors: all sensors
sharing the same `stamp_index` were acquired at the same instant and
form one combined offset measurement downstream.

---

## Other `StampMetadata` fields

| Field | Meaning |
|-------|---------|
| `timestamp_ns` | TAI nanoseconds from `GDS::StateMetadata` |
| `sequence` | Per-Start counter; constant within one guide series |
| `stamp_index` | Per-stamp index within the series; grouping key for multi-sensor offsets |
