"""Brainchild BTC-9100 controller driver (the brain inside the DPI-1100
Dry Temperature Calibrator).

Modbus RTU over RS-232 or RS-485. Documented register map and scaling
formula from the Brainchild BTC-4100/7100/8100/9100 User's Manual
(UM91001I), chapter 7.
"""

import inspect
import threading
import time

from pymodbus.client import ModbusSerialClient


# pymodbus has renamed the slave-address keyword three times across versions:
#   2.x:    unit=
#   3.0-3.7 slave=
#   3.8+:   device_id=
# Detect once at import time so the right name is used everywhere.
def _detect_slave_kwarg():
    for cand in ("device_id", "slave", "unit"):
        try:
            params = inspect.signature(
                ModbusSerialClient.read_holding_registers).parameters
            if cand in params:
                return cand
        except (ValueError, TypeError):
            pass
    return "slave"   # safe-ish fallback

_SLAVE_KW = _detect_slave_kwarg()


# Scaling for non-linear (thermocouple / RTD) input with DP = 1 - this is
# the default for the DPI-1100 (range 50-650 degC, one decimal place).
SCALE_LOW  = -1999.9
SCALE_HIGH = 4553.6
SPAN       = SCALE_HIGH - SCALE_LOW   # 6553.5


def encode(temp_c):
    """deg C -> 16-bit Modbus value per BTC-9100 conversion formula."""
    m = round((float(temp_c) - SCALE_LOW) / SPAN * 65535)
    return max(0, min(65535, int(m)))


def decode(modbus_value):
    """16-bit Modbus value -> deg C."""
    return float(modbus_value) / 65535 * SPAN + SCALE_LOW


# Register addresses (decimal) from the manual's parameter table
REG_SP1   = 0      # set point 1 - the one we write
REG_SP1L  = 9      # SP1 low limit
REG_SP1H  = 10     # SP1 high limit
REG_PV    = 64     # process value (current measured temperature)
REG_SV    = 65     # current active set point
REG_TIMER = 66     # remaining time of dwell timer
REG_EROR  = 67     # error code
REG_MODE  = 68     # operation mode + alarm status
REG_CMND  = 70     # command register
REG_JOB2  = 72     # job2 (used for Reset / AutoTune / Manual mode commands)

# Documented JOB2 command values
CMD_RESET     = 0x6825
CMD_AUTOTUNE  = 0x6828
CMD_MANUAL    = 0x6827


class FurnaceError(Exception):
    pass


class DPI1100:
    """Talk to the BTC-9100 inside a DPI-1100 calibrator."""

    def __init__(self, port, baudrate=9600, slave=1, parity="N",
                 stopbits=1, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.slave = slave
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self._client = None
        self._lock = threading.Lock()

    # ---------- connection ----------
    def open(self):
        self._client = ModbusSerialClient(
            port=self.port,
            baudrate=self.baudrate,
            parity=self.parity,
            stopbits=self.stopbits,
            bytesize=8,
            timeout=self.timeout,
        )
        if not self._client.connect():
            self._client = None
            raise FurnaceError(f"Could not open {self.port} at {self.baudrate}")

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def is_open(self):
        return self._client is not None and self._client.connected

    # ---------- low-level ----------
    def _read_register(self, address):
        if not self.is_open():
            raise FurnaceError("Furnace port not open")
        kw = {"address": address, "count": 1, _SLAVE_KW: self.slave}
        with self._lock:
            rr = self._client.read_holding_registers(**kw)
        if rr is None or rr.isError():
            raise FurnaceError(f"Read register {address} failed: {rr}")
        return rr.registers[0]

    def _write_register(self, address, value):
        if not self.is_open():
            raise FurnaceError("Furnace port not open")
        value = int(value) & 0xFFFF
        kw = {"address": address, "value": value, _SLAVE_KW: self.slave}
        with self._lock:
            rr = self._client.write_register(**kw)
        if rr is None or rr.isError():
            raise FurnaceError(f"Write register {address} failed: {rr}")

    # ---------- high-level ----------
    def read_pv(self):
        """Current measured temperature in degrees C."""
        return decode(self._read_register(REG_PV))

    def read_sv(self):
        """Currently active setpoint (may differ from SP1 during ramping)."""
        return decode(self._read_register(REG_SV))

    def read_sp1(self):
        """Read back the SP1 setpoint."""
        return decode(self._read_register(REG_SP1))

    def write_sp1(self, temp_c):
        """Change the target setpoint to temp_c."""
        self._write_register(REG_SP1, encode(temp_c))

    def read_mode(self):
        """Operation mode + alarm status word."""
        return self._read_register(REG_MODE)

    def read_error_code(self):
        return self._read_register(REG_EROR)

    def reset(self):
        """Same as pressing the R key on the front panel."""
        self._write_register(REG_JOB2, CMD_RESET)

    def autotune(self):
        self._write_register(REG_JOB2, CMD_AUTOTUNE)

    def enter_manual_mode(self):
        self._write_register(REG_JOB2, CMD_MANUAL)

    def status_snapshot(self):
        """Return (pv, sv, mode_word) in one go - useful for the GUI tick."""
        pv = self.read_pv()
        sv = self.read_sv()
        mode = self.read_mode()
        return pv, sv, mode


class FurnaceAdapter:
    """Wraps a connected DPI1100 to look like the Arduino TempReader,
    so the same Experiment runner can drive either heat source."""

    POLL_INTERVAL_S = 2.0
    SP_MIN_C = 50.0
    SP_MAX_C = 650.0

    def __init__(self, furnace):
        self.furnace = furnace
        self._latest_value = None
        self._last_target = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        """Furnace is already connected; just start the poll thread."""
        if not self.furnace.is_open():
            raise FurnaceError("Furnace serial port is not open")
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def is_open(self):
        return self.furnace is not None and self.furnace.is_open()

    def _poll(self):
        while not self._stop.wait(self.POLL_INTERVAL_S):
            try:
                pv = self.furnace.read_pv()
            except Exception:
                continue
            with self._lock:
                self._latest_value = pv

    # ----- TempReader-compatible interface used by the experiment runner -----
    def latest(self):
        with self._lock:
            return self._latest_value

    def get(self):
        with self._lock:
            return self._latest_value, time.time()

    def set_target(self, temp_c):
        t = max(self.SP_MIN_C, min(self.SP_MAX_C, float(temp_c)))
        self.furnace.write_sp1(t)
        self._last_target = t

    def set_safety_limit(self, temp_c):
        # The BTC-9100 enforces its own SP1H high-limit; nothing to do here.
        pass

    def force_heater_off(self):
        # No direct "heater off" command; bring SP1 to minimum so the
        # controller stops calling for heat.
        try:
            self.furnace.write_sp1(self.SP_MIN_C)
            self._last_target = self.SP_MIN_C
        except Exception:
            pass

    def force_heater_on(self):
        # Not meaningful for a PID controller - it controls heating itself.
        pass

    def auto_heater(self):
        pass

    def send_command(self, _cmd):
        pass

    def heater_state(self):
        # Furnace doesn't expose a simple ON/OFF; return None so the GUI
        # shows "--" instead of guessing.
        return None

    def last_target(self):
        return self._last_target

    def error(self):
        return None
