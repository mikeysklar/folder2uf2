#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT
#
# folder2uf2: pack a folder into a FAT filesystem image wrapped in a UF2,
# targeting the CIRCUITPY drive region of an RP2040/RP2350 CircuitPython
# board. Lets a product ship code + libraries + assets as one drag-and-drop
# UF2 alongside (or after) the CircuitPython firmware UF2.
#
# The FAT geometry replicates what CircuitPython itself creates: oofatfs
# f_mkfs(FM_FAT, au=0) called on a virtual device whose sector 0 is a
# synthesized MBR with partition 1 at LBA 1 (supervisor/shared/flash.c).
# The raw flash region therefore holds a bare FAT volume (no MBR), whose
# BPB records 1 hidden sector. See README.md for the source derivation.
#
# Idea credit: jepler's archived mkfatimg (C/oofatfs) explored the same
# folder -> FAT image direction; this is an independent stdlib-only
# implementation.

import argparse
import os
import random
import struct
import sys
import time

SECTOR = 512
UF2_MAGIC0 = 0x0A324655
UF2_MAGIC1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_FAMILY_ID = 0x00002000
UF2_PAYLOAD = 256

FAMILY_RP2040 = 0xE48BFF56
FAMILY_RP2350_ARM_S = 0xE48BFF59
FAMILY_ABSOLUTE = 0xE48BFF57  # rp2xxx absolute (data anywhere in flash)

XIP_BASE = 0x10000000

# drive_start = CIRCUITPY_FIRMWARE_SIZE + CIRCUITPY_INTERNAL_NVM_SIZE (4K).
# Default firmware size is 1020K (ports/raspberrypi/mpconfigport.h); boards
# with CYW43 wifi override it to 1536K in their mpconfigboard.mk.
# flash_total from EXTERNAL_FLASH_DEVICES part number.
BOARDS = {
    "raspberry_pi_pico": (FAMILY_RP2040, 0x100000, 2 * 1024 * 1024),
    "raspberry_pi_pico_w": (FAMILY_RP2040, 0x181000, 2 * 1024 * 1024),
    "raspberry_pi_pico2": (FAMILY_RP2350_ARM_S, 0x100000, 4 * 1024 * 1024),
    "raspberry_pi_pico2_w": (FAMILY_RP2350_ARM_S, 0x181000, 4 * 1024 * 1024),
    "adafruit_feather_rp2040": (FAMILY_RP2040, 0x100000, 8 * 1024 * 1024),
    "adafruit_qtpy_rp2040": (FAMILY_RP2040, 0x100000, 8 * 1024 * 1024),
    "adafruit_metro_rp2350": (FAMILY_RP2350_ARM_S, 0x100000, 16 * 1024 * 1024),
    "adafruit_fruit_jam": (FAMILY_RP2350_ARM_S, 0x100000, 16 * 1024 * 1024),
}

# Espressif boards keep the CIRCUITPY drive in a data/fat partition. tinyuf2
# on ESP32 only writes ota_0, so these produce a raw image for esptool rather
# than a UF2. Values are (offset, size) from
# ports/espressif/esp-idf-config/partitions-*.csv, sized to the fat partition
# ALONE, which is always safe.
#
# Real boards vary, so prefer --partition-table with a dump read off the chip:
#   esptool --before no-reset --after no-reset read-flash 0x8000 0xc00 pt.bin
# A Metro ESP32-S3 16MB has ffat 11968K plus an ota_1 to extend into; a Metro
# ESP32-S2 4MB has ffat 960K and no ota_1 at all.
#
# With CIRCUITPY_STORAGE_EXTEND the filesystem may span the fat partition PLUS
# the spare OTA partition, since supervisor_flash_get_block_count() returns
# (_partition[0]->size + _partition[1]->size). The port decides at boot with
#     storage_extended = (_partition[0]->size < fatfs_bytes());
# so sizing to the fat partition alone turns extension off and gives a smaller
# drive, while over-sizing writes a filesystem that runs past its partition.
# Use --storage-extend WITH --partition-table to size it correctly.
ESP_PARTITIONS = {
    "esp32_4mb": (0x310000, 960 * 1024),
    "esp32_8mb": (0x450000, 3776 * 1024),
    "esp32_16mb": (0x450000, 11968 * 1024),
}


