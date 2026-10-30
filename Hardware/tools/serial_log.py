"""Save the recorder's serial log to a file, surviving resets and power cuts.

usage: serial_log.py PORT SECONDS OUTFILE [--reset]

  PORT      e.g. COM3 (the ESP32-S3's USB-Serial/JTAG port)
  SECONDS   how long to record
  --reset   reset the chip first, so the log starts at the bootloader

Unlike `idf.py monitor` this runs unattended: it reconnects when the USB
port disappears (power cut, reset) and waits for it when it is not there
yet.  Only one program can hold the port, so close any monitor first.
Needs pyserial (ESP-IDF's Python environment has it).
"""
import sys
import time

import serial


def open_port(port):
    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.2
    s.dtr = False           # IO0 high: a reset boots the app, not the
    s.rts = False           # download mode
    s.open()
    return s


def set_rts(s, value):
    # Windows' usbser.sys only sends the line state when DTR is written,
    # so an RTS change alone never reaches the chip (esptool does this too).
    s.rts = value
    s.dtr = s.dtr


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    port, seconds, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]

    try:
        s = open_port(port)
    except (serial.SerialException, OSError):
        s = None                            # not plugged in yet
    if "--reset" in sys.argv and s:
        set_rts(s, True)                    # EN low: chip held in reset
        time.sleep(0.2)
        set_rts(s, False)
        print("reset sent", flush=True)

    end = time.time() + seconds
    with open(out, "wb") as f:
        while time.time() < end:
            try:
                if s is None:
                    s = open_port(port)
                    print("connected", flush=True)
                data = s.read(4096)
                if data:
                    f.write(data)
                    f.flush()
            except (serial.SerialException, OSError):
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
                s = None
                time.sleep(0.2)
    if s:
        s.close()


if __name__ == "__main__":
    main()
