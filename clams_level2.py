#!/usr/bin/env python3
"""
clams_level2.py — Algorithmic Level2 CBOOT generator for SIM2K ECUs.

Reimplements the level2 bootloader patch logic originally found in the
Hyundai SIM2K ToolBox (.NET), so that level2 CBOOT files can be generated
from a plain original CBOOT without requiring pre-built samples.

Patch algorithm (per CPU family):
  1. Locate the DevMode hook pattern and enable developer mode (byte 0x08).
  2. Locate the TesterPresent pattern and boost the S3 timer (byte 0x0B).
  3. (optional) Extend UDS service 0x23 read range (byte 0x0F).
  4. (optional) Replace UDS service 0x23 with 0x19 for full-flash read (byte 0x19).
  5. Recompute the two Siemens CRC32 structures (at file offsets 0x300 and 0x340)
     over their protected ranges and write the new values back.

The CRC32 variant used here is the Siemens (TriCore) checksum:
  polynomial 0x04C11DB7, init 0, xorout 0, with per-byte bit reflection and
  final output bit reflection.  This is functionally CRC-32/ISO-HDLC with the
  init and xorout masking removed.
"""

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Siemens CRC32
# ---------------------------------------------------------------------------

_POLY = 0x04C11DB7
_REFL_POLY = 0xEDB88320  # bit-reversed 0x04C11DB7

# Pre-computed reflected lookup table (256 entries).
_TABLE = [0] * 256
for _i in range(256):
    _v = _i
    for _ in range(8):
        _v = (_v >> 1) ^ _REFL_POLY if (_v & 1) else (_v >> 1)
    _TABLE[_i] = _v


def _reflect_u8(b: int) -> int:
    b = ((b & 0xF0) >> 4) | ((b & 0x0F) << 4)
    b = ((b & 0xCC) >> 2) | ((b & 0x33) << 2)
    b = ((b & 0xAA) >> 1) | ((b & 0x55) << 1)
    return b & 0xFF


def _reflect_u32(v: int) -> int:
    v = ((v & 0xFFFF0000) >> 16) | ((v & 0x0000FFFF) << 16)
    v = ((v & 0xFF00FF00) >> 8) | ((v & 0x00FF00FF) << 8)
    v = ((v & 0xF0F0F0F0) >> 4) | ((v & 0x0F0F0F0F) << 4)
    v = ((v & 0xCCCCCCCC) >> 2) | ((v & 0x33333333) << 2)
    v = ((v & 0xAAAAAAAA) >> 1) | ((v & 0x55555555) << 1)
    return v & 0xFFFFFFFF


def siemens_crc32(data: bytes, init: int = 0) -> int:
    """Compute the Siemens CRC32 over *data*.

    Mirrors the toolbox AdvancedCrc32.Compute with poly=0x04C11DB7,
    init=0, xorout=0, ReflectionIn=False, ReflectionOut=False.  When those
    reflection flags are False the routine pre-reflects each input byte and
    post-reflects the result, which is mathematically equivalent to the
    standard CRC-32/ISO-HDLC with init=0 and xorout=0.
    """
    crc = _reflect_u32(init)
    for raw in data:
        b = _reflect_u8(raw)
        crc = (crc >> 8) ^ _TABLE[(crc ^ b) & 0xFF]
    return _reflect_u32(crc) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Pattern finder (nibble-granular wildcards)
# ---------------------------------------------------------------------------

def _pattern_to_bytes(hex_str: str) -> Tuple[bytes, bytes]:
    """Convert a hex pattern string with '?' wildcards to (pattern, mask).

    Each character represents one nibble.  A hex digit sets the corresponding
    nibble in both pattern and mask (mask nibble = 0xF); a '?' wildcards the
    nibble (mask nibble = 0x0).  Characters are paired LSB-first to form bytes,
    so "AB" -> pattern 0xAB, mask 0xFF and "A?" -> pattern 0xA0, mask 0xF0.
    """
    if len(hex_str) % 2 != 0:
        raise ValueError(f"pattern length must be even, got {len(hex_str)}: {hex_str!r}")
    pat_chars: List[str] = []
    mask_chars: List[str] = []
    for c in hex_str:
        if c == '?':
            pat_chars.append('0')
            mask_chars.append('0')
        else:
            pat_chars.append(c)
            mask_chars.append('F')
    pattern = bytes(int(''.join(pat_chars[i:i + 2]), 16) for i in range(0, len(pat_chars), 2))
    mask = bytes(int(''.join(mask_chars[i:i + 2]), 16) for i in range(0, len(mask_chars), 2))
    return pattern, mask


