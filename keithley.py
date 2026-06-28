"""Keithley 2400-series source meter driver.

Supports the 2400 / 2401 / 2410 / 2420 / 2430 / 2440 family. All share
the same SCPI command set. Communication via:
  - USB-TMC                       VISA resource e.g. "USB0::0x05E6::0x2400::123::INSTR"
  - GPIB                          VISA resource e.g. "GPIB0::24::INSTR"
  - Ethernet (LXI / raw TCP-IP)   VISA resource e.g. "TCPIP0::192.168.1.50::INSTR"
  - RS-232 directly via pyserial  (no VISA needed)
"""

import threading
import time

try:
    import pyvisa
    PYVISA_OK = True
except Exception:
    PYVISA_OK = False

try:
    import serial as pyserial
    PYSERIAL_OK = True
except Exception:
    PYSERIAL_OK = False


class KeithleyError(Exception):
    pass


class Keithley2400:
    """Driver for the 2400-series source meter.

    Pass EITHER a VISA resource string (recommended for USB / GPIB / Ethernet),
    OR a serial port + baud (for RS-232 mode).
    """

    def __init__(self, resource=None, port=None, baudrate=9600,
                 timeout_s=5.0):
        if not resource and not port:
            raise KeithleyError(
                "Need either a VISA resource string or a serial port.")
        self.resource = resource
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._conn = None
        self._mode = None  # 'visa' or 'serial'
        self._lock = threading.Lock()

    # ---------- connection ----------
    def open(self):
        if self.resource:
            if not PYVISA_OK:
                raise KeithleyError(
                    "pyvisa not installed. Run: pip install pyvisa pyvisa-py")
            # Try pyvisa-py first (pure Python, no NI-VISA needed); fall
            # back to default backend if a real NI-VISA is installed.
            try:
                rm = pyvisa.ResourceManager("@py")
            except Exception:
                rm = pyvisa.ResourceManager()
            inst = rm.open_resource(self.resource)
            inst.timeout = int(self.timeout_s * 1000)
            inst.read_termination = "\n"
            inst.write_termination = "\n"
            self._conn = inst
            self._mode = "visa"
        else:
            if not PYSERIAL_OK:
                raise KeithleyError("pyserial not installed.")
            self._conn = pyserial.Serial(
                self.port, baudrate=self.baudrate,
                bytesize=8, parity="N", stopbits=1,
                timeout=self.timeout_s,
            )
            self._mode = "serial"
            time.sleep(0.3)
            self._conn.reset_input_buffer()

    def close(self):
        if not self._conn:
            return
        # Safety: turn output off on disconnect.
        try:
            self.output_off()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._mode = None

    def is_open(self):
        return self._conn is not None

    # ---------- low-level I/O ----------
    def _write(self, cmd):
        if not self.is_open():
            raise KeithleyError("Not connected")
        with self._lock:
            if self._mode == "visa":
                self._conn.write(cmd)
            else:
                self._conn.write((cmd + "\n").encode("ascii"))

    def _query(self, cmd):
        if not self.is_open():
            raise KeithleyError("Not connected")
        with self._lock:
            if self._mode == "visa":
                return self._conn.query(cmd).strip()
            else:
                self._conn.write((cmd + "\n").encode("ascii"))
                line = self._conn.readline().decode("ascii", errors="replace")
                return line.strip()

    # ---------- standard SCPI ----------
    def identify(self):
        return self._query("*IDN?")

    def reset(self):
        self._write("*RST")
        time.sleep(0.5)

    def clear(self):
        self._write("*CLS")

    # ---------- source configuration ----------
    def configure_source_voltage(self, level=0.0, compliance_a=0.1,
                                  measure_range_auto=True):
        """Set up to source voltage and measure current."""
        self._write(":SOUR:FUNC VOLT")
        self._write(":SOUR:VOLT:MODE FIXED")
        self._write(f":SOUR:VOLT:LEV {level:g}")
        self._write(":SENS:FUNC 'CURR'")
        self._write(f":SENS:CURR:PROT {compliance_a:g}")
        if measure_range_auto:
            self._write(":SENS:CURR:RANG:AUTO ON")
        self._write(":FORM:ELEM VOLT,CURR")

    def configure_source_current(self, level=0.0, compliance_v=20.0,
                                  measure_range_auto=True):
        """Set up to source current and measure voltage."""
        self._write(":SOUR:FUNC CURR")
        self._write(":SOUR:CURR:MODE FIXED")
        self._write(f":SOUR:CURR:LEV {level:g}")
        self._write(":SENS:FUNC 'VOLT'")
        self._write(f":SENS:VOLT:PROT {compliance_v:g}")
        if measure_range_auto:
            self._write(":SENS:VOLT:RANG:AUTO ON")
        self._write(":FORM:ELEM VOLT,CURR")

    def set_voltage(self, v):
        self._write(f":SOUR:VOLT:LEV {float(v):g}")

    def set_current(self, i):
        self._write(f":SOUR:CURR:LEV {float(i):g}")

    def set_compliance_current(self, a):
        self._write(f":SENS:CURR:PROT {float(a):g}")

    def set_compliance_voltage(self, v):
        self._write(f":SENS:VOLT:PROT {float(v):g}")

    # ---------- output ----------
    def output_on(self):
        self._write(":OUTP ON")

    def output_off(self):
        self._write(":OUTP OFF")

    def output_state(self):
        """Returns True/False/None for ON/OFF/unknown."""
        try:
            r = self._query(":OUTP?")
            return r.strip() in ("1", "ON")
        except Exception:
            return None

    # ---------- single measurement ----------
    def read(self):
        """Trigger a measurement and return (voltage_V, current_A)."""
        s = self._query(":READ?")
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) < 2:
            raise KeithleyError(f"Unexpected READ? reply: {s!r}")
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            raise KeithleyError(f"Could not parse READ? reply: {s!r}")

    # ---------- IV sweep ----------
    def sweep_iv(self, source="voltage", start=0.0, stop=1.0, points=51,
                 compliance=0.1, settle_s=0.05, stop_flag=None,
                 progress_fn=None):
        """Run a manual point-by-point IV sweep.

        source     : 'voltage' or 'current' - which to step
        start/stop : sweep endpoints
        points     : number of points (linear spacing)
        compliance : current limit (if sourcing V) or voltage limit (if sourcing I)
        settle_s   : delay between setting source and taking measurement
        stop_flag  : threading.Event to abort mid-sweep
        progress_fn: callable(i, total, v, i_value) for live update

        Returns list of dicts: {'V': float, 'I': float, 'R': float|None}.
        """
        if source not in ("voltage", "current"):
            raise KeithleyError("source must be 'voltage' or 'current'")
        results = []
        try:
            if source == "voltage":
                self.configure_source_voltage(start, compliance)
            else:
                self.configure_source_current(start, compliance)
            self.output_on()
            time.sleep(0.1)

            for k in range(points):
                if stop_flag is not None and stop_flag.is_set():
                    break
                if points > 1:
                    val = start + (stop - start) * k / (points - 1)
                else:
                    val = start
                if source == "voltage":
                    self.set_voltage(val)
                else:
                    self.set_current(val)
                time.sleep(settle_s)
                v, i = self.read()
                r = (v / i) if (i not in (0, 0.0)) else None
                results.append({"V": v, "I": i, "R": r})
                if progress_fn:
                    try:
                        progress_fn(k + 1, points, v, i)
                    except Exception:
                        pass
        finally:
            try:
                self.output_off()
            except Exception:
                pass
        return results

    # ---------- listing VISA resources (helper for the GUI) ----------
    @staticmethod
    def list_visa_resources():
        """Return a list of VISA resource strings the OS can see."""
        if not PYVISA_OK:
            return []
        try:
            rm = pyvisa.ResourceManager("@py")
        except Exception:
            try:
                rm = pyvisa.ResourceManager()
            except Exception:
                return []
        try:
            return list(rm.list_resources())
        except Exception:
            return []
