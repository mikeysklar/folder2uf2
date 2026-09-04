# folder2uf2

## What it does

Ships a CircuitPython project as one file, so customers copy once.

## Install

```sh
pip install folder2uf2
```

Python 3.8 or newer, standard library only. No dependencies.

## Three ways to ship

| Output | Goes onto | Board must be |
|---|---|---|
| `code.py` | `CIRCUITPY` | running CircuitPython |
| `.uf2` | `RPI-RP2`, `RP2350` | in the UF2 bootloader |
| `.bin` | flashed by esptool | ESP32, any state |

## Self-extract: one file, no bootloader

Pack the project. Output must be `code.py`, the name CircuitPython runs.

```sh
folder2uf2 --self-extract -o code.py myproject/
```

### What your customer does

```
1. drag code.py onto CIRCUITPY
2. CIRCUITPY disappears for about 4 seconds
3. board reboots with code.py, lib/ and assets in place
```

### Ports

Any board with `zlib` and `binascii`. Firmware does the writing.

| Chip | Tested on | Self-extract |
|---|---|---|
| RP2040 | Metro RP2040 | yes |
| RP2350 | Metro RP2350 | yes |
| ESP32-S2 | Metro ESP32-S2 | yes |
| ESP32-S3 | Metro ESP32-S3 | yes |
| SAMD51 | Metro M4 AirLift | yes |
| nRF52840 | Feather nRF52840 | yes |
| STM32F405 | Feather STM32F405 | yes |
| SAMD21 | Metro M0 Express | no, lacks `zlib` |

Every row was run on hardware, not inferred.

SAMD, nRF and STM keep the filesystem on external QSPI. That changes nothing.

`.uf2` and `.bin` output stay RP2 and ESP32 only.

### If it fails

The drive is handed back with an error. Nobody gets locked out.

### How it works

`storage.unsafe_disable_usb_drive()` makes CIRCUITPY writable from `code.py`.

```python
# payload rides in trailing comments, inflated one file at a time
# peak RAM is the largest single file, not the whole tree
# remount() cannot: MSC holds the block device lock while mounted
```

### Limits

Needs CircuitPython installed already. CIRCUITPY vanishes while it runs.

```
zlib required: default wherever CIRCUITPY_FULL_BUILD is set
60KB incompressible single file verified on a 264KB RP2040
```

## UF2, for RP2 boards

```sh
# filesystem only
folder2uf2 --board adafruit_metro_rp2350 -o fs.uf2 myproject/

# firmware and filesystem together
folder2uf2 --board adafruit_metro_rp2350 \
           --combine firmware.uf2 -o product.uf2 myproject/

# wipe residual data rather than writing only used sectors
folder2uf2 --board adafruit_metro_rp2350 --full -o fs.uf2 myproject/

# a board with no built-in profile
folder2uf2 --flash-offset 0x100000 --flash-size 0x1000000 \
           --family-id 0xe48bff59 -o fs.uf2 myproject/

folder2uf2 --list-boards
```

## ESP32

tinyuf2 writes only `ota_0`, so ESP32 gets a raw image for esptool.

```sh
folder2uf2 --board esp32_16mb -o fs.bin myproject/
esptool --before no-reset --after no-reset write-flash 0x450000 fs.bin
```

### Storage extend

Layouts differ, so read the table off your chip.

```sh
esptool --before no-reset --after no-reset read-flash 0x8000 0xc00 pt.bin
folder2uf2 --board esp32_16mb --partition-table pt.bin --storage-extend -o fs.bin src/
```

```c
storage_extended = (_partition[0]->size < fatfs_bytes());
/* S3 16MB has that slot: 28032 sectors. S2 4MB does not: 1920. */
```

### Download mode

One-shot: one esptool run per entry, then power cycle.

### Bootloader

Install the tinyuf2 bootloader first, per your board's learn guide.

### Browser instead of esptool

Takes a file and an offset, so customers need no Python.

<https://adafruit.github.io/Adafruit_WebSerial_ESPTool/>

## How it was tested

### Self-extract, three boards

RP2040 is the tight one.

```
Adafruit CircuitPython 10.3.0 on 2026-08-31; Adafruit Metro RP2040 with rp2040
Adafruit CircuitPython 10.3.0 on 2026-08-31; Adafruit Metro RP2350 with rp2350b
Adafruit CircuitPython 10.3.0 on 2026-08-31; Adafruit Metro ESP32S3 with ESP32S3

60144 bytes of source packed into 84356 bytes
assets.bin 60000 bytes incompressible, md5 matched
lib/mypkg/__init__.py created, nested dir
code.py replaced by the product, printed its marker
```

Drive returned unaided. All boards restored, identical.

### What the RP2040 taught

Two MemoryErrors before it fit, both invisible on roomier chips.

```
"".join(chunks)        -> allocating 80037 bytes, failed
growing a bytearray    -> allocating 49680 bytes, failed
preallocate + memoryview -> fits
```

### Combine, Metro RP2350

Before, then after one UF2 carrying the 10.3.0 release.

```
Adafruit CircuitPython 10.3.0-alpha.4 on 2026-07-23; Adafruit Metro RP2350 with rp2350b

Adafruit CircuitPython 10.3.0 on 2026-08-31; Adafruit Metro RP2350 with rp2350b
code.py output:
FOLDER2UF2-COMBINE-OK marker 8f3a21
lib import works

restored by a second combined UF2, 108 files identical
```

### ESP32 image, Metro ESP32-S3

Partition table read from the chip, not assumed.

```
ffat  data fat  0x450000  0xBB0000  11968K
wrote 43520 bytes, hash verified
volume 28032 sectors, files intact
```

## Safety

Firmware is untouched unless you pass `--combine`.

```
--self-extract writes only your files, leaving others alone
an ESP32 image sized wrong overflows, so read the table
```

## Credit

Idea from jepler's archived mkfatimg. Thanks @todbot for the requests.

https://github.com/adafruit/circuitpython/issues/10074

## License

MIT