def locate_pattern(data: bytes, hex_str: str, start: int = 0,
                   end: Optional[int] = None) -> List[int]:
    """Return all offsets in *data* where *hex_str* matches.

    *hex_str* uses '?' for nibble wildcards (e.g. "FF??80" matches any byte
    pair 0xFF, 0xYZ, 0x80).  Returns an empty list if no match.
    """
    pattern, mask = _pattern_to_bytes(hex_str)
    if not pattern:
        return []
    limit = (end if end is not None else len(data)) - len(pattern)
    out = []
    pos = start
    while pos <= limit:
        ok = True
        for j in range(len(pattern)):
            if (data[pos + j] & mask[j]) != (pattern[j] & mask[j]):
                ok = False
                break
        if ok:
            out.append(pos)
        pos += 1
    return out


# ---------------------------------------------------------------------------
# Siemens CRC structure
# ---------------------------------------------------------------------------

@dataclass
class ChecksumRange:
    start: int
    stop: int

    @property
    def length(self) -> int:
        return self.stop - self.start + 1


@dataclass
class SiemensCrcStructure:
    """Layout of the Siemens CRC structure stored inside the CBOOT.

    The structure is read as little-endian uint32 values:
        +0  CrcInitValue
        +4  CrcActualValue
        +8  RangeCount
        +12 ranges[]  (each: uint32 Start, uint32 Stop)
    """
    offset: int = 0
    init_value: int = 0
    actual_value: int = 0
    ranges: List[ChecksumRange] = field(default_factory=list)

    @classmethod
    def parse(cls, data: bytes, offset: int) -> "SiemensCrcStructure":
        s = cls(offset=offset)
        s.init_value = struct.unpack_from('<I', data, offset)[0]
        s.actual_value = struct.unpack_from('<I', data, offset + 4)[0]
        count = struct.unpack_from('<I', data, offset + 8)[0]
        p = offset + 12
        for _ in range(count):
            if p + 8 > len(data):
                break
            start = struct.unpack_from('<I', data, p)[0]
            stop = struct.unpack_from('<I', data, p + 4)[0]
            s.ranges.append(ChecksumRange(start, stop))
            p += 8
        return s

    def write_actual(self, data: bytearray, value: int) -> None:
        """Write *value* into the CrcActualValue slot (little-endian)."""
        struct.pack_into('<I', data, self.offset + 4, value & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# Patch definitions
# ---------------------------------------------------------------------------

# Patch byte applied at (pattern_position + offset).
_PATCHES_TC1782 = [
    # (name, pattern, offset_from_match, new_byte, required)
    ('DevMode',      "BB500520910000F8022FD9FF000182003C05C21004F1ABA2AA21",
     0x0C, 0x08, True),
    ('TesterPresent',
     "404F40F46DFF????B012402C40F46DFF????DF220C8014CFEE07DA00822440F46DFF"
     "????3C04DA123C02DA1302F440F46DFF????82020090",
     0x2B, 0x0B, True),
    ('Mode23Range',  "00000000FFFFFFFF00010000??????8001000000",
     0x08, 0x0F, False),
    ('Mode23Replace', "??????8023020000??????80",
     0x04, 0x19, False),
]

_PATCHES_TC1791 = [
    ('DevMode',      "BB500520910000F8022FD9FF000182003C0504F1C210ABA2AA217E23BF40FBFF0090",
     0x0C, 0x08, True),
    ('TesterPresent',
     "404F40F46DFFB2F4B012402C40F46DFF9CF4DF220C8014CFEE0?????????????6DFF"
     "8FF43C04DA123C02DA1302F440F46DFF90F482020090",
     0x2B, 0x0B, True),
    ('Mode23Range',  "00000000FFFFFFFF00010000??????8001000000",
     0x08, 0x0F, False),
    ('Mode23Replace', "??????8023020000??????80",
     0x04, 0x19, False),
]

# Offsets of the two CRC structures inside the CBOOT file.
_CRC1_OFFSET = 0x300
_CRC2_OFFSET = 0x340


class Level2Generator:
    """Generate a level2 (patched) CBOOT from an original CBOOT.

    Parameters
    ----------
    cboot : original CBOOT bytes
    bootloader_physical_address : physical flash address of the CBOOT
        (used to translate CRC range addresses into file offsets)
    cpu : "TC1782" or "TC1791"
    enable_mode23 : extend the UDS service 0x23 read range
    replace_mode23 : replace UDS service 0x23 with 0x19 (full-flash read)
    log : optional callable(str) for diagnostic output
    """

    def __init__(self, cboot: bytes, bootloader_physical_address: int,
                 cpu: str = "TC1782", enable_mode23: bool = True,
                 replace_mode23: bool = True, log=None):
        self.cboot = bytes(cboot)
        self.phys = bootloader_physical_address & 0xFFFFFFFF
        self.cpu = cpu
        self.enable_mode23 = enable_mode23
        self.replace_mode23 = replace_mode23
        self._log = log or (lambda *_: None)

        if cpu == "TC1782":
            self._patches = _PATCHES_TC1782
        elif cpu == "TC1791":
            self._patches = _PATCHES_TC1791
        else:
            raise ValueError(f"unsupported CPU: {cpu!r} (expected TC1782 or TC1791)")

    # -- public API --------------------------------------------------------

    def generate(self) -> bytes:
        """Return the patched (level2) CBOOT."""
        buf = bytearray(self.cboot)

        # 1. Apply byte patches.
        for name, pattern, off, value, required in self._patches:
            if name == 'Mode23Range' and not self.enable_mode23:
                continue
            if name == 'Mode23Replace' and not self.replace_mode23:
                continue
            matches = locate_pattern(bytes(buf), pattern)
            if len(matches) != 1:
                if required:
                    raise RuntimeError(
                        f"{name}: expected 1 match, found {len(matches)} — "
                        f"cannot generate level2"
                    )
                self._log(f"{name}: not found, skipping (optional)")
                continue
            pos = matches[0] + off
            old = buf[pos]
            buf[pos] = value
            self._log(f"{name}: patch @0x{pos:06X} 0x{old:02X}->0x{value:02X}")

        # 2. Recompute CRC1 (ranges are absolute physical addresses).
        crc1_struct = SiemensCrcStructure.parse(bytes(buf), _CRC1_OFFSET)
        crc1_data = self._gather_crc1_bytes(bytes(buf), crc1_struct)
        new_crc1 = siemens_crc32(crc1_data, init=crc1_struct.init_value)
        crc1_struct.write_actual(buf, new_crc1)
        self._log(f"CRC1: was=0x{crc1_struct.actual_value:08X} now=0x{new_crc1:08X} "
                  f"({len(crc1_struct.ranges)} ranges, {len(crc1_data)} bytes)")

        # 3. Recompute CRC2 (ranges are relative to the first range start).
        crc2_struct = SiemensCrcStructure.parse(bytes(buf), _CRC2_OFFSET)
        crc2_data = self._gather_crc2_bytes(bytes(buf), crc2_struct)
        new_crc2 = siemens_crc32(crc2_data, init=crc2_struct.init_value)
        crc2_struct.write_actual(buf, new_crc2)
        self._log(f"CRC2: was=0x{crc2_struct.actual_value:08X} now=0x{new_crc2:08X} "
                  f"({len(crc2_struct.ranges)} ranges, {len(crc2_data)} bytes)")

        return bytes(buf)

    # -- helpers -----------------------------------------------------------

    def _gather_crc1_bytes(self, data: bytes, s: SiemensCrcStructure) -> bytes:
        """CRC1 ranges use absolute physical addresses.

        file_offset = range.start - bootloader_physical_address
        """
        out = bytearray()
        for r in s.ranges:
            off = r.start - self.phys
            if off < 0 or off + r.length > len(data):
                raise RuntimeError(
                    f"CRC1 range 0x{r.start:08X}-0x{r.stop:08X} outside CBOOT "
                    f"(phys=0x{self.phys:08X})"
                )
            out.extend(data[off:off + r.length])
        return bytes(out)

    def _gather_crc2_bytes(self, data: bytes, s: SiemensCrcStructure) -> bytes:
        """CRC2 ranges are relative to the first range's start address.

        file_offset = range[i].start - range[0].start
        """
        if not s.ranges:
            return b""
        base = s.ranges[0].start
        out = bytearray()
        for r in s.ranges:
            off = r.start - base
            if off < 0 or off + r.length > len(data):
                raise RuntimeError(
                    f"CRC2 range 0x{r.start:08X}-0x{r.stop:08X} outside CBOOT "
                    f"(base=0x{base:08X})"
                )
            out.extend(data[off:off + r.length])
        return bytes(out)

    # -- verification ------------------------------------------------------

    def verify_original(self) -> bool:
        """Check that the *original* CBOOT's stored CRC matches our computation.

        Useful as a sanity check before generating the level2 patch — if this
        fails, the bootloader physical address or CPU family is wrong.
        """
        s1 = SiemensCrcStructure.parse(self.cboot, _CRC1_OFFSET)
        try:
            computed = siemens_crc32(self._gather_crc1_bytes(self.cboot, s1),
                                     init=s1.init_value)
        except RuntimeError:
            return False
        return computed == s1.actual_value


# ---------------------------------------------------------------------------
# CPU / ECU mapping
# ---------------------------------------------------------------------------

# Maps ECU type -> list of (CPU family, bootloader physical address) candidates.
# SIM2K250 has two layout variants depending on the bootloader version:
#   - Layout_80010000  (CBOOT length 0x0FE00, phys 0x80010000) — older 606A1/A4
#   - Layout_80020000  (CBOOT length 0x1FE00, phys 0x80020000) — 606Z0/X1 and later
# The correct variant is auto-detected from the CRC structure at runtime.
ECU_CPU_MAP = {
    "SIM2K250": [("TC1782", 0x80010000), ("TC1782", 0x80020000)],
    "SIM2K260": [("TC1791", 0x80020000)],
    "SIM2K305": [("TC1782", 0x80020000)],
}


def _detect_phys(cboot: bytes, candidates) -> int:
    """Try each candidate physical address and return the one whose CRC ranges
    fit inside the CBOOT and whose stored CRC matches our computation."""
    for phys in candidates:
        try:
            s1 = SiemensCrcStructure.parse(cboot, _CRC1_OFFSET)
            off = s1.ranges[0].start - phys
            if off < 0:
                continue
            last = s1.ranges[-1]
            if last.stop - phys + 1 > len(cboot):
                continue
            data = bytearray()
            for r in s1.ranges:
                o = r.start - phys
                data.extend(cboot[o:o + r.length])
            if siemens_crc32(bytes(data), init=s1.init_value) == s1.actual_value:
                return phys
        except (RuntimeError, IndexError, struct.error):
            continue
    # Fallback: return the first candidate; the generator will raise a clearer error.
    return candidates[0] if candidates else 0


def generate_level2(cboot: bytes, ecu_type: str, enable_mode23: bool = True,
                    replace_mode23: bool = True, log=None) -> bytes:
    """Convenience wrapper: generate a level2 CBOOT for the given ECU type.

    The bootloader physical address is auto-detected from the CRC structure.
    """
    if ecu_type not in ECU_CPU_MAP:
        raise ValueError(f"unsupported ECU type: {ecu_type!r}")
    candidates = ECU_CPU_MAP[ecu_type]
    phys = _detect_phys(cboot, [p for _, p in candidates])
    cpu = candidates[0][0]  # all candidates share the same CPU for a given ECU
    gen = Level2Generator(cboot, phys, cpu=cpu,
                          enable_mode23=enable_mode23,
                          replace_mode23=replace_mode23,
                          log=log)
    return gen.generate()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 clams_level2.py <original_cboot.bin> <ecu_type> [output.bin]")
        print(f"  ECU types: {', '.join(ECU_CPU_MAP)}")
        sys.exit(1)
    path = sys.argv[1]
    ecu = sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else None
    with open(path, 'rb') as f:
        data = f.read()
    if ecu not in ECU_CPU_MAP:
        print(f"Error: unknown ECU type {ecu!r}. Supported: {', '.join(ECU_CPU_MAP)}")
        sys.exit(1)
    gen = None
    for cpu, phys in ECU_CPU_MAP[ecu]:
        g = Level2Generator(data, phys, cpu=cpu,
                             log=lambda m: print(f"  {m}"))
        if g.verify_original():
            gen = g
            print(f"  Auto-detected bootloader physical address: 0x{phys:08X} ({cpu})")
            break
    if gen is None:
        print("WARNING: original CRC does not match for any candidate physical address.")
        print("         Trying first candidate anyway...")
        cpu, phys = ECU_CPU_MAP[ecu][0]
        gen = Level2Generator(data, phys, cpu=cpu,
                              log=lambda m: print(f"  {m}"))
    level2 = gen.generate()
    if out:
        with open(out, 'wb') as f:
            f.write(level2)
        print(f"Wrote {len(level2)} bytes to {out}")
    else:
        print(f"Generated level2: {len(level2)} bytes (not written to file)")