def parse_partition_table(path):
    """Parse an esp-idf partition table dump. Returns (fat_offset, fat_size,
    next_ota_size). next_ota_size is 0 when the table has no spare OTA slot."""
    d = open(path, "rb").read()
    fat = None
    otas = []
    for i in range(0, len(d) - 32, 32):
        e = d[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        _, ptype, subtype, off, size, raw, _ = struct.unpack("<HBBII16sI", e)
        name = raw.rstrip(b"\x00").decode("ascii", "replace")
        if ptype == 1 and subtype == 0x81:      # data, fat
            fat = (off, size, name)
        elif ptype == 0 and 0x10 <= subtype <= 0x1f:  # app, ota_N
            otas.append((subtype, off, size, name))
    if fat is None:
        raise ValueError("%s has no data/fat partition" % path)
    # CircuitPython extends into the *next* OTA slot, which only exists when
    # the table defines more than one.
    next_ota = sorted(otas)[1][2] if len(otas) > 1 else 0
    return fat[0], fat[1], next_ota

MAX_FAT12 = 0xFF5
MAX_FAT16 = 0xFFF5


class Geometry:
    """FAT12/16 geometry exactly as oofatfs f_mkfs computes it for
    CircuitPython's flash blockdev (sz_blk=1, ss=512, au=0, 1 FAT)."""

    def __init__(self, volume_sectors):
        sz_vol = volume_sectors
        if sz_vol < 22:
            raise ValueError("volume too small")
        cst = [1, 4, 16, 64, 256, 512]
        au = 0
        while True:
            pau = au
            if pau == 0:
                # C: for (i=0, pau=1; cst[i] && cst[i] <= n; i++, pau <<= 1);
                n = sz_vol // 0x1000
                pau = 1
                for c in cst:
                    if c <= n:
                        pau <<= 1
                    else:
                        break
            n_clst = sz_vol // pau
            if n_clst > MAX_FAT12:
                fmt = 16
                nbytes = n_clst * 2 + 4
            else:
                fmt = 12
                nbytes = (n_clst * 3 + 1) // 2 + 3
            sz_fat = (nbytes + SECTOR - 1) // SECTOR
            sz_rsv = 1
            n_rootdir = 128 if sz_vol <= 256 else 512
            sz_dir = n_rootdir * 32 // SECTOR
            b_data = sz_rsv + sz_fat + sz_dir  # relative to volume start
            # sz_blk == 1 so no erase-block alignment adjustment
            if sz_vol < b_data + pau * 16:
                raise ValueError("volume too small")
            n_clst = (sz_vol - sz_rsv - sz_fat - sz_dir) // pau
            if fmt == 16:
                if n_clst > MAX_FAT16:
                    if au == 0 and pau * 2 <= 64:
                        au = pau * 2
                        continue
                    raise ValueError("too many clusters for FAT16")
                if n_clst <= MAX_FAT12:
                    au = pau * 2
                    if au <= 128:
                        continue
                    raise ValueError("cluster count in FAT12/16 gap")
            if fmt == 12 and n_clst > MAX_FAT12:
                raise ValueError("too many clusters for FAT12")
            break
        self.fat_type = fmt
        self.sectors_per_cluster = pau
        self.reserved = sz_rsv
        self.fat_sectors = sz_fat
        self.root_entries = n_rootdir
        self.rootdir_sectors = sz_dir
        self.total_sectors = sz_vol
        self.cluster_count = n_clst
        self.data_start = b_data  # sector index within volume
        self.cluster_bytes = pau * SECTOR


INVALID_SFN = set('"*+,./:;<=>?[\\]| ')


def _sfn_char(c):
    c = c.upper()
    if ord(c) < 0x20 or c in INVALID_SFN:
        return "_"
    return c


def make_sfn(name, used):
    """Generate an 8.3 short name (11 bytes, space padded). Returns
    (sfn_bytes, needs_lfn)."""
    if "." in name and not name.startswith("."):
        base, _, ext = name.rpartition(".")
    else:
        base, ext = name, ""
    lossy = name.startswith(".")
    base_f = "".join(_sfn_char(c) for c in base if c not in ". ")
    ext_f = "".join(_sfn_char(c) for c in ext if c not in ". ")
    if base_f != base or ext_f != ext or len(base_f) > 8 or len(ext_f) > 3 \
            or not base_f:
        lossy = True
    fits = (not lossy and base == base_f and ext == ext_f
            and len(base_f) <= 8 and len(ext_f) <= 3)
    sfn = None
    if fits:
        cand = (base_f[:8].ljust(8) + ext_f[:3].ljust(3)).encode("ascii")
        if cand not in used:
            sfn = cand
    if sfn is None:
        base_f = (base_f or "_")[:8]
        for tail in range(1, 1000000):
            t = "~%d" % tail
            cand_base = base_f[: 8 - len(t)] + t
            cand = (cand_base.ljust(8) + ext_f[:3].ljust(3)).encode("ascii")
            if cand not in used:
                sfn = cand
                break
        lossy = True
    used.add(sfn)
    return sfn, lossy or not fits


def lfn_checksum(sfn):
    s = 0
    for b in sfn:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def lfn_entries(name, sfn):
    """VFAT long-name entries, in on-disk order (last logical first)."""
    u = name.encode("utf-16-le")
    chars = list(struct.unpack("<%dH" % (len(u) // 2), u))
    chars.append(0)
    while len(chars) % 13:
        chars.append(0xFFFF)
    n = len(chars) // 13
    ck = lfn_checksum(sfn)
    out = []
    for i in range(n - 1, -1, -1):
        seq = i + 1
        if i == n - 1:
            seq |= 0x40
        c = chars[i * 13:(i + 1) * 13]
        ent = struct.pack(
            "<B10sBBB12sH4s",
            seq,
            struct.pack("<5H", *c[0:5]),
            0x0F, 0, ck,
            struct.pack("<6H", *c[5:11]),
            0,
            struct.pack("<2H", *c[11:13]),
        )
        out.append(ent)
    return out


def fat_datetime(ts):
    t = time.localtime(ts)
    year = max(1980, min(2107, t.tm_year))
    d = ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return d, tm


def dirent(sfn, attr, cluster, size, ts):
    d, tm = fat_datetime(ts)
    return struct.pack(
        "<11sBBBHHHHHHHI",
        sfn, attr, 0, 0, tm, d, d, 0, tm, d, cluster & 0xFFFF, size)


ATTR_DIR = 0x10
ATTR_VOLUME = 0x08


class Node:
    def __init__(self, name, path=None, is_dir=False, data=None, ts=None):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.data = data  # bytes for synthesized files
        self.ts = ts
        self.children = []  # for dirs
        self.cluster = 0
        self.size = 0


EXTRA_FILES = [
    # (path-in-image, data) -- what CircuitPython's own formatter creates
    # to keep macOS/Linux from writing trash/index files (filesystem.c).
    (".fseventsd/no_log", b""),
    (".metadata_never_index", b""),
    (".Trashes", b""),
    (".Trash-1000", b""),
]


def build_tree(src, extra_files=True, build_ts=None):
    if build_ts is None:
        build_ts = time.time()
    root = Node("", is_dir=True, ts=build_ts)

    def get_dir(node, name, ts):
        for c in node.children:
            if c.name == name and c.is_dir:
                return c
        d = Node(name, is_dir=True, ts=ts)
        node.children.append(d)
        return d

    if extra_files:
        for relpath, data in EXTRA_FILES:
            parts = relpath.split("/")
            cur = root
            for p in parts[:-1]:
                cur = get_dir(cur, p, build_ts)
            cur.children.append(Node(parts[-1], data=data, ts=build_ts))

    def walk(dirpath, node):
        for entry in sorted(os.listdir(dirpath)):
            if entry in (".DS_Store",):
                continue
            full = os.path.join(dirpath, entry)
            st = os.stat(full)
            if os.path.isdir(full):
                sub = get_dir(node, entry, st.st_mtime)
                sub.ts = st.st_mtime
                walk(full, sub)
            else:
                # user file replaces a synthesized one of the same name
                node.children = [c for c in node.children
                                 if not (c.name == entry and not c.is_dir)]
                node.children.append(
                    Node(entry, path=full, ts=st.st_mtime,
                         data=None))
    walk(src, root)
    return root


class FatBuilder:
    def __init__(self, geo, label="CIRCUITPY", volume_id=None, build_ts=None):
        self.geo = geo
        self.label = label
        self.volume_id = volume_id if volume_id is not None \
            else random.getrandbits(32)
        self.build_ts = build_ts if build_ts is not None else time.time()
        self.image = bytearray(geo.total_sectors * SECTOR)
        self.fat = [0] * (geo.cluster_count + 2)
        self.next_cluster = 2

    def alloc_chain(self, nbytes):
        """Allocate a contiguous cluster chain; returns first cluster."""
        n = max(1, (nbytes + self.geo.cluster_bytes - 1)
                // self.geo.cluster_bytes)
        if self.next_cluster + n - 1 > self.geo.cluster_count + 1:
            raise ValueError(
                "content does not fit: need %d more clusters" % n)
        first = self.next_cluster
        for i in range(n):
            c = first + i
            self.fat[c] = c + 1 if i < n - 1 else \
                (0xFFF if self.geo.fat_type == 12 else 0xFFFF)
        self.next_cluster += n
        return first

    def cluster_offset(self, cluster):
        return (self.geo.data_start + (cluster - 2)
                * self.geo.sectors_per_cluster) * SECTOR

    def write_cluster_data(self, cluster, data):
        off = self.cluster_offset(cluster)
        self.image[off:off + len(data)] = data

    def entries_for(self, node, used_sfns):
        sfn, needs_lfn = make_sfn(node.name, used_sfns)
        ents = lfn_entries(node.name, sfn) if needs_lfn else []
        attr = ATTR_DIR if node.is_dir else 0
        ents.append(dirent(sfn, attr, node.cluster, node.size, node.ts))
        return ents

    def layout(self, root):
        # Pass 1: read file data, then allocate clusters depth-first the
        # way FatFs would create them sequentially.
        def prep(node):
            for c in node.children:
                if c.is_dir:
                    prep(c)
                else:
                    if c.data is None:
                        with open(c.path, "rb") as f:
                            c.data = f.read()
                    c.size = len(c.data)

        def alloc(node, is_root):
            if not is_root:
                # entry count: . and .. plus children entries (with LFNs)
                count = 2
                used = set()
                for c in node.children:
                    _, needs = make_sfn(c.name, used)
                    count += 1 + (len(lfn_entries(c.name, b"x" * 11))
                                  if needs else 0)
                node.size = 0
                node.cluster = self.alloc_chain(count * 32)
            for c in node.children:
                if c.is_dir:
                    alloc(c, False)
            for c in node.children:
                if not c.is_dir:
                    c.cluster = self.alloc_chain(c.size) if c.size else 0
                    if c.size:
                        self.write_cluster_data(c.cluster, c.data)
        prep(root)
        alloc(root, True)

        # Pass 2: emit directory entries.
        def emit(node, parent_cluster, is_root):
            used = set()
            out = b""
            if is_root:
                d, tm = fat_datetime(self.build_ts)
                out += struct.pack(
                    "<11sBBBHHHHHHHI",
                    self.label.upper().ljust(11)[:11].encode("ascii"),
                    ATTR_VOLUME, 0, 0, tm, d, d, 0, tm, d, 0, 0)
            else:
                out += dirent(b".          ", ATTR_DIR, node.cluster, 0,
                              node.ts)
                out += dirent(b"..         ", ATTR_DIR,
                              parent_cluster if parent_cluster != 0 else 0,
                              0, node.ts)
            for c in node.children:
                out += b"".join(self.entries_for(c, used))
            if is_root:
                cap = self.geo.root_entries * 32
                if len(out) > cap:
                    raise ValueError("too many entries in root directory")
                off = self.geo.reserved * SECTOR \
                    + self.geo.fat_sectors * SECTOR
                self.image[off:off + len(out)] = out
            else:
                self.write_cluster_data(node.cluster, out)
            for c in node.children:
                if c.is_dir:
                    emit(c, 0 if is_root else node.cluster, False)
        emit(root, 0, True)
        self.write_boot_sector()
        self.write_fat()

    def write_boot_sector(self):
        g = self.geo
        b = bytearray(SECTOR)
        b[0:3] = b"\xEB\xFE\x90"
        b[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", b, 11, SECTOR)          # bytes/sector
        b[13] = g.sectors_per_cluster
        struct.pack_into("<H", b, 14, g.reserved)      # reserved sectors
        b[16] = 1                                      # number of FATs
        struct.pack_into("<H", b, 17, g.root_entries)
        if g.total_sectors < 0x10000:
            struct.pack_into("<H", b, 19, g.total_sectors)
        else:
            struct.pack_into("<I", b, 32, g.total_sectors)
        b[21] = 0xF8                                   # media descriptor
        struct.pack_into("<H", b, 22, g.fat_sectors)
        struct.pack_into("<H", b, 24, 63)              # sectors/track
        struct.pack_into("<H", b, 26, 255)             # heads
        # BPB_HiddSec: volume begins at LBA 1 of CircuitPython's virtual
        # MBR device (supervisor/shared/flash.c PART1_START_BLOCK).
        struct.pack_into("<I", b, 28, 1)
        b[36] = 0x80                                   # drive number
        b[38] = 0x29                                   # ext boot signature
        struct.pack_into("<I", b, 39, self.volume_id)
        # oofatfs f_mkfs leaves the BPB label as NO NAME; the real label
        # lives in the root directory entry (f_setlabel behavior).
        # oofatfs writes literally "FAT     " here for FAT12 and FAT16
        b[43:62] = b"NO NAME    " + b"FAT     "
        b[510] = 0x55
        b[511] = 0xAA
        self.image[0:SECTOR] = b

    def write_fat(self):
        g = self.geo
        self.fat[0] = 0xF8 | (0xF00 if g.fat_type == 12 else 0xFF00)
        self.fat[1] = 0xFFF if g.fat_type == 12 else 0xFFFF
        raw = bytearray(g.fat_sectors * SECTOR)
        if g.fat_type == 16:
            for i, v in enumerate(self.fat):
                struct.pack_into("<H", raw, i * 2, v & 0xFFFF)
        else:
            for i, v in enumerate(self.fat):
                off = i * 3 // 2
                if i % 2 == 0:
                    raw[off] = v & 0xFF
                    raw[off + 1] = (raw[off + 1] & 0xF0) | ((v >> 8) & 0x0F)
                else:
                    raw[off] = (raw[off] & 0x0F) | ((v << 4) & 0xF0)
                    raw[off + 1] = (v >> 4) & 0xFF
        off = g.reserved * SECTOR
        self.image[off:off + len(raw)] = raw

    def used_bytes(self):
        """Bytes of image actually meaningful (metadata + allocated
        clusters), for trimmed UF2 output."""
        end_cluster = self.next_cluster
        off = self.cluster_offset(end_cluster - 1) + self.geo.cluster_bytes \
            if end_cluster > 2 else self.geo.data_start * SECTOR
        return off


def image_blocks(image, target_addr, trim_to=None):
    """Split an image into (address, payload) UF2 block payloads."""
    data = image if trim_to is None else image[:trim_to]
    # pad to erase-sector (4K) boundary so the bootloader erases/writes
    # whole sectors deterministically
    pad = (-len(data)) % 4096
    data = bytes(data) + b"\xff" * pad
    n = (len(data) + UF2_PAYLOAD - 1) // UF2_PAYLOAD
    return [(target_addr + i * UF2_PAYLOAD,
             data[i * UF2_PAYLOAD:(i + 1) * UF2_PAYLOAD]) for i in range(n)]


def read_uf2(path):
    """Return (blocks, family_id) from an existing UF2 file."""
    raw = open(path, "rb").read()
    if len(raw) % SECTOR:
        raise ValueError("%s is not a whole number of 512-byte UF2 blocks"
                         % path)
    blocks = []
    family = None
    for i in range(len(raw) // SECTOR):
        b = raw[i * SECTOR:(i + 1) * SECTOR]
        m0, m1, flags, addr, size, _no, _total, fam = struct.unpack(
            "<IIIIIIII", b[:32])
        if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1:
            raise ValueError("%s: block %d has a bad UF2 magic" % (path, i))
        if flags & UF2_FLAG_FAMILY_ID:
            if family is not None and fam != family:
                raise ValueError("%s mixes family ids 0x%08x and 0x%08x"
                                 % (path, family, fam))
            family = fam
        blocks.append((addr, b[32:32 + size]))
    return blocks, family


def write_uf2_blocks(path, blocks, family_id):
    """Write blocks as one UF2, numbering them across the whole file."""
    total = len(blocks)
    with open(path, "wb") as f:
        for i, (addr, chunk) in enumerate(blocks):
            head = struct.pack(
                "<IIIIIIII",
                UF2_MAGIC0, UF2_MAGIC1, UF2_FLAG_FAMILY_ID,
                addr, len(chunk), i, total, family_id)
            f.write(head + chunk.ljust(476, b"\x00")
                    + struct.pack("<I", UF2_MAGIC_END))
    return total


def write_uf2(path, image, target_addr, family_id, trim_to=None):
    return write_uf2_blocks(
        path, image_blocks(image, target_addr, trim_to), family_id)


def parse_size(s):
    return int(s, 0)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="folder2uf2",
        description="Pack a folder into a CIRCUITPY FAT filesystem UF2 "
                    "for RP2040/RP2350 CircuitPython boards.")
    p.add_argument("source", nargs="?", help="folder to pack")
    p.add_argument("-o", "--output", help="output UF2 path (default: "
                   "<source>.uf2)")
    p.add_argument("--board", choices=sorted(BOARDS) + sorted(ESP_PARTITIONS),
                   help="board name (sets flash offset, size, family id). "
                   "esp32_* targets write a raw image for esptool, not a UF2")
    p.add_argument("--combine", metavar="FIRMWARE.UF2",
                   help="prepend a firmware UF2 so one file flashes both "
                   "firmware and filesystem")
    p.add_argument("--flash-offset", type=parse_size,
                   help="CIRCUITPY drive start offset in flash (e.g. "
                   "0x100000)")
    p.add_argument("--flash-size", type=parse_size,
                   help="total flash size (drive runs to end of flash)")
    p.add_argument("--fs-size", type=parse_size,
                   help="filesystem size in bytes (overrides "
                   "flash-size - flash-offset)")
    p.add_argument("--family-id", type=parse_size,
                   help="UF2 family id (default from --board)")
    p.add_argument("--base-addr", type=parse_size, default=XIP_BASE,
                   help="flash XIP base address (default 0x10000000)")
    p.add_argument("--label", default="CIRCUITPY",
                   help="volume label (default CIRCUITPY)")
    p.add_argument("--volume-id", type=parse_size,
                   help="FAT volume serial number (default random)")
    p.add_argument("--full", action="store_true",
                   help="write the entire filesystem region instead of "
                   "only the used portion (wipes residual data; larger "
                   "UF2)")
    p.add_argument("--no-extra-files", action="store_true",
                   help="skip the .fseventsd/.metadata_never_index/"
                   ".Trashes files CircuitPython normally creates")
    p.add_argument("--partition-table", metavar="PT.BIN",
                   help="ESP32: derive offset and size from a partition table "
                   "read off the chip at 0x8000")
    p.add_argument("--storage-extend", action="store_true",
                   help="ESP32: extend the filesystem into the spare OTA "
                   "partition, matching CIRCUITPY_STORAGE_EXTEND. Requires "
                   "--partition-table")
    p.add_argument("--img-out", help="also write the raw FAT image here")
    p.add_argument("--self-extract", action="store_true",
                   help="emit a single self-extracting code.py instead of an "
                   "image. Drag it onto CIRCUITPY and it unpacks itself. "
                   "Works on any port, no bootloader or esptool needed")
    p.add_argument("--list-boards", action="store_true",
                   help="list built-in boards and exit")
    args = p.parse_args(argv)

    if args.list_boards:
        for name, (fam, off, tot) in sorted(BOARDS.items()):
            print("%-28s family=0x%08x offset=0x%06x fs_size=%.1fMB"
                  % (name, fam, off, (tot - off) / 1e6))
        for name, (off, size) in sorted(ESP_PARTITIONS.items()):
            print("%-28s esptool     offset=0x%06x fs_size=%.1fMB"
                  % (name, off, size / 1e6))
        return 0

    if args.self_extract:
        from . import selfextract
        text, raw, biggest = selfextract.build(args.source)
        out = args.output or "code.py"
        with open(out, "w") as f:
            f.write(text)
        print("%s: %d files, %d bytes of source packed into %d bytes"
              % (out, text.count("\n#>"), raw, len(text)))
        print("largest single file %d bytes, which bounds RAM on the board"
              % biggest)
        print("drag onto CIRCUITPY; it unpacks and reboots")
        return 0

    if not args.source:
        p.error("source folder is required")
    if not os.path.isdir(args.source):
        p.error("source %r is not a directory" % args.source)

    esp = args.board in ESP_PARTITIONS if args.board else False
    if esp:
        if args.combine:
            p.error("--combine is UF2 only; ESP32 images are flashed with "
                    "esptool")
        offset, part_size = ESP_PARTITIONS[args.board]
        ota_size = 0
        if args.partition_table:
            offset, part_size, ota_size = parse_partition_table(
                args.partition_table)
        fs_size = part_size
        if args.storage_extend:
            if not args.partition_table:
                p.error("--storage-extend needs --partition-table, since the "
                        "spare OTA partition size varies by board")
            if not ota_size:
                p.error("this partition table has no spare OTA slot, so "
                        "storage cannot extend")
            fs_size = part_size + ota_size
        if args.fs_size is not None:
            fs_size = args.fs_size
        geo = Geometry(fs_size // SECTOR)
        root = build_tree(args.source, extra_files=not args.no_extra_files)
        fb = FatBuilder(geo, label=args.label, volume_id=args.volume_id)
        fb.layout(root)
        out = args.output or os.path.basename(
            os.path.abspath(args.source)) + ".bin"
        data = fb.image if args.full else fb.image[:fb.used_bytes()]
        with open(out, "wb") as f:
            f.write(data)
        print("%s: FAT%d, %d bytes, volume %dK at offset 0x%06x%s"
              % (out, geo.fat_type, len(data), fs_size // 1024, offset,
                 " (storage extended)" if args.storage_extend else ""))
        print("flash with: esptool --before no-reset --after no-reset "
              "write-flash 0x%06x %s" % (offset, out))
        return 0

    family = args.family_id
    offset = args.flash_offset
    fs_size = args.fs_size
    if args.board:
        bfam, boff, btot = BOARDS[args.board]
        family = family if family is not None else bfam
        offset = offset if offset is not None else boff
        if fs_size is None:
            fs_size = (args.flash_size or btot) - offset
    elif fs_size is None and args.flash_size is not None \
            and offset is not None:
        fs_size = args.flash_size - offset
    if family is None or offset is None or fs_size is None:
        p.error("need --board, or --flash-offset with --flash-size/"
                "--fs-size and --family-id")
    if fs_size <= 0 or fs_size % 4096:
        p.error("filesystem size must be positive and 4K aligned")
    if offset % 4096:
        p.error("flash offset must be 4K aligned")

    geo = Geometry(fs_size // SECTOR)
    root = build_tree(args.source, extra_files=not args.no_extra_files)
    fb = FatBuilder(geo, label=args.label, volume_id=args.volume_id)
    fb.layout(root)

    out = args.output or os.path.basename(
        os.path.abspath(args.source)) + ".uf2"
    trim = None if args.full else fb.used_bytes()
    fs_addr = args.base_addr + offset
    blocks = image_blocks(fb.image, fs_addr, trim_to=trim)

    if args.combine:
        fw_blocks, fw_family = read_uf2(args.combine)
        if fw_family is not None and fw_family != family:
            p.error("firmware family 0x%08x does not match 0x%08x; wrong "
                    "board?" % (fw_family, family))
        fw_end = max(a + len(c) for a, c in fw_blocks)
        if fw_end > fs_addr:
            p.error("firmware reaches 0x%08x, past the filesystem start "
                    "0x%08x" % (fw_end, fs_addr))
        blocks = fw_blocks + blocks
        print("combined: %d firmware blocks + %d filesystem blocks"
              % (len(fw_blocks), len(blocks) - len(fw_blocks)))
    nblocks = write_uf2_blocks(out, blocks, family)
    if args.img_out:
        with open(args.img_out, "wb") as f:
            f.write(fb.image)
    used_kb = fb.used_bytes() / 1024
    print("%s: FAT%d, %d sectors/cluster, %d clusters, %.0fKB used of "
          "%.0fKB" % (out, geo.fat_type, geo.sectors_per_cluster,
                      geo.cluster_count, used_kb, fs_size / 1024))
    lo = min(a for a, _ in blocks)
    hi = max(a + len(c) for a, c in blocks)
    print("UF2: %d blocks, flash 0x%08x..0x%08x, family 0x%08x"
          % (nblocks, lo, hi, family))
    return 0

