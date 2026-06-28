import re
import time
import threading
import serial


_T_LINE_RE = re.compile(r"^T:\s*([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)\s*$")
_H_LINE_RE = re.compile(r"^H:\s*([01])\s*$")

PING_INTERVAL_S = 10.0


class TempReader:
    """Background reader/writer for the PT100 + SSR Arduino sketch.

    Reads:  T:<temp>, H:<0|1>
    Sends:  SET:<target>, H:1, H:0, AUTO, PING
    """

    def __init__(self, port, baudrate=115200, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None
        self._read_thread = None
        self._ping_thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._latest_value = None
        self._latest_time = 0.0
        self._heater_on = None
        self._last_target = None
        self._error = None

    def open(self):
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )
        self._ser.reset_input_buffer()
        time.sleep(2.0)  # Arduino auto-reset on serial open
        self._stop.clear()
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self._ping_thread.start()

    def close(self):
        self._stop.set()
        try:
            self.send_command("H:0")
        except Exception:
            pass

        for t in (self._read_thread, self._ping_thread):
            if t:
                t.join(timeout=2.0)

        self._read_thread = None
        self._ping_thread = None

        if self._ser and self._ser.is_open:
            self._ser.close()
            self._ser = None

    def is_open(self):
        return self._ser is not None and self._ser.is_open

    # ---------- background loops ----------

    def _read_loop(self):

        while not self._stop.is_set():

            try:
                raw = self._ser.readline()

            except Exception as e:

                with self._lock:
                    self._error = str(e)

                return

            if not raw:
                continue

            line = raw.decode(
                "ascii",
                errors="replace"
            ).strip()

            if not line:
                continue

            m = _T_LINE_RE.match(line)

            if m:

                try:
                    value = float(m.group(1))

                except ValueError:
                    continue

                with self._lock:
                    self._latest_value = value
                    self._latest_time = time.time()

                continue

            mh = _H_LINE_RE.match(line)

            if mh:

                with self._lock:
                    self._heater_on = (
                        mh.group(1) == "1"
                    )

    def _ping_loop(self):

        while not self._stop.wait(
            PING_INTERVAL_S
        ):

            try:

                self.send_command(
                    "PING"
                )

            except Exception:

                pass

    # ---------- writes ----------

    def send_command(self, cmd):

        if not self.is_open():

            return

        line = (
            cmd.strip()
            + "\n"
        ).encode("ascii")

        with self._write_lock:

            try:

                self._ser.write(line)

                self._ser.flush()

            except Exception as e:

                with self._lock:

                    self._error = str(e)

    def set_target(self, temp_c):

        self._last_target = float(temp_c)

        self.send_command(
            f"SET:{float(temp_c):.3f}"
        )

    def set_safety_limit(self, temp_c):

        self.send_command(
            f"LIMIT:{float(temp_c):.3f}"
        )

    def force_heater_on(self):

        self.send_command("H:1")

    def force_heater_off(self):

        self.send_command("H:0")

    def auto_heater(self):

        self.send_command("AUTO")

    # ---------- reads ----------

    def get(self):

        with self._lock:

            return (
                self._latest_value,
                self._latest_time
            )

    def latest(self):

        v, _ = self.get()

        return v

    def heater_state(self):

        with self._lock:

            return self._heater_on

    def last_target(self):

        return self._last_target

    def error(self):

        with self._lock:

            return self._error