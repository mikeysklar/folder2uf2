# SPDX-License-Identifier: MIT
import hashlib
import os
import plistlib
import shutil
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

import folder2uf2 as f2  # noqa: E402
from fatreader import FatVolume, read_uf2  # noqa: E402


@pytest.fixture
def sample(tmp_path):
    src = tmp_path / "src"
    (src / "lib" / "helpers").mkdir(parents=True)
    (src / "code.py").write_text('print("MARKER")\n')
    (src / "settings.toml").write_text('X="y"\n')
    (src / "lib" / "helpers" / "mod_with_a_long_name.py").write_text(
        "VALUE = 42\n")
    data = bytes(range(256)) * 400  # 100KB deterministic
    (src / "data.bin").write_bytes(data)
    return src


def build(src, tmp_path, size=15 * 1024 * 1024, **kw):
    out = tmp_path / "out.uf2"
    img = tmp_path / "out.img"
    argv = [str(src), "-o", str(out), "--img-out", str(img),
            "--flash-offset", "0x100000", "--fs-size", hex(size),
            "--family-id", "0xe48bff59", "--volume-id", "0x12345678"]
    for k, v in kw.items():
        argv.append("--" + k.replace("_", "-"))
        if v is not True:
            argv.append(str(v))
    assert f2.main(argv) == 0
    return out, img


def test_geometry_metro_rp2350():
    # 15MiB volume: expect FAT16, 4 sectors/cluster, 31-sector FAT,
    # 512 root entries, 7664 clusters (hand-computed from oofatfs f_mkfs)
    g = f2.Geometry(30720)
    assert g.fat_type == 16
    assert g.sectors_per_cluster == 4
    assert g.fat_sectors == 31
    assert g.root_entries == 512
    assert g.cluster_count == 7664
    assert g.data_start == 64


def test_geometry_small_is_fat12():
    g = f2.Geometry(2048)  # 1MiB volume (raspberry_pi_pico)
    assert g.fat_type == 12


def test_roundtrip(sample, tmp_path):
    out, img = build(sample, tmp_path)
    vol = FatVolume(img.read_bytes())
    assert vol.fat_type == 16
    assert vol.hidden == 1
    files, dirs, label = vol.walk()
    assert label.startswith("CIRCUITPY")
    assert files["code.py"] == b'print("MARKER")\n'
    assert files["settings.toml"] == b'X="y"\n'
    assert files["lib/helpers/mod_with_a_long_name.py"] == b"VALUE = 42\n"
    assert hashlib.sha256(files["data.bin"]).hexdigest() == \
        hashlib.sha256(bytes(range(256)) * 400).hexdigest()
    # CircuitPython-formatter hygiene files present by default
    assert files[".metadata_never_index"] == b""
    assert files[".fseventsd/no_log"] == b""
    assert "lib" in dirs and "lib/helpers" in dirs


def test_uf2_addresses(sample, tmp_path):
    out, img = build(sample, tmp_path)
    start, payload, family = read_uf2(str(out))
    assert start == 0x10100000
    assert family == 0xE48BFF59
    assert len(payload) % 4096 == 0
    # payload must be a prefix of the raw image (padding is 0xff over
    # zeroed image tail, so compare only up to the trim point)
    raw = img.read_bytes()
    trimmed = payload.rstrip(b"\xff")
    assert raw[:len(trimmed)] == trimmed


def test_full_writes_whole_region(sample, tmp_path):
    out, img = build(sample, tmp_path, size=2 * 1024 * 1024, full=True)
    start, payload, family = read_uf2(str(out))
    assert len(payload) == 2 * 1024 * 1024


def test_no_extra_files(sample, tmp_path):
    out, img = build(sample, tmp_path, no_extra_files=True)
    files, dirs, label = FatVolume(img.read_bytes()).walk()
    assert ".metadata_never_index" not in files


def test_user_file_overrides_extra(sample, tmp_path):
    (sample / ".metadata_never_index").write_text("x")
    out, img = build(sample, tmp_path)
    files, _, _ = FatVolume(img.read_bytes()).walk()
    assert files[".metadata_never_index"] == b"x"


def test_content_too_big(sample, tmp_path):
    (sample / "big.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    with pytest.raises(ValueError):
        build(sample, tmp_path, size=1024 * 1024)


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("hdiutil"),
                    reason="hdiutil (macOS) required")
