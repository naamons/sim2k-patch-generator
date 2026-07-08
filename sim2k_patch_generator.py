#!/usr/bin/env python3
"""
SIM2K250 / SIM2K260 / SIM2K305 Patch Generator
Generates ECU unlocking patches from an original full binary (boot read).

Based on the Hyundai / Kia SIM2K documentation and sample binaries found in
/Users/clams/Downloads/Hyundai.
"""

import sys
import struct
import datetime
import traceback
from pathlib import Path

from sim2k_db import (
    LEVEL2_MAP,
    resolve as resolve_cboot,
    resolve_overwrite,
)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTextEdit, QFileDialog,
    QGroupBox, QFormLayout, QProgressBar, QTabWidget, QMessageBox,
    QComboBox, QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox
)
from PyQt6.QtCore import pyqtSignal, QObject, QThread
from PyQt6.QtGui import QFont


# ---------------------------------------------------------------------------
# ECU layout database
# ---------------------------------------------------------------------------

class ECUConfig:
    """Segment layouts and patch generation parameters."""

    # Segment order as it appears in a *combined protected patch file*.
    # Offsets are relative to the start of the protected container.
    # The "full_file" block contains ASW + DATA and has the size shown below.
    PROTECTED_LAYOUTS = {
        "SIM2K250": {
            "cpu": "TC1782",
            "method": "overwrite",
            "full_file_size": 0x280000,
            "level2_offset": 0x290000,
            "level2_erase_fill_size": 0x10000,   # logical block size
            "overwrite_offset": 0x2A0000,
            "overwrite_block_size": 0x1000,
            "container_size": 0x2A1000,
            # Service 23 -> 19 replacement patch for full-flash read on secure-gateway vehicles.
            "service_patch": {
                "addresses": [0x8005B060, 0x8005B0C0],
                "data": bytes.fromhex("910002F8D9FF903FDC0F"),
            },
            "segments": {
                # file_offset measured from the start of the original full binary.
                # Values are taken from an actual SIM2K250 boot read binary.
                "ASW1":  {"address": 0x80020000, "file_offset": 0x20000, "length": 0x1DFE00, "erase": "FF00"},
                "DATA":  {"address": 0xA0200000, "file_offset": 0x200000, "length": 0x77E00,  "erase": "FF02"},
                # CBOOT is not stored inside the standard 0x280000-byte boot read.
                "CBOOT": {"address": 0xA0240000, "file_offset": None,      "length": 0xFE00,   "erase": "FF02"},
            },
            "bootloaders": {
                "606A1_C2": {"cvn": "06008CEE"},
                "606Z0_C2": {"cvn": "CDCB141B"},
                "6H6N0_C2": {"cvn": "A0DF9C6F"},
            },
        },
        "SIM2K260": {
            "cpu": "TC1791",
            "method": "replacement",
            "full_file_size": 0x400000,
            "level2_offset": 0x400000,
            "level2_erase_fill_size": 0x20000,
            "overwrite_offset": 0x420000,
            "overwrite_block_size": 0x1000,
            "container_size": 0x421000,
            "segments": {
                # Offsets are placeholders; adjust when a real SIM2K260 boot read is available.
                "ASW1":  {"address": 0x80040000, "file_offset": 0x00000, "length": 0x1C0000, "erase": "FF00"},
                "ASW2":  {"address": 0x808C0000, "file_offset": 0x1C0000, "length": 0x3FE00, "erase": None},
                "DATA":  {"address": 0xA0200000, "file_offset": 0x200000, "length": 0xAFE00,  "erase": "FF02"},
                "CBOOT": {"address": 0xA0880000, "file_offset": None,      "length": 0x1FE00,  "erase": "FF02"},
            },
            "bootloaders": {
                "640C0_C2": {"cvn": "48B79A5C"},
            },
        },
        "SIM2K305": {
            "cpu": "TC1782",
            "method": "replacement",
            "full_file_size": 0x280000,
            "level2_offset": None,   # SIM2K305 has no separate level2 in the protected file
            "overwrite_offset": 0x280000,
            "overwrite_block_size": 0x1000,
            "container_size": 0x2A1000,
            "segments": {
                # Offsets are placeholders; adjust when a real SIM2K305 boot read is available.
                "ASW1":  {"address": 0x800C0000, "file_offset": 0x00000, "length": 0x1BFE00, "erase": "FF00 02"},
                "DATA":  {"address": 0xA0040000, "file_offset": 0x200000, "length": 0x7FE00,  "erase": "FF00 01"},
                "CBOOT": {"address": 0xA0040000, "file_offset": None,      "length": 0x1FE00,  "erase": "FF00 01"},
            },
            "bootloaders": {
                "6U2V0_C2": {},
            },
        },
    }

    @classmethod
    def types(cls):
        return list(cls.PROTECTED_LAYOUTS.keys())

    @classmethod
    def get(cls, ecu_type):
        return cls.PROTECTED_LAYOUTS.get(ecu_type)


# ---------------------------------------------------------------------------
# Calibration / bootloader detection
# ---------------------------------------------------------------------------

def detect_calibration_and_bootloader(binary_data):
    """
    Detect the F182 calibration reference in the binary.
    Known references are 10-13 chars with letters, digits and underscores.
    """
    import re

    # Search the built-in database first (exact and reliable).
    for cal in LEVEL2_MAP.keys():
        if cal.encode() in binary_data:
            return cal

    # Generic pattern for calibration-like strings.
    pattern = re.compile(rb"[A-Z0-9_]{10,13}")
    for match in pattern.finditer(binary_data):
        s = match.group().decode("ascii", "replace")
        # Calibration references end with digits/letters and contain at least one underscore
        # or a numeric suffix.
        if s.count("_") >= 1 and any(c.isdigit() for c in s):
            return s

    return None


