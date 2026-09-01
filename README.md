# folder2uf2

Pack a folder into a CIRCUITPY FAT filesystem image wrapped in a UF2, so a
CircuitPython-based product can ship its code, libraries and assets as one
drag-and-drop file. Addresses
[adafruit/circuitpython#10074](https://github.com/adafruit/circuitpython/issues/10074)
for the rp2 port, where the bootloader can write any flash region (per
tannewt's scoping in that issue: a folder -> FAT filesystem -> UF2 converter,
orthogonal to CircuitPython itself).

Single stdlib-only Python file, no dependencies. Python 3.8+.

```
python3 folder2uf2.py my_product_files/ --board adafruit_metro_rp2350 -o product-fs.uf2
```

Flash CircuitPython firmware first (once), then the filesystem UF2. Or hand
both UF2s to the end user: BOOTSEL, drop firmware UF2, BOOTSEL again, drop
filesystem UF2. Reflashing CircuitPython later does not touch the
filesystem region, and reflashing the filesystem UF2 does not touch the
firmware.

WARNING: the filesystem UF2 REPLACES the CIRCUITPY drive contents on the
target board. That is its purpose; do not flash it on a board whose files
you want to keep.

## Usage

```
folder2uf2 SOURCE_DIR [-o OUT.uf2] --board BOARD
folder2uf2 SOURCE_DIR -o OUT.uf2 --flash-offset 0x100000 --flash-size 0x1000000 --family-id 0xe48bff59
```

| option | meaning |
|---|---|
| `--board` | built-in board profile (`--list-boards` to see all) |
| `--flash-offset` | CIRCUITPY drive start offset in flash |
| `--flash-size` / `--fs-size` | total flash size, or explicit filesystem size |
| `--family-id` | UF2 family (RP2040 `0xe48bff56`, RP2350 Arm-S `0xe48bff59`) |
| `--full` | write the whole filesystem region, not just the used part |
| `--label` | volume label, default `CIRCUITPY` |
| `--volume-id` | FAT serial number, default random |
| `--no-extra-files` | skip the `.metadata_never_index` etc. hygiene files |
| `--img-out` | also write the raw FAT image, mountable for inspection |

By default the UF2 covers only the used part of the filesystem (metadata +
allocated clusters), so it flashes fast. `--full` writes the entire region,
wiping any residual data from a previous filesystem; use it for production
images.

Built-in boards: raspberry_pi_pico, raspberry_pi_pico_w, raspberry_pi_pico2,
raspberry_pi_pico2_w, adafruit_feather_rp2040, adafruit_qtpy_rp2040,
adafruit_metro_rp2350, adafruit_fruit_jam. For anything else pass the
explicit options; the "How the offsets were derived" recipe below shows
where to look them up.

## How the offsets were derived

All values come from the CircuitPython source (verified at tags
10.3.0-alpha.4 and 10.3.0-rc.0; identical there, and per the maintainers
the location only changes on major version boundaries):

- `ports/raspberrypi/mpconfigport.h`: `CIRCUITPY_FIRMWARE_SIZE` defaults to
  1020 KiB, `CIRCUITPY_INTERNAL_NVM_SIZE` is 4 KiB, and
  `CIRCUITPY_CIRCUITPY_DRIVE_START_ADDR = FIRMWARE_SIZE + NVM_SIZE`
  (= 0x100000 for most boards). Boards with wifi override firmware size to
  1536 KiB in `mpconfigboard.mk` (drive start 0x181000).
- `ports/raspberrypi/supervisor/internal_flash.c`: the drive runs from
  that offset to the end of flash; flash size comes from the chip's RDID
  (board's `EXTERNAL_FLASH_DEVICES` tells you the fitted part).
- `supervisor/shared/flash.c`: sector 0 of the USB drive is a virtual MBR
  synthesized at read time with partition 1 at LBA 1
  (`PART1_START_BLOCK`); logical sector N maps to raw flash at
  `(N-1)*512`. So the raw flash region holds a bare FAT volume whose BPB
  records 1 hidden sector, and that is exactly what this tool emits.
- `supervisor/shared/filesystem.c` + `lib/oofatfs/ff.c`: CircuitPython
  formats with `f_mkfs(FM_FAT, au=0)` on 512-byte sectors, 1 FAT, no
  erase-block alignment (`GET_BLOCK_SIZE` is 1). The `Geometry` class here
  replicates that algorithm, so the tool produces the same FAT12/16
  geometry CircuitPython itself would create (verified against a real
  board's boot sector: identical parameters). Root label entry, `NO NAME`
  BPB label, `"FAT     "` type string, and the `.fseventsd/no_log`,
  `.metadata_never_index`, `.Trashes`, `.Trash-1000` hygiene files all
  match the firmware formatter.

UF2 target address = `0x10000000 (XIP base) + drive offset`.

RP2350 note: a data-only UF2 with the Arm-S family (0xe48bff59) flashes
fine on a stock CircuitPython board (no partition table); this is the same
family and structure CircuitPython's own firmware UF2 uses. The
`0xe48bff57` absolute family also exists for RP2350 and can be selected
with `--family-id` if needed.

ESP32 boards are out of scope: tinyuf2 only accepts writes inside the
ota_0 partition, so a filesystem UF2 cannot work there today (see the
issue thread).

## Verified on hardware

Adafruit Metro RP2350 running CircuitPython 10.3.0-rc.0:

- A sample folder (code.py + lib/package + settings.toml + 100 KB binary)
  was packed, flashed via the UF2 bootloader, and the board booted straight
  into the shipped code.py (marker strings on serial).
- Every file read back over the CircuitPython REPL matched the source
  byte-for-byte (crc32 + size), including the 100 KB blob and files in
  nested long-name directories.
- CircuitPython mounted the filesystem read-write (it wrote its own
  boot_out.txt), the OS mounted the drive normally, and a 113-file, 1.8 MB
  real-world tree (a full CIRCUITPY backup) was also packed, flashed with
  `--full`, and verified file-for-file the same way.

Local tests (`pytest tests/`) build images, parse them with an independent
FAT reader, and on macOS also mount them with hdiutil to verify contents.

## Credits

jepler's archived [mkfatimg](https://github.com/jepler/mkfatimg)
(C + oofatfs) explored the same folder-to-FAT-image direction for
CircuitPython; this is an independent pure-Python implementation of the
same idea, extended with the UF2 wrapper and board flash maps.