def test_mount_with_hdiutil(sample, tmp_path):
    _, img = build(sample, tmp_path)
    res = subprocess.run(
        ["hdiutil", "attach", "-imagekey",
         "diskimage-class=CRawDiskImage", "-nobrowse", "-plist", str(img)],
        capture_output=True, check=True)
    plist = plistlib.loads(res.stdout)
    mounts = [e for e in plist["system-entities"]
              if e.get("mount-point")]
    assert mounts, "image did not mount"
    mp = mounts[0]["mount-point"]
    dev = plist["system-entities"][0]["dev-entry"]
    try:
        assert open(os.path.join(mp, "code.py")).read() == \
            'print("MARKER")\n'
        got = open(os.path.join(
            mp, "lib", "helpers", "mod_with_a_long_name.py")).read()
        assert got == "VALUE = 42\n"
        h = hashlib.sha256(
            open(os.path.join(mp, "data.bin"), "rb").read()).hexdigest()
        assert h == hashlib.sha256(bytes(range(256)) * 400).hexdigest()
    finally:
        subprocess.run(["hdiutil", "detach", dev], capture_output=True)


def test_combine_prepends_firmware_and_renumbers(tmp_path):
    """--combine emits one UF2 whose blocks are numbered across both regions."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "code.py").write_text("print('hi')\n")

    # a stand-in firmware UF2 at the start of flash
    fw = tmp_path / "fw.uf2"
    f2.write_uf2(str(fw), b"\xa5" * 8192, f2.XIP_BASE, f2.FAMILY_RP2040)

    out = tmp_path / "combined.uf2"
    assert f2.main(["--board", "adafruit_feather_rp2040", "--combine",
                    str(fw), "-o", str(out), str(src)]) == 0

    blocks, family = f2.read_uf2(str(out))
    assert family == f2.FAMILY_RP2040
    raw = out.read_bytes()
    total = len(raw) // 512
    for i in range(total):
        _, _, _, _, _, no, tot, _ = struct.unpack("<IIIIIIII",
                                                  raw[i * 512:i * 512 + 32])
        assert no == i and tot == total
    # firmware first, filesystem after, no overlap
    fw_end = f2.XIP_BASE + 8192
    assert blocks[0][0] == f2.XIP_BASE
    assert any(a == f2.XIP_BASE + 0x100000 for a, _ in blocks)
    assert all(a >= fw_end for a, _ in blocks if a >= f2.XIP_BASE + 0x100000)


def test_combine_rejects_wrong_family(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "code.py").write_text("x\n")
    fw = tmp_path / "fw.uf2"
    f2.write_uf2(str(fw), b"\x00" * 4096, f2.XIP_BASE, f2.FAMILY_RP2350_ARM_S)
    with pytest.raises(SystemExit):
        f2.main(["--board", "adafruit_feather_rp2040", "--combine", str(fw),
                 "-o", str(tmp_path / "o.uf2"), str(src)])


def test_esp32_writes_raw_bin_not_uf2(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "code.py").write_text("print('esp')\n")
    out = tmp_path / "fs.bin"
    assert f2.main(["--board", "esp32_4mb", "-o", str(out), str(src)]) == 0
    data = out.read_bytes()
    assert data[:4] != struct.pack("<I", f2.UF2_MAGIC0)
    # a bare FAT volume: BPB hidden-sectors is 1, matching the rp2 layout
    assert struct.unpack("<I", data[0x1C:0x20])[0] == 1


def test_partition_table_from_real_hardware():
    """Dumps read off a Metro ESP32-S3 and a Metro ESP32-S2 at 0x8000."""
    here = os.path.dirname(__file__)

    off, size, ota = f2.parse_partition_table(
        os.path.join(here, "pt_esp32s3_16mb.bin"))
    assert (off, size, ota) == (0x450000, 11968 * 1024, 2048 * 1024)
    # the board reported 28032 sectors, i.e. fat + spare ota
    assert (size + ota) // 512 == 28032

    off, size, ota = f2.parse_partition_table(
        os.path.join(here, "pt_esp32s2_4mb.bin"))
    assert (off, size, ota) == (0x310000, 960 * 1024, 0)
    # no spare OTA slot, so no extension; the board reported 1920 sectors
    assert size // 512 == 1920


def test_storage_extend_requires_a_partition_table(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "code.py").write_text("x\n")
    with pytest.raises(SystemExit):
        f2.main(["--board", "esp32_16mb", "--storage-extend",
                 "-o", str(tmp_path / "fs.bin"), str(src)])
