# folder2uf2

## What it does

Packs a folder into a CIRCUITPY filesystem image you can flash.

## Requirements

Python 3, standard library only. No dependencies.

## Usage

```sh
# filesystem only, drag and drop onto the bootloader drive
folder2uf2 --board adafruit_metro_rp2350 -o fs.uf2 myproject/

# firmware and filesystem as one file for customers
folder2uf2 --board adafruit_metro_rp2350 \
           --combine firmware.uf2 -o product.uf2 myproject/

# any board, without a built-in profile
folder2uf2 --flash-offset 0x100000 --flash-size 0x1000000 \
           --family-id 0xe48bff59 -o fs.uf2 myproject/

folder2uf2 --list-boards
```

## ESP32

tinyuf2 writes only `ota_0`, so ESP32 gets a raw image for esptool.

```sh
folder2uf2 --board esp32_4mb -o fs.bin myproject/
esptool write-flash 0x310000 fs.bin
```

## How it was tested

### Metro RP2350, before

```
Adafruit CircuitPython 10.3.0 on 2026-08-31; Adafruit Metro RP2350 with rp2350b
Board ID:adafruit_metro_rp2350
```

### After one combined UF2

Firmware and filesystem both changed.

```
Adafruit CircuitPython 10.3.0-alpha.4 on 2026-07-23; Adafruit Metro RP2350 with rp2350b
Board ID:adafruit_metro_rp2350

code.py output:
FOLDER2UF2-COMBINE-OK marker 8f3a21
lib import works
```

### Restore

A second combined UF2. 108 files verified identical.

### Metro ESP32-S3, not verified yet

```
Adafruit CircuitPython 10.3.0 on 2026-08-31; Adafruit Metro ESP32S3 with ESP32S3
Board ID:adafruit_metro_esp32s3
```

Reaches ROM download mode. esptool over USB-Serial-JTAG stayed unreliable.

## Safety

Writes only the CIRCUITPY region. Firmware is untouched without `--combine`.

## Credit

Idea from jepler's archived mkfatimg. Thanks @todbot for the requests.

https://github.com/adafruit/circuitpython/issues/10074

## License

MIT
