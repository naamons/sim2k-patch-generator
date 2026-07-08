# SIM2K Patch Generator

A PyQt6 desktop tool that generates ECU unlock patches for Hyundai/Kia **SIM2K250**, **SIM2K260**, and **SIM2K305** engine/transmission controllers.

## Features

- **One binary in, all files out** — load a boot read binary and get the complete patch set
- **51 calibrations** built into the database (covers most SIM2K250/SIM2K260 variants)
- **Auto-detection** of ECU type, calibration reference, and bootloader reference
- **UDS service 23 → 19** patch for full-flash read on secure-gateway vehicles
- **Custom password** support to lock out other tuners
- **Byte-identical output** to reference protected patch files

## Quick Start

```bash
# Install dependencies
pip install PyQt6

# Run the tool
python3 sim2k_patch_generator.py
```

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

## Database Coverage

| Bootloader | Calibrations | Unique level2 | Unique overwrites |
|---|---|---|---|
| 606A1_C2 | 29 | 3 | 8 |
| 606Z0_C2 | 15 | 3 | 6 |
| 606A4_C2 | 5 | 1 | 1 |
| 606A0_C2 | 2 | 1 | 1 |
| **Total** | **51** | **8** | **16** |

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

## License

This project is licensed under the GNU General Public License v3.0 — see the [LICENSE](LICENSE) file for details.

**Important:** If you distribute a modified version of this software, you must:
1. Make your source code available under the same GPL-3.0 license
2. Credit the original author
3. State the changes you made

This ensures that improvements to the tool remain open source and benefit the community.

## Credits

Based on the Hyundai/Kia SIM2K documentation and sample ECU binaries.
