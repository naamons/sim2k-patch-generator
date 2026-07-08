#!/usr/bin/env python3
"""
Generate sim2k_db.py from ECU sample files.

This script scans a directory tree (e.g. the Hyundai dataset) for:
  - CBOOT original files   (e.g. 606A1_C2.bin)
  - Level2 CBOOT files     (e.g. 606A1_C2.level2.bin)
  - Overwrite files        (e.g. 606TA051.606A1_C2.bin)
  - Calibration folders    (e.g. CNNFJM___TAA)

It builds a sim2k_db.py module that the patch generator can import to
auto-resolve CBOOT / level2 / overwrite by calibration reference.

Usage:
    python3 generate_db.py /path/to/Hyundai/samples
"""

import sys
import os
import hashlib
import base64
import zlib
from pathlib import Path


def find_cboot_pairs(samples_dir):
    """
    Find all CBOOT original + level2 pairs.
    Returns: dict of {variant: (orig_bytes, level2_bytes, md5)}
    """
    pairs = {}
    
    for level2_path in sorted(Path(samples_dir).rglob('*.level2.bin')):
        # Skip if filename contains 'level1'
        if 'level1' in level2_path.name:
            continue
        
        # Find corresponding original
        orig_name = level2_path.name.replace('.level2.bin', '.bin')
        orig_path = level2_path.parent / orig_name
        
        if not orig_path.exists():
            continue
        
        # Extract variant name (e.g. 606A1_C2)
        variant = level2_path.stem.replace('.level2', '')
        
        with open(orig_path, 'rb') as f:
            orig_data = f.read()
        with open(level2_path, 'rb') as f:
            level2_data = f.read()
        
        # Use the level2 MD5 as key to avoid duplicates
        md5 = hashlib.md5(level2_data).hexdigest()
        
        if variant not in pairs:
            pairs[variant] = []
        
        pairs[variant].append({
            'orig': orig_data,
            'level2': level2_data,
            'level2_md5': md5,
            'path': str(level2_path),
        })
    
    return pairs


def find_overwrite_files(samples_dir):
    """
    Find all overwrite files (*.CBOOT.bin pattern).
    Returns: dict of {calibration_ref: (asw_ref, variant, overwrite_bytes, md5)}
    """
    overwrites = {}
    
    # Pattern: <ASWREF>.<BOOTLOADER>.bin
    for p in sorted(Path(samples_dir).rglob('*.*_C2.bin')):
        name = p.name
        parts = name.split('.')
        
        # Must have exactly 3 parts: ASWREF.BOOTLOADER.bin
        if len(parts) != 3:
            continue
        if not parts[1].endswith('_C2'):
            continue
        
        asw_ref = parts[0]
        variant = parts[1]
        
        # Get calibration folder name
        cal_folder = p.parent.parent.name
        
        with open(p, 'rb') as f:
            data = f.read()
        
        md5 = hashlib.md5(data).hexdigest()
        
        overwrites[cal_folder] = {
            'asw_ref': asw_ref,
            'variant': variant,
            'data': data,
            'md5': md5,
            'path': str(p),
        }
    
    return overwrites


def find_calibration_mapping(samples_dir, cboot_pairs):
    """
    Map calibration folders to their CBOOT variant and level2 MD5.
    """
    mapping = {}
    
    for cal_dir in sorted(Path(samples_dir).rglob('[A-Z]*')):
        if not cal_dir.is_dir():
            continue
        
        # Find CBOOT subfolder
        for subdir in cal_dir.iterdir():
            if not subdir.is_dir():
                continue
            if '_C2' not in subdir.name:
                continue
            
            variant = subdir.name
            level2_file = subdir / f'{variant}.level2.bin'
            orig_file = subdir / f'{variant}.bin'
            
            if level2_file.exists() and orig_file.exists():
                with open(level2_file, 'rb') as f:
                    level2_data = f.read()
                md5 = hashlib.md5(level2_data).hexdigest()
                
                mapping[cal_dir.name] = (variant, md5)
                break
    
    return mapping


def generate_db_module(samples_dir, output_path='sim2k_db.py'):
    """
    Generate the sim2k_db.py module.
    """
    print(f'Scanning {samples_dir}...')
    
    # Find CBOOT pairs
    cboot_pairs = find_cboot_pairs(samples_dir)
    print(f'Found {sum(len(v) for v in cboot_pairs.values())} CBOOT pairs across {len(cboot_pairs)} variants')
    
    # Find overwrite files
    overwrites = find_overwrite_files(samples_dir)
    print(f'Found {len(overwrites)} overwrite files')
    
    # Build calibration mapping
    cal_mapping = find_calibration_mapping(samples_dir, cboot_pairs)
    print(f'Mapped {len(cal_mapping)} calibrations')
    
    # Deduplicate CBOOT files by variant (use first occurrence)
    unique_cboots = {}
    unique_level2s = {}
    
    for variant, pairs_list in cboot_pairs.items():
        # Use first occurrence for original CBOOT
        unique_cboots[variant] = pairs_list[0]['orig']
        
        # Deduplicate level2 by MD5
        for pair in pairs_list:
            md5 = pair['level2_md5']
            if md5 not in unique_level2s:
                unique_level2s[md5] = pair['level2']
    
    # Deduplicate overwrites by MD5
    unique_overwrites = {}
    overwrite_map = {}
    
    for cal, info in overwrites.items():
        md5 = info['md5']
        if md5 not in unique_overwrites:
            unique_overwrites[md5] = info['data']
        overwrite_map[cal] = (info['asw_ref'], info['variant'], md5)
    
    # Build calibration -> level2 mapping
    level2_map = {}
    for cal, (variant, md5) in cal_mapping.items():
        level2_map[cal] = (variant, md5)
    
    # Generate Python module
    lines = [
        '"""Auto-generated SIM2K bootloader / level2 / overwrite database."""',
        '',
        'import base64, zlib',
        '',
    ]
    
    # LEVEL2_MAP
    lines.append('# Calibration reference -> (bootloader variant, level2 MD5)')
    lines.append('LEVEL2_MAP = {')
    for cal, (variant, md5) in sorted(level2_map.items()):
        lines.append(f'    "{cal}": ("{variant}", "{md5}"),')
    lines.append('}')
    lines.append('')
    
    # OVERWRITE_MAP
    lines.append('# Calibration reference -> (ASW reference, bootloader variant, overwrite MD5)')
    lines.append('OVERWRITE_MAP = {')
    for cal, (asw, variant, md5) in sorted(overwrite_map.items()):
        lines.append(f'    "{cal}": ("{asw}", "{variant}", "{md5}"),')
    lines.append('}')
    lines.append('')
    
    # Helper to emit compressed byte dicts
    def emit_bytes_dict(name, d):
        lines.append(f'{name} = {{')
        for key, data in sorted(d.items()):
            compressed = zlib.compress(data)
            b64 = base64.b64encode(compressed).decode('ascii')
            lines.append(f'    "{key}": zlib.decompress(base64.b64decode("{b64}")),')
        lines.append('}')
        lines.append('')
    
    # CBOOT_ORIGINALS
    lines.append('# Bootloader variant -> original CBOOT bytes')
    emit_bytes_dict('CBOOT_ORIGINALS', unique_cboots)
    
    # LEVEL2_PATCHED
    lines.append('# Level2 MD5 -> patched level2 CBOOT bytes')
    emit_bytes_dict('LEVEL2_PATCHED', unique_level2s)
    
    # OVERWRITE_CONTENTS
    lines.append('# Overwrite MD5 -> overwrite content bytes')
    emit_bytes_dict('OVERWRITE_CONTENTS', unique_overwrites)
    
    # Resolve functions
    lines.append('''
def resolve(calibration_ref, bootloader_ref=None):
    """Return (original_cboot, level2_cboot) for a calibration reference."""
    if calibration_ref not in LEVEL2_MAP:
        return None, None
    variant, md5 = LEVEL2_MAP[calibration_ref]
    if bootloader_ref and bootloader_ref != variant:
        return None, None
    return CBOOT_ORIGINALS.get(variant), LEVEL2_PATCHED.get(md5)


def resolve_overwrite(calibration_ref):
    """Return overwrite content for a calibration reference."""
    if calibration_ref not in OVERWRITE_MAP:
        return None
    asw, variant, md5 = OVERWRITE_MAP[calibration_ref]
    return OVERWRITE_CONTENTS.get(md5)
''')
    
    # Write file
    with open(output_path, 'w') as f:
        f.write('\n'.join(lines))
    
    file_size = os.path.getsize(output_path)
    print(f'Generated {output_path} ({file_size:,} bytes)')
    print(f'  - {len(level2_map)} calibrations')
    print(f'  - {len(unique_cboots)} unique CBOOT originals')
    print(f'  - {len(unique_level2s)} unique level2 files')
    print(f'  - {len(unique_overwrites)} unique overwrite files')


def main():
    if len(sys.argv) < 2:
        print('Usage: python3 generate_db.py /path/to/Hyundai/samples')
        print()
        print('The samples directory should contain calibration folders with:')
        print('  - CBOOT subfolders (e.g. 606A1_C2/) containing .bin and .level2.bin')
        print('  - Overwrite files (e.g. 606TA051.606A1_C2.bin)')
        sys.exit(1)
    
    samples_dir = sys.argv[1]
    if not os.path.isdir(samples_dir):
        print(f'Error: {samples_dir} is not a directory')
        sys.exit(1)
    
    generate_db_module(samples_dir)


if __name__ == '__main__':
    main()