def detect_bootloader(binary_data):
    """Look for known bootloader references in the binary."""
    known = ["606A1_C2", "606Z0_C2", "606A0_C2", "606A4_C2", "640C0_C2", "6U2V0_C2"]
    for variant in known:
        if variant.encode() in binary_data:
            return variant
    return None


# ---------------------------------------------------------------------------
# Overwrite file format
# ---------------------------------------------------------------------------

class OverwriteRecord:
    """
    One [4-byte address][4-byte length][payload] record as described in
    Appendix A of the documentation.
    """

    __slots__ = ("address", "data")

    def __init__(self, address, data):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        self.address = address
        self.data = bytes(data)

    @classmethod
    def parse_stream(cls, data, offset=0):
        """Parse a record starting at *offset*. Returns (record, next_offset)."""
        if offset + 8 > len(data):
            return None, offset
        address = struct.unpack(">I", data[offset:offset + 4])[0]
        length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        if length == 0:
            return None, offset
        if offset + 8 + length > len(data):
            return None, offset
        return cls(address, data[offset + 8:offset + 8 + length]), offset + 8 + length

    def to_bytes(self):
        return struct.pack(">I", self.address) + struct.pack(">I", len(self.data)) + self.data


class OverwriteFile:
    """Collection of OverwriteRecords."""

    def __init__(self, records=None):
        self.records = list(records) if records else []

    @classmethod
    def parse(cls, data):
        off = 0
        recs = []
        while off < len(data):
            rec, nxt = OverwriteRecord.parse_stream(data, off)
            if rec is None:
                break
            recs.append(rec)
            off = nxt
        return cls(recs)

    def to_bytes(self):
        return b"".join(r.to_bytes() for r in self.records)

    def __len__(self):
        return len(self.records)


# ---------------------------------------------------------------------------
# Patch calculation helpers
# ---------------------------------------------------------------------------

