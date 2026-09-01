# SPDX-License-Identifier: MIT
# Minimal independent FAT12/16 reader used to verify built images.

import struct


class FatVolume:
    def __init__(self, image):
        self.img = image
        b = image
        (self.bps,) = struct.unpack_from("<H", b, 11)
        self.spc = b[13]
        (self.reserved,) = struct.unpack_from("<H", b, 14)
        self.nfats = b[16]
        (self.root_entries,) = struct.unpack_from("<H", b, 17)
        (tot16,) = struct.unpack_from("<H", b, 19)
        (self.fat_sectors,) = struct.unpack_from("<H", b, 22)
        (tot32,) = struct.unpack_from("<I", b, 32)
        self.total_sectors = tot16 or tot32
        (self.hidden,) = struct.unpack_from("<I", b, 28)
        self.fstype = b[54:62].decode("ascii", "replace")
        assert b[510] == 0x55 and b[511] == 0xAA, "missing boot signature"
        self.root_start = self.reserved + self.nfats * self.fat_sectors
        self.rootdir_sectors = self.root_entries * 32 // self.bps
        self.data_start = self.root_start + self.rootdir_sectors
        self.cluster_count = (self.total_sectors - self.data_start) \
            // self.spc
        self.fat_type = 12 if self.cluster_count <= 0xFF5 else 16

    def fat_entry(self, n):
        off = self.reserved * self.bps
        if self.fat_type == 16:
            (v,) = struct.unpack_from("<H", self.img, off + n * 2)
            return v
        o = off + n * 3 // 2
        v = self.img[o] | (self.img[o + 1] << 8)
        return (v >> 4) if n % 2 else (v & 0xFFF)

    def chain(self, first):
        out = []
        c = first
        end = 0xFF8 if self.fat_type == 12 else 0xFFF8
        while 2 <= c < end:
            out.append(c)
            c = self.fat_entry(c)
            if len(out) > self.cluster_count:
                raise ValueError("FAT chain loop")
        return out

    def cluster_bytes(self, cluster):
        off = (self.data_start + (cluster - 2) * self.spc) * self.bps
        return self.img[off:off + self.spc * self.bps]

    def read_chain(self, first, size=None):
        data = b"".join(self.cluster_bytes(c) for c in self.chain(first))
        return data[:size] if size is not None else data

    def _parse_dir(self, raw):
        """Yield (long_name_or_None, sfn, attr, cluster, size)."""
        lfn_parts = {}
        for i in range(0, len(raw), 32):
            e = raw[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn_parts.clear()
                continue
            attr = e[11]
            if attr == 0x0F:
                seq = e[0] & 0x1F
                chars = e[1:11] + e[14:26] + e[28:32]
                lfn_parts[seq] = chars
                continue
            name = None
            if lfn_parts:
                joined = b"".join(lfn_parts[k]
                                  for k in sorted(lfn_parts))
                s = joined.decode("utf-16-le")
                name = s.split("\x00")[0]
                lfn_parts = {}
            sfn = e[0:11]
            (clus,) = struct.unpack_from("<H", e, 26)
            (size,) = struct.unpack_from("<I", e, 28)
            yield name, sfn, attr, clus, size

    def sfn_to_name(self, sfn):
        base = sfn[0:8].decode("ascii").rstrip()
        ext = sfn[8:11].decode("ascii").rstrip()
        return base + ("." + ext if ext else "")

    def walk(self):
        """Return ({path: bytes}, [dir paths], volume_label)."""
        files = {}
        dirs = []
        label = None
        root_raw = self.img[self.root_start * self.bps:
                            self.data_start * self.bps]

        def handle(raw, prefix):
            for name, sfn, attr, clus, size in self._parse_dir(raw):
                if attr & 0x08:
                    nonlocal label
                    label = self.sfn_to_name(sfn[:8] + b"   ") \
                        if False else sfn.decode("ascii").rstrip()
                    continue
                disp = name if name else self.sfn_to_name(sfn)
                if disp in (".", ".."):
                    continue
                path = prefix + disp
                if attr & 0x10:
                    dirs.append(path)
                    handle(self.read_chain(clus), path + "/")
                else:
                    files[path] = self.read_chain(clus, size) \
                        if size else b""
        handle(root_raw, "")
        return files, dirs, label


def read_uf2(path):
    """Return (start_addr, payload bytes, family_id) from a UF2 file."""
    data = open(path, "rb").read()
    assert len(data) % 512 == 0
    segs = {}
    family = None
    for i in range(0, len(data), 512):
        (m0, m1, flags, addr, sz, blk, nblk, fam) = struct.unpack_from(
            "<IIIIIIII", data, i)
        assert m0 == 0x0A324655 and m1 == 0x9E5D5157
        (mend,) = struct.unpack_from("<I", data, i + 508)
        assert mend == 0x0AB16F30
        if flags & 0x00002000:
            family = fam
        segs[addr] = data[i + 32:i + 32 + sz]
    start = min(segs)
    out = bytearray()
    addr = start
    for a in sorted(segs):
        assert a == addr, "non-contiguous UF2"
        out += segs[a]
        addr += len(segs[a])
    return start, bytes(out), family
