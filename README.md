# SIM2K Patch Generator

A PyQt6 desktop tool that generates ECU unlock patches for Hyundai/Kia **SIM2K250**, **SIM2K260**, and **SIM2K305** engine/transmission controllers.

## Features

- **One binary in, all files out** — load a boot read binary and get the complete patch set
- **Auto-detection** of ECU type, calibration reference, and bootloader reference
- **Algorithmic level2 generation** — CBOOT is patched in-place using the
  Siemens CRC32 checksum algorithm; no pre-built level2 samples required
- **UDS service 23 → 19** patch for full-flash read on secure-gateway vehicles
- **Custom password** support to lock out other tuners
- **Byte-identical output** to reference protected patch files (when database is available)

## Quick Start

```bash
# Install dependencies
pip install PyQt6

# Generate database from your ECU samples (optional, but recommended)
python3 generate_db.py /path/to/your/Hyundai/samples

# Run the tool
python3 sim2k_patch_generator.py
```

## Database Generation

The tool works with or without a database of known CBOOT / overwrite files.

**Level2 CBOOT** is now **generated algorithmically** from the original CBOOT
using `clams_level2.py`. The generator reproduces the patch logic from the
original SIM2K toolbox:

1. Enable developer mode (byte patch on the DevMode hook)
2. Boost the TesterPresent S3 timer
3. Extend UDS service `0x23` read range (optional)
4. Replace UDS service `0x23` with `0x19` for full-flash read (optional)
5. Recompute the two Siemens CRC32 structures at offsets `0x300` and `0x340`

The bootloader physical address is **auto-detected** from the CBOOT's own CRC
structure — no manual layout selection required.

The optional database (for CBOOT original resolution and overwrite content)
can still be generated from your own ECU samples:

```bash
python3 generate_db.py /path/to/your/samples
```

The samples directory should contain calibration folders with:
- **CBOOT subfolders** (e.g. `606A1_C2/`) containing `.bin` files
- **Overwrite files** (e.g. `606TA051.606A1_C2.bin`)

The script will generate `sim2k_db.py` with all the mappings.

**Without the database**, the tool will still work:
- Level2 CBOOT is generated algorithmically (no samples needed)
- CBOOT original must be provided via the override field, or be present in the boot read
- Overwrite data will be generated from the ASW hook pattern (stub)

## Usage

1. Click **Browse** and select your boot read binary (e.g. `[Original] ... .bin`)
2. ECU type auto-detects — leave it on **Auto-detect**
3. Leave the **UDS service 23 → 19** checkbox enabled (default)
4. Optionally enter a **custom password** (8 bytes hex) to lock out other tuners
5. Click **Generate Patches**, choose an output folder
6. Done — all patch files are in the output folder

## Output Structure

```
<output>/
├── ASW/
│   ├── asw.bin              # Original ASW
│   ├── asw.trojan.bin       # Patched ASW (with overwrite applied)
│   └── asw.overwrite.bin    # Overwrite records
├── CBOOT/
│   ├── cboot.bin            # Original CBOOT
│   └── cboot.level2.bin     # Patched CBOOT
├── DATA/
│   └── data.bin             # Calibration data
└── [Protected]_SIM2K250_patch.bin   # ← this is the file you flash via OBD
```

## Custom Password

Enter an 8-byte hex password (e.g. `DEADBEEFCAFEBABE`) to lock out other tuners.

The password is written to offset `0x0F4C` in the level2 CBOOT. Other tuners who don't know the password cannot unlock the ECU via OBD.

## Protected Container Layout (SIM2K250)

| Offset | Size | Content |
|---|---|---|
| 0x000000 | 0x280000 | Original full binary (ASW + DATA) |
| 0x280000 | 0x10000 | Original CBOOT |
| 0x290000 | 0x10000 | Level2 CBOOT (patched) |
| 0x2A0000 | 0x1000 | Overwrite data |

## Requirements

- Python 3.9+
- PyQt6 >= 6.5.0

## Files

| File | Description |
|---|---|
| `sim2k_patch_generator.py` | Main application |
| `clams_level2.py` | Algorithmic level2 CBOOT generator (Siemens CRC32 + pattern patcher) |
| `generate_db.py` | Database generator script |
| `sim2k_db.py` | Generated database (not included, create your own) |

## License

This project is licensed under the GNU General Public License v3.0 — see the [LICENSE](LICENSE) file for details.

**Important:** If you distribute a modified version of this software, you must:
1. Make your source code available under the same GPL-3.0 license
2. Credit the original author
3. State the changes you made

This ensures that improvements to the tool remain open source and benefit the community.

## Credits

Based on the Hyundai/Kia SIM2K documentation and sample ECU binaries.