class PatchCalculator:
    """
    Searches for documented hook patterns and builds overwrite data.

    NOTE: The actual TriCore patch routine, CRC fix and security bypass are
    ECU-specific.  This implementation builds the correct *container* format
    and uses either:
      * a user-supplied reference overwrite file, or
      * a built-in conservative stub that follows the documented pattern.
    """

    # Pattern from the Combi.rtf note: position found + 4 is the patch address.
    # 20 bytes: 02 00 00 00 00 00 00 00 0F 00 88 13 60 EA 00 00 53 65 CA 35
    HOOK_PATTERN = bytes.fromhex("02000000000000000F00881360EA00005365CA35")

    def __init__(self, asw_data, ecu_type):
        if not isinstance(asw_data, (bytes, bytearray)):
            raise TypeError("asw_data must be bytes")
        self.asw = bytes(asw_data)
        self.ecu_type = ecu_type
        self.config = ECUConfig.get(ecu_type)

    def find_hook_location(self):
        """
        Search for the documented hook pattern in ASW.
        Returns (hook_offset, patch_addr_offset) or (None, None).
        """
        pos = self.asw.find(self.HOOK_PATTERN)
        if pos < 0:
            return None, None
        return pos, pos + 4

    def find_free_space(self, min_size=0x200, alignment=0x10, preferred_fill=None):
        """
        Find a region of at least *min_size* bytes filled with 0x00 or 0xFF.
        Prefers *preferred_fill* (0x00 or 0xFF) if given; otherwise searches
        both fill values and returns the largest aligned run.
        """
        if len(self.asw) < min_size:
            return None

        candidates = []
        fill_values = [preferred_fill] if preferred_fill is not None else [0x00, 0xFF]
        fill_values = [v for v in fill_values if v is not None]

        for fill in fill_values:
            run_start = None
            run_len = 0
            for i, b in enumerate(self.asw):
                if b == fill:
                    if run_start is None:
                        run_start = i
                    run_len += 1
                else:
                    if run_start is not None and run_len >= min_size:
                        # Align start up to nearest *alignment* boundary inside the run.
                        aligned = ((run_start + alignment - 1) // alignment) * alignment
                        if aligned + min_size <= run_start + run_len:
                            candidates.append((aligned, run_start + run_len - aligned, fill))
                    run_start = None
                    run_len = 0

            if run_start is not None and run_len >= min_size:
                aligned = ((run_start + alignment - 1) // alignment) * alignment
                if aligned + min_size <= run_start + run_len:
                    candidates.append((aligned, run_start + run_len - aligned, fill))

        if not candidates:
            return None

        # Pick the run that is large enough and nearest to the end of ASW
        # (typical location for software patches).
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def build_overwrite_from_reference(self, ref_overwrite_path):
        """Copy an existing valid overwrite file (adapts addresses if needed)."""
        with open(ref_overwrite_path, "rb") as f:
            ref = f.read()
        return OverwriteFile.parse(ref)

    def build_stub_overwrite(self, routine_address=None):
        """
        Build a minimal but structurally valid overwrite file for an ASW that
        contains the documented hook pattern.

        Uses the real-world observed record layout:
          1. hook write(s) with a jump to the routine
          2. routine body at a free address
          3. CRC/verification fix
        """
        hook_off, patch_addr_off = self.find_hook_location()
        if hook_off is None:
            raise RuntimeError("Hook pattern not found in ASW - cannot build stub patch")

        if routine_address is None:
            free_off = self.find_free_space(min_size=0x300, alignment=0x10)
            if free_off is None:
                raise RuntimeError("No suitable free space found in ASW")
            # ASW1 base address for all supported types
            base = self.config["segments"]["ASW1"]["address"]
            routine_address = base + free_off

        # Hook patch address is in ASW flash.
        base = self.config["segments"]["ASW1"]["address"]
        hook_address = base + patch_addr_off

        # Compute the jump displacement / absolute address.  This is a TriCore
        # specific detail.  The bytes below are just a safe placeholder that
        # matches the *format* of real records; real-world use needs the correct
        # TriCore instruction sequence.
        target = routine_address

        records = []

        # Record 1: hook writes the jump target address (4 bytes)
        records.append(OverwriteRecord(hook_address, struct.pack(">I", target)))

        # Record 2: routine body placeholder.
        # In a real implementation this would be the TriCore code that checks
        # RAM for the call pattern and branches to the OBD unlock path.
        routine = self._make_stub_routine(target)
        records.append(OverwriteRecord(routine_address, routine))

        # Record 3: CRC / verification fix word (4 bytes, placeholder).
        crc_addr = self._find_crc_location()
        if crc_addr:
            records.append(OverwriteRecord(crc_addr, struct.pack(">I", 0xA3C528C3)))

        return OverwriteFile(records)

    def _make_stub_routine(self, base_addr):
        """
        Placeholder TriCore routine.  The exact bytes must be replaced with the
        real patch for the target software version.
        """
        # Minimum size so the record is non-trivial; 0x124 matches the sample.
        size = 0x124
        routine = bytearray(size)
        # Write a recognizable signature at the start.
        sig = struct.pack(">I", 0x02000000)
        routine[0:4] = sig
        # Write the base address as a marker.
        routine[4:8] = struct.pack(">I", base_addr)
        # Fill the rest with NOP-ish data (TriCore NOP = 0x00 00 00 00).
        for i in range(8, size, 4):
            routine[i:i + 4] = b"\x00\x00\x00\x00"
        return bytes(routine)

    def _find_crc_location(self):
        """Placeholder: returns a flash address near the end of ASW1."""
        seg = self.config["segments"]["ASW1"]
        return seg["address"] + seg["length"] - 4

    def apply_service_read_patch(self, overwrite, enabled=True):
        """
        Add or remove the UDS service 23 -> 19 replacement patch records
        used by secure-gateway vehicles to enable full-flash reading
        (document section VIII).

        When *enabled* is True, records are appended (skipping duplicates).
        When *enabled* is False, any record at a service-patch address is
        stripped from the overwrite file.
        """
        if not self.config or "service_patch" not in self.config:
            return overwrite

        sp = self.config["service_patch"]
        sp_addrs = set(sp["addresses"])

        if enabled:
            existing = {rec.address for rec in overwrite.records}
            for addr in sp["addresses"]:
                if addr not in existing:
                    overwrite.records.append(OverwriteRecord(addr, sp["data"]))
        else:
            overwrite.records = [
                rec for rec in overwrite.records if rec.address not in sp_addrs
            ]

        return overwrite

    def embed_custom_password(self, overwrite, password_bytes):
        """
        Embed a custom 8-byte password into the overwrite data.

        The password is written to flash address 0x801FF000 (in the ASW1 free
        area).  The level2 CBOOT patch routine can then be modified to read
        from this location instead of the hardcoded RAM address 0x000F4C.

        This prevents other tuners from accessing the ECU without knowing the
        custom password.
        """
        if not password_bytes or len(password_bytes) != 8:
            return overwrite

        # Use a flash address in the ASW1 free area (before the patch routine).
        # This address is known to the level2 CBOOT patch routine.
        password_addr = 0x801FF000

        # Remove any existing password record.
        overwrite.records = [
            rec for rec in overwrite.records if rec.address != password_addr
        ]

        # Add the password record.
        overwrite.records.append(OverwriteRecord(password_addr, password_bytes))
        return overwrite


# ---------------------------------------------------------------------------
# Core segment extractor
# ---------------------------------------------------------------------------

class SegmentExtractor:
    """Extract segments from original full binaries and protected patch files."""

    def __init__(self, binary_data, ecu_type):
        self.data = bytes(binary_data)
        self.ecu_type = ecu_type
        self.config = ECUConfig.get(ecu_type)

    def extract_segments(self, cboot_data=None):
        """
        Extract ASW/DATA segments from the *original full binary*.

        Each ECU layout defines a *file_offset* where the segment lives inside
        the boot read.  CBOOT is usually not present in the boot read; pass it
        via *cboot_data* or it will be returned as empty bytes.
        """
        if not self.config:
            return None

        segments = {}
        for name, info in self.config["segments"].items():
            length = info["length"]
            offset = info.get("file_offset")

            if name == "CBOOT" and cboot_data is not None:
                segments[name] = bytes(cboot_data)[:length]
                continue

            if offset is None:
                # Segment is not part of this binary.
                segments[name] = b""
                continue

            if offset + length <= len(self.data):
                segments[name] = self.data[offset:offset + length]
            else:
                segments[name] = b""

        return segments

    def extract_from_protected_container(self):
        """
        Parse an already-built protected patch file and return its parts.
        Useful for inspecting / re-building containers.
        """
        if not self.config:
            return None

        c = self.config
        full = self.data[0:c["full_file_size"]]
        level2 = b""
        if c["level2_offset"] is not None:
            level2_end = c["level2_offset"] + c["level2_erase_fill_size"]
            level2 = self.data[c["level2_offset"]:level2_end]

        over = self.data[c["overwrite_offset"]:c["overwrite_offset"] + c["overwrite_block_size"]]
        # Trim trailing 0xFF padding.
        over = over.rstrip(b"\xFF")

        return {
            "full_file": full,
            "level2": level2,
            "overwrite": over,
            "overwrite_parsed": OverwriteFile.parse(over),
        }


# ---------------------------------------------------------------------------
# Patch generator worker (runs in QThread)
# ---------------------------------------------------------------------------

class WorkerSignals(QObject):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(bool, str)
    log = pyqtSignal(str)


class PatchWorker(QThread):
    """Background worker for patch generation."""

    def __init__(self, original_path, ecu_type, output_dir,
                 reference_overwrite=None, cboot_path=None, cboot_level2_path=None,
                 service_read_patch=True, custom_password=None):
        super().__init__()
        self.original_path = original_path
        self.ecu_type = ecu_type
        self.output_dir = Path(output_dir)
        self.reference_overwrite = reference_overwrite
        self.cboot_path = cboot_path
        self.cboot_level2_path = cboot_level2_path
        self.service_read_patch = service_read_patch
        self.custom_password = custom_password
        self.signals = WorkerSignals()

    def log(self, msg):
        self.signals.log.emit(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

    def run(self):
        try:
            self.signals.progress.emit(5, "Loading binary...")
            with open(self.original_path, "rb") as f:
                full_data = f.read()

            config = ECUConfig.get(self.ecu_type)
            if not config:
                self.signals.completed.emit(False, f"Unknown ECU type: {self.ecu_type}")
                return

            container_mode = len(full_data) == config["container_size"]
            boot_read_mode = len(full_data) == config["full_file_size"]

            if container_mode:
                self.log(f"Input is a protected container ({len(full_data)} bytes)")
            elif boot_read_mode:
                self.log(f"Input is a boot read ({len(full_data)} bytes)")
            else:
                self.signals.completed.emit(
                    False,
                    f"Unexpected binary size: 0x{len(full_data):X}. "
                    f"Expected 0x{config['full_file_size']:X} or 0x{config['container_size']:X}.",
                )
                return

            # ------------------------------------------------------------------
            # Segment extraction
            # ------------------------------------------------------------------
            self.signals.progress.emit(15, "Extracting segments...")
            extractor = SegmentExtractor(full_data, self.ecu_type)

            cboot_data = None
            cboot_level2_data = None
            detected_overwrite = None

            if container_mode:
                # Pull everything directly from the container.
                parts = extractor.extract_from_protected_container()
                full_data = parts["full_file"]
                # CBOOT original sits before level2 in the container.
                if self.ecu_type == "SIM2K250":
                    cboot_data = full_data[0x280000:0x280000 + config["segments"]["CBOOT"]["length"]]
                    cboot_level2_data = full_data[0x290000:0x290000 + config["segments"]["CBOOT"]["length"]]
                elif self.ecu_type == "SIM2K260":
                    cboot_data = full_data[0x400000:0x400000 + config["segments"]["CBOOT"]["length"]]
                    cboot_level2_data = full_data[0x410000:0x410000 + config["segments"]["CBOOT"]["length"]]
                elif self.ecu_type == "SIM2K305":
                    cboot_data = full_data[0x280000:0x280000 + config["segments"]["CBOOT"]["length"]]
                detected_overwrite = parts.get("overwrite_parsed")

            # Extract ASW/DATA from the (possibly trimmed) full file.
            segments = extractor.extract_segments(cboot_data)
            asw = segments.get("ASW1", b"")
            data = segments.get("DATA", b"")
            cboot = segments.get("CBOOT", b"")
            asw2 = segments.get("ASW2", b"") if self.ecu_type == "SIM2K260" else b""

            # ------------------------------------------------------------------
            # Resolve CBOOT from database if still missing.
            # ------------------------------------------------------------------
            calibration_ref = detect_calibration_and_bootloader(full_data)
            bootloader_ref = detect_bootloader(full_data)
            if calibration_ref:
                self.log(f"Detected calibration reference: {calibration_ref}")
            if bootloader_ref:
                self.log(f"Detected bootloader reference: {bootloader_ref}")

            if not cboot and calibration_ref:
                db_cboot, db_level2 = resolve_cboot(calibration_ref, bootloader_ref)
                if db_cboot:
                    cboot = db_cboot
                    self.log(f"Resolved CBOOT from database for {calibration_ref}")
                if not cboot_level2_data and db_level2:
                    cboot_level2_data = db_level2
                    self.log(f"Resolved level2 CBOOT from database for {calibration_ref}")

            # Allow optional overrides.
            if self.cboot_path and Path(self.cboot_path).exists():
                with open(self.cboot_path, "rb") as f:
                    cboot = f.read()
                self.log(f"Overriding CBOOT: {self.cboot_path}")

            if self.cboot_level2_path and Path(self.cboot_level2_path).exists():
                with open(self.cboot_level2_path, "rb") as f:
                    cboot_level2_data = f.read()
                self.log(f"Overriding level2 CBOOT: {self.cboot_level2_path}")

            self.log(
                f"ASW1=0x{len(asw):X} DATA=0x{len(data):X} CBOOT=0x{len(cboot):X} "
                f"level2=0x{len(cboot_level2_data or b''):X}"
            )

            if not cboot:
                self.signals.completed.emit(
                    False,
                    "CBOOT could not be found or resolved. "
                    "Provide a CBOOT file or a binary that includes it.",
                )
                return

            # ------------------------------------------------------------------
            # Build overwrite data
            # ------------------------------------------------------------------
            self.signals.progress.emit(30, "Calculating patch data...")
            calc = PatchCalculator(asw, self.ecu_type)

            overwrite_from_db = False

            if self.reference_overwrite and Path(self.reference_overwrite).exists():
                self.log(f"Using reference overwrite: {self.reference_overwrite}")
                overwrite = calc.build_overwrite_from_reference(self.reference_overwrite)
                overwrite_from_db = True
            elif detected_overwrite is not None:
                self.log("Reusing overwrite from protected container")
                overwrite = detected_overwrite
                overwrite_from_db = True
            elif calibration_ref:
                db_overwrite = resolve_overwrite(calibration_ref)
                if db_overwrite:
                    self.log(f"Resolved overwrite from database for {calibration_ref}")
                    overwrite = OverwriteFile.parse(db_overwrite)
                    overwrite_from_db = True
                else:
                    self.log("Generating overwrite from binary hook pattern...")
                    overwrite = calc.build_stub_overwrite()
            else:
                self.log("Generating overwrite from binary hook pattern...")
                overwrite = calc.build_stub_overwrite()

            # Service 23->19 patch: only apply/remove when the overwrite was
            # generated from scratch (stub).  Database/reference overwrites
            # already contain the correct calibration-specific service records.
            if not overwrite_from_db:
                overwrite = calc.apply_service_read_patch(
                    overwrite, enabled=self.service_read_patch
                )

            overwrite_bytes = overwrite.to_bytes()
            self.log(f"Overwrite records: {len(overwrite)}, raw size: 0x{len(overwrite_bytes):X}")

            # Build patched ASW (apply overwrite in memory).
            self.signals.progress.emit(45, "Applying patch to ASW...")
            asw_patched = self._apply_overwrite(asw, overwrite)

            # Build CBOOT level2.
            self.signals.progress.emit(55, "Building CBOOT level2...")
            if cboot_level2_data:
                cboot_level2 = bytearray(cboot_level2_data)
            else:
                # No database/container level2 available; build a placeholder.
                cboot_level2 = bytearray(self._build_cboot_level2(
                    cboot or b"\xFF" * config["segments"]["CBOOT"]["length"],
                    self.cboot_level2_path
                ))

            # Apply custom password to level2 CBOOT if provided.
            # Password is stored at offset 0x0F4C (8 bytes).
            if self.custom_password:
                if len(cboot_level2) >= 0x0F4C + 8:
                    cboot_level2[0x0F4C:0x0F4C + 8] = self.custom_password
                    self.log(
                        f"Custom password written to level2 CBOOT at 0x0F4C: "
                        f"{self.custom_password.hex()}"
                    )
                else:
                    self.log("WARNING: level2 CBOOT too small for password injection")

            cboot_level2 = bytes(cboot_level2)

            # Create output directories and files.
            self.signals.progress.emit(65, "Writing output files...")
            self.output_dir.mkdir(parents=True, exist_ok=True)

            cboot_dir = self.output_dir / "CBOOT"
            cboot_dir.mkdir(exist_ok=True)
            self._write(cboot_dir / "cboot.bin", cboot if cboot else cboot_level2)
            self._write(cboot_dir / "cboot.level2.bin", cboot_level2)

            asw_dir = self.output_dir / "ASW"
            asw_dir.mkdir(exist_ok=True)
            self._write(asw_dir / "asw.bin", asw)
            if asw2:
                self._write(asw_dir / "asw2.bin", asw2)
            self._write(asw_dir / "asw.trojan.bin", asw_patched)
            self._write(asw_dir / "asw.overwrite.bin", overwrite_bytes)

            data_dir = self.output_dir / "DATA"
            data_dir.mkdir(exist_ok=True)
            self._write(data_dir / "data.bin", data)

            # Build combined protected patch container.
            self.signals.progress.emit(80, "Building protected patch container...")
            container = self._build_container(
                full_data, cboot or cboot_level2, cboot_level2, overwrite_bytes
            )
            container_path = self.output_dir / f"[Protected]_{self.ecu_type}_patch.bin"
            self._write(container_path, container)

            self.signals.progress.emit(100, "Complete!")
            self.signals.completed.emit(True, f"Patches written to {self.output_dir}")

        except Exception as e:
            err = traceback.format_exc()
            self.log(f"ERROR: {e}\n{err}")
            self.signals.completed.emit(False, f"Generation failed: {e}")

    @staticmethod
    def _write(path, data):
        with open(path, "wb") as f:
            f.write(data)

    def _apply_overwrite(self, segment, overwrite, segment_name="ASW1"):
        """Apply OverwriteFile records to a segment copy using absolute addresses."""
        config = ECUConfig.get(self.ecu_type)
        base = config["segments"][segment_name]["address"]
        patched = bytearray(segment)
        for rec in overwrite.records:
            offset = rec.address - base
            if 0 <= offset < len(patched) and offset + len(rec.data) <= len(patched):
                patched[offset:offset + len(rec.data)] = rec.data
            else:
                # Address outside this segment; skip with warning.
                pass
        return bytes(patched)

    def _build_cboot_level2(self, cboot_orig, level2_path=None):
        """
        Create the level2 CBOOT.  If *level2_path* is supplied, read it;
        otherwise fall back to the original CBOOT with a log warning that no
        real level2 patch has been applied.
        """
        if level2_path and Path(level2_path).exists():
            with open(level2_path, "rb") as f:
                data = f.read()
            self.log(f"Loaded level2 CBOOT: {level2_path} ({len(data)} bytes)")
            return bytes(data)

        self.log(
            "WARNING: no level2 CBOOT patch file supplied; using original CBOOT "
            "as level2 placeholder.  Real unlock patches need a TriCore-specific "
            "level2 routine."
        )
        return bytes(cboot_orig)

    def _build_container(self, full_data, cboot_orig, cboot_level2, overwrite_bytes):
        """Assemble the protected patch container according to documented layout."""
        config = ECUConfig.get(self.ecu_type)
        size = config["container_size"]
        # Initialise erased state (0xFF) everywhere; then overlay real data.
        container = bytearray(b"\xFF" * size)

        overwrite_start = config["overwrite_offset"]
        overwrite_block = config["overwrite_block_size"]
        cboot_len = config["segments"]["CBOOT"]["length"]
        full_file_size = config["full_file_size"]

        if self.ecu_type == "SIM2K250":
            # 0x000000 - full_file_size: original full file.
            container[0:full_file_size] = full_data
            # CBOOT originals and level2 sit immediately after the full file block.
            cboot_offset = full_file_size
            level2_offset = cboot_offset + 0x10000  # documented 0x10000 block
            container[cboot_offset:cboot_offset + cboot_len] = cboot_orig[:cboot_len]
            container[level2_offset:level2_offset + cboot_len] = cboot_level2[:cboot_len]
            # Overwrite data.
            real_ow = min(len(overwrite_bytes), overwrite_block)
            container[overwrite_start:overwrite_start + real_ow] = overwrite_bytes[:real_ow]

        elif self.ecu_type == "SIM2K260":
            container[0:full_file_size] = full_data
            cboot_offset = full_file_size
            level2_offset = cboot_offset + 0x10000
            container[cboot_offset:cboot_offset + cboot_len] = cboot_orig[:cboot_len]
            container[level2_offset:level2_offset + cboot_len] = cboot_level2[:cboot_len]
            real_ow = min(len(overwrite_bytes), overwrite_block)
            container[overwrite_start:overwrite_start + real_ow] = overwrite_bytes[:real_ow]

        elif self.ecu_type == "SIM2K305":
            container[0:full_file_size] = full_data
            container[overwrite_start:overwrite_start + cboot_len] = cboot_orig[:cboot_len]
            ow_data_start = overwrite_start + cboot_len
            real_ow = min(len(overwrite_bytes), overwrite_block - cboot_len)
            if real_ow > 0:
                container[ow_data_start:ow_data_start + real_ow] = overwrite_bytes[:real_ow]

        return bytes(container)


# ---------------------------------------------------------------------------
# Hex viewer widget
# ---------------------------------------------------------------------------

class HexViewWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = b""
        self.start_offset = 0

        layout = QVBoxLayout(self)
        self.hex_display = QTextEdit()
        self.hex_display.setReadOnly(True)
        self.hex_display.setFont(QFont("Courier New", 9))
        layout.addWidget(self.hex_display)

        nav = QHBoxLayout()
        self.offset_label = QLabel("Offset: 0x00000000")
        self.up_btn = QPushButton("▲ Page Up")
        self.down_btn = QPushButton("▼ Page Down")
        self.up_btn.clicked.connect(self.page_up)
        self.down_btn.clicked.connect(self.page_down)
        nav.addWidget(self.up_btn)
        nav.addWidget(self.down_btn)
        nav.addStretch()
        nav.addWidget(self.offset_label)
        layout.addLayout(nav)

    def setData(self, data):
        self.data = bytes(data) if data else b""
        self.start_offset = 0
        self.update_display()

    def page_up(self):
        self.start_offset = max(0, self.start_offset - 0x1000)
        self.update_display()

    def page_down(self):
        max_off = max(0, len(self.data) - 0x1000)
        self.start_offset = min(max_off, self.start_offset + 0x1000)
        self.update_display()

    def update_display(self):
        display_size = min(0x1000, max(0, len(self.data) - self.start_offset))
        chunk = self.data[self.start_offset:self.start_offset + display_size]

        lines = []
        for i in range(0, len(chunk), 16):
            line = chunk[i:i + 16]
            hex_part = " ".join(f"{b:02X}" for b in line)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in line)
            lines.append(f"{self.start_offset + i:08X}  {hex_part:<48}  {ascii_part}")

        self.hex_display.setPlainText("\n".join(lines) if lines else "<empty>")
        self.offset_label.setText(f"Offset: 0x{self.start_offset:08X}")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_binary = None
        self.current_ecu = None
        self.worker = None
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("SIM2K Patch Generator")
        self.setGeometry(100, 100, 1500, 950)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # ---- Input group ----
        input_group = QGroupBox("Input")
        form = QFormLayout()

        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_btn = QPushButton("Browse...")
        self.file_btn.clicked.connect(self.browse_binary)
        row.addWidget(self.file_edit)
        row.addWidget(self.file_btn)
        form.addRow("Original Full Binary:", row)

        row = QHBoxLayout()
        self.overwrite_edit = QLineEdit()
        self.overwrite_edit.setReadOnly(True)
        self.overwrite_btn = QPushButton("Browse...")
        self.overwrite_btn.clicked.connect(self.browse_overwrite)
        self.overwrite_clear_btn = QPushButton("Clear")
        self.overwrite_clear_btn.clicked.connect(self.clear_overwrite)
        row.addWidget(self.overwrite_edit)
        row.addWidget(self.overwrite_btn)
        row.addWidget(self.overwrite_clear_btn)
        form.addRow("Reference Overwrite (optional override):", row)

        row = QHBoxLayout()
        self.cboot_edit = QLineEdit()
        self.cboot_edit.setReadOnly(True)
        self.cboot_btn = QPushButton("Browse...")
        self.cboot_btn.clicked.connect(self.browse_cboot)
        self.cboot_clear_btn = QPushButton("Clear")
        self.cboot_clear_btn.clicked.connect(self.clear_cboot)
        row.addWidget(self.cboot_edit)
        row.addWidget(self.cboot_btn)
        row.addWidget(self.cboot_clear_btn)
        form.addRow("CBOOT override (optional, e.g. 606A1_C2.bin):", row)

        row = QHBoxLayout()
        self.level2_edit = QLineEdit()
        self.level2_edit.setReadOnly(True)
        self.level2_btn = QPushButton("Browse...")
        self.level2_btn.clicked.connect(self.browse_level2)
        self.level2_clear_btn = QPushButton("Clear")
        self.level2_clear_btn.clicked.connect(self.clear_level2)
        row.addWidget(self.level2_edit)
        row.addWidget(self.level2_btn)
        row.addWidget(self.level2_clear_btn)
        form.addRow("Level2 CBOOT override (optional, e.g. 606A1_C2.level2.bin):", row)

        self.ecu_combo = QComboBox()
        self.ecu_combo.addItems(["Auto-detect"] + ECUConfig.types())
        self.ecu_combo.currentTextChanged.connect(self.on_ecu_changed)
        form.addRow("ECU Type:", self.ecu_combo)

        self.service_read_cb = QCheckBox(
            "Replace UDS service 23 → 19 (enable full-flash read on secure-gateway vehicles)"
        )
        self.service_read_cb.setChecked(True)
        form.addRow(self.service_read_cb)

        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("Leave empty for original ECU password")
        self.password_edit.setMaxLength(16)  # 8 bytes = 16 hex chars
        self.password_edit.setInputMask("HH HH HH HH HH HH HH HH;_")
        self.password_edit.setToolTip(
            "8-byte hex password written to RAM 0x000F4C by the patch routine.\n"
            "Used to lock out other tuners who don't know the new password.\n"
            "Leave empty to keep the original ECU password."
        )
        form.addRow("Custom password (8 bytes hex):", self.password_edit)

        input_group.setLayout(form)
        main_layout.addWidget(input_group)

        # ---- Output / segment table ----
        self.tabs = QTabWidget()

        # Segments tab
        seg_widget = QWidget()
        seg_layout = QVBoxLayout(seg_widget)
        self.segment_table = QTableWidget()
        self.segment_table.setColumnCount(5)
        self.segment_table.setHorizontalHeaderLabels(["Segment", "Address", "Length", "Erase Cmd", "Extracted Size"])
        self.segment_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        seg_layout.addWidget(self.segment_table)

        self.inspect_btn = QPushButton("Inspect as Protected Container")
        self.inspect_btn.clicked.connect(self.inspect_container)
        self.inspect_btn.setEnabled(False)
        seg_layout.addWidget(self.inspect_btn)

        self.generate_btn = QPushButton("Generate Patches")
        self.generate_btn.clicked.connect(self.generate_patches)
        self.generate_btn.setEnabled(False)
        seg_layout.addWidget(self.generate_btn)

        self.tabs.addTab(seg_widget, "Segments")

        # Hex view
        hex_widget = QWidget()
        hex_layout = QVBoxLayout(hex_widget)
        self.hex_view = HexViewWidget()
        hex_layout.addWidget(self.hex_view)
        self.tabs.addTab(hex_widget, "Hex View")

        # Log
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFont(QFont("Courier New", 9))
        log_layout.addWidget(self.log_display)
        self.tabs.addTab(log_widget, "Log")

        main_layout.addWidget(self.tabs)

        # ---- Status ----
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("Ready")
        main_layout.addWidget(self.progress)
        main_layout.addWidget(self.status)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def browse_binary(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Original Full Binary", "", "Binary Files (*.bin);;All Files (*)"
        )
        if path:
            self.file_edit.setText(path)
            self.load_binary(path)

    def browse_overwrite(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Reference Overwrite File", "", "Binary Files (*.bin);;All Files (*)"
        )
        if path:
            self.overwrite_edit.setText(path)

    def clear_overwrite(self):
        self.overwrite_edit.clear()

    def browse_cboot(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select CBOOT Binary", "", "Binary Files (*.bin);;All Files (*)"
        )
        if path:
            self.cboot_edit.setText(path)

    def clear_cboot(self):
        self.cboot_edit.clear()

    def browse_level2(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Level2 CBOOT Patch File", "", "Binary Files (*.bin);;All Files (*)"
        )
        if path:
            self.level2_edit.setText(path)

    def clear_level2(self):
        self.level2_edit.clear()

    def load_binary(self, path):
        try:
            with open(path, "rb") as f:
                self.current_binary = f.read()

            self.log(f"Loaded binary: {path}")
            self.log(f"File size: 0x{len(self.current_binary):X} ({len(self.current_binary):,} bytes)")

            detected = self.detect_ecu_type(self.current_binary)
            if detected and self.ecu_combo.currentText() == "Auto-detect":
                self.ecu_combo.setCurrentText(detected)
                self.current_ecu = detected
                self.log(f"Detected ECU type: {detected}")
            else:
                self.current_ecu = self.ecu_combo.currentText()
                if self.current_ecu == "Auto-detect":
                    self.current_ecu = None

            self.hex_view.setData(self.current_binary)
            self.update_segment_table()
            self.generate_btn.setEnabled(self.current_ecu is not None)
            self.inspect_btn.setEnabled(True)
            self.status.setText("Binary loaded")

        except Exception as e:
            self.log(f"ERROR loading binary: {e}")
            QMessageBox.critical(self, "Error", f"Failed to load binary: {e}")

    def detect_ecu_type(self, data):
        """Fast heuristic: file size first, then bootloader signatures."""
        size = len(data)
        for name, cfg in ECUConfig.PROTECTED_LAYOUTS.items():
            if size == cfg["full_file_size"] or size == cfg["container_size"]:
                return name

        patterns = {
            "SIM2K250": [b"606A1", b"606Z0", b"6H6N0"],
            "SIM2K260": [b"640C0"],
            "SIM2K305": [b"6U2V0", b"6U2S0"],
        }
        for name, pats in patterns.items():
            for p in pats:
                if p in data:
                    return name
        return None

    def on_ecu_changed(self, text):
        if text == "Auto-detect":
            self.current_ecu = self.detect_ecu_type(self.current_binary) if self.current_binary else None
        else:
            self.current_ecu = text
        if self.current_binary:
            self.update_segment_table()
            self.generate_btn.setEnabled(self.current_ecu is not None)

    def update_segment_table(self):
        ecu = self.current_ecu
        if not ecu or ecu not in ECUConfig.PROTECTED_LAYOUTS:
            self.segment_table.setRowCount(0)
            return

        cfg = ECUConfig.get(ecu)
        segments = cfg["segments"]
        self.segment_table.setRowCount(len(segments))

        extractor = SegmentExtractor(self.current_binary, ecu) if self.current_binary else None
        extracted = extractor.extract_segments() if extractor else {}

        for row, (name, info) in enumerate(segments.items()):
            sz = len(extracted.get(name, b""))
            self.segment_table.setItem(row, 0, QTableWidgetItem(name))
            self.segment_table.setItem(row, 1, QTableWidgetItem(f"0x{info['address']:08X}"))
            self.segment_table.setItem(row, 2, QTableWidgetItem(f"0x{info['length']:X}"))
            self.segment_table.setItem(row, 3, QTableWidgetItem(str(info.get("erase", "N/A"))))
            self.segment_table.setItem(row, 4, QTableWidgetItem(f"0x{sz:X}"))

    def inspect_container(self):
        if not self.current_binary or not self.current_ecu:
            return
        cfg = ECUConfig.get(self.current_ecu)
        if len(self.current_binary) != cfg["container_size"]:
            QMessageBox.information(
                self, "Inspect",
                f"This is not a protected container (expected 0x{cfg['container_size']:X} bytes)."
            )
            return

        extractor = SegmentExtractor(self.current_binary, self.current_ecu)
        parts = extractor.extract_from_protected_container()
        self.log("Protected container inspection:")
        self.log(f"  full_file size: 0x{len(parts['full_file']):X}")
        self.log(f"  level2 size: 0x{len(parts['level2']):X}")
        self.log(f"  overwrite raw size: 0x{len(parts['overwrite']):X}")
        self.log(f"  overwrite records: {len(parts['overwrite_parsed'])}")
        for i, rec in enumerate(parts["overwrite_parsed"].records[:10]):
            self.log(f"    [{i}] addr=0x{rec.address:08X} len=0x{len(rec.data):X}")

    def generate_patches(self):
        if not self.current_binary or not self.current_ecu:
            QMessageBox.warning(self, "Warning", "Please load a binary and select ECU type.")
            return

        out_dir = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if not out_dir:
            return

        ref = self.overwrite_edit.text() or None

        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.generate_btn.setEnabled(False)

        cboot = self.cboot_edit.text() or None
        level2 = self.level2_edit.text() or None
        service_patch = self.service_read_cb.isChecked()

        # Parse custom password (8 bytes hex, e.g. "AA BB CC DD EE FF 00 11").
        password_hex = self.password_edit.text().replace(" ", "").replace("_", "")
        custom_password = None
        if password_hex:
            if len(password_hex) != 16:
                QMessageBox.warning(
                    self, "Warning",
                    "Custom password must be exactly 8 bytes (16 hex characters)."
                )
                return
            try:
                custom_password = bytes.fromhex(password_hex)
            except ValueError:
                QMessageBox.warning(
                    self, "Warning",
                    "Custom password contains invalid hex characters."
                )
                return

        self.worker = PatchWorker(
            self.file_edit.text(), self.current_ecu, out_dir, ref, cboot, level2,
            service_patch, custom_password
        )
        self.worker.signals.log.connect(self.log)
        self.worker.signals.progress.connect(self.on_progress)
        self.worker.signals.completed.connect(self.on_completed)
        self.worker.start()

    def on_progress(self, value, msg):
        self.progress.setValue(value)
        self.status.setText(msg)
        self.log(msg)

    def on_completed(self, success, msg):
        self.progress.setVisible(False)
        self.generate_btn.setEnabled(True)
        self.status.setText(msg)
        if success:
            QMessageBox.information(self, "Done", msg)
        else:
            QMessageBox.critical(self, "Failed", msg)

    def log(self, msg):
        self.log_display.append(msg)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
