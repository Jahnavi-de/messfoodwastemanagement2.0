"""
Minimal HX711 load-cell amplifier driver for the Raspberry Pi.

Uses the RPi.GPIO API:
  * Pi 2 / 3 / 4 : sudo apt install python3-rpi.gpio
  * Pi 5         : pip install rpi-lgpio   (drop-in replacement, same API)

Wiring (BCM pin numbers, power the HX711 from 3.3 V so DOUT is 3.3 V logic):
  HX711 VCC -> Pi 3.3 V      HX711 GND -> Pi GND
  HX711 DT  -> dout_pin      HX711 SCK -> sck_pin
"""

import statistics
import time

try:
    import RPi.GPIO as GPIO  # provided by RPi.GPIO (Pi 2-4) or rpi-lgpio (Pi 5)
except ImportError:  # pragma: no cover - only on machines without GPIO
    GPIO = None

_GAIN_PULSES = {128: 1, 32: 2, 64: 3}  # channel A gain 128 is what load cells normally use
_SATURATED = (0x7FFFFF, -0x800000)


class HX711:
    def __init__(self, dout_pin: int, sck_pin: int, gain: int = 128):
        if GPIO is None:
            raise RuntimeError(
                "RPi.GPIO is not installed. Pi 2/3/4: 'sudo apt install python3-rpi.gpio'. "
                "Pi 5: 'pip install rpi-lgpio' in the project's virtualenv."
            )
        if gain not in _GAIN_PULSES:
            raise ValueError("gain must be 128, 64 or 32")
        self.dout = dout_pin
        self.sck = sck_pin
        self.extra_pulses = _GAIN_PULSES[gain]
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.sck, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.dout, GPIO.IN)
        time.sleep(0.01)
        self.read_raw()  # the first conversion only selects the gain/channel

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while GPIO.input(self.dout) == 1:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"HX711 (DT=GPIO{self.dout}, SCK=GPIO{self.sck}) not responding: check wiring and power"
                )
            time.sleep(0.001)

    def read_raw(self, timeout: float = 1.0) -> int:
        """One 24-bit two's-complement conversion."""
        self._wait_ready(timeout)
        value = 0
        for _ in range(24):
            GPIO.output(self.sck, True)
            GPIO.output(self.sck, False)
            value = (value << 1) | GPIO.input(self.dout)
        for _ in range(self.extra_pulses):
            GPIO.output(self.sck, True)
            GPIO.output(self.sck, False)
        if value & 0x800000:
            value -= 0x1000000
        return value

    def read_median(self, samples: int = 7) -> float:
        """Median of several conversions; ignores saturated/glitched values."""
        values = []
        for _ in range(samples * 2):
            v = self.read_raw()
            if v not in _SATURATED:
                values.append(v)
                if len(values) >= samples:
                    break
        if not values:
            raise RuntimeError(
                f"HX711 on GPIO{self.dout} only returned saturated values: check the load cell wires (E+, E-, A+, A-)"
            )
        return float(statistics.median(values))

    @staticmethod
    def cleanup() -> None:
        if GPIO is not None:
            GPIO.cleanup()
