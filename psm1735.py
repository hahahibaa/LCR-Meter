import time
import threading
import serial


class PSMError(Exception):
    pass


class PSM1735:
    """Wraps the N4L PSM1735 ASCII protocol over USB-serial.

    Commands are CRLF-terminated, fields comma-separated, multiple commands
    semicolon-separated. Replies are uppercase, comma-delimited.
    """

    def __init__(self, port, baudrate=19200, timeout=2.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None
        self._lock = threading.Lock()

    def open(self):
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            rtscts=True,
            dsrdtr=False,
        )
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                self.output_off()
            except Exception:
                pass
            self._ser.close()
            self._ser = None

    def is_open(self):
        return self._ser is not None and self._ser.is_open

    def _write(self, cmd):
        if not self.is_open():
            raise PSMError("Port not open")
        line = (cmd.strip() + "\r").encode("ascii")
        self._ser.write(line)
        self._ser.flush()

    def _read_line(self):
        raw = self._ser.readline()
        if not raw:
            return ""
        return raw.decode("ascii", errors="replace").strip()

    def send(self, cmd):
        with self._lock:
            self._write(cmd)

    def query(self, cmd):
        with self._lock:
            self._write(cmd)
            return self._read_line()

    def query_many(self, cmd, expected_lines=None, line_timeout=10.0):
        """Read multiple response lines until silence or expected count."""
        with self._lock:
            self._write(cmd)
            lines = []
            old_to = self._ser.timeout
            self._ser.timeout = line_timeout
            try:
                while True:
                    line = self._read_line()
                    if not line:
                        break
                    lines.append(line)
                    if expected_lines and len(lines) >= expected_lines:
                        break
            finally:
                self._ser.timeout = old_to
            return lines

    # --- identification ---
    def idn(self):
        return self.query("*IDN?")

    def reset(self):
        self.send("*RST")
        time.sleep(0.5)

    # --- mode and LCR config ---
    def set_mode(self, mode):
        # mode: SIGGEN | VRMS | GAINPH | VECTOR | POWER | LCR | HARMON | TXA
        self.send(f"MODE,{mode.upper()}")

    def set_mode_lcr(self):
        self.send("MODE,LCR")

    def set_lcr_conditions_full(self, conditions):
        """Map the app's 3-state conditions selector to CONFIG 22.

        CONFIG 22:
          0 = Auto frequency
          1 = Manual
          2 = Auto shunt
        """
        mapping = {"AUTO_FREQ": 0, "MANUAL": 1, "AUTO_SHUNT": 2}
        v = mapping.get(conditions.upper(), 1)
        self.send(f"CONFIG,22,{v}")

    def set_lcr_shunt_mode(self, shunt_mode):
        # CONFIG 23: 0 = Default, 1 = Manual
        mapping = {"DEFAULT": 0, "AUTO": 0, "MANUAL": 1}
        v = mapping.get(shunt_mode.upper(), 0)
        self.send(f"CONFIG,23,{v}")

    def set_lcr_connection(self, connection):
        # CONFIG 145: 0 = Shunt, 1 = Divider Zx low, 2 = Divider Zx high
        mapping = {"SHUNT": 0, "DIVIDER_LOW": 1, "DIVIDER_HIGH": 2}
        v = mapping.get(connection.upper(), 0)
        self.send(f"CONFIG,145,{v}")

    def set_lcr_graph(self, graph):
        # CONFIG 139: 0 = Single, 1 = Tan delta / QF, 2 = Resistance
        mapping = {"SINGLE": 0, "TAND_QF": 1, "RESISTANCE": 2}
        v = mapping.get(graph.upper(), 1)
        self.send(f"CONFIG,139,{v}")

    def set_lcr(self, conditions="MANUAL", parameter="AUTO", head="NORMAL"):
        # conditions: AUTO | MANUAL
        # parameter: AUTO | CAPACITANCE | INDUCTANCE | IMPEDANCE | ADMITTANCE
        # head: NONE | LOW | NORMAL | HIGH | VHIGH
        self.send(f"LCR,{conditions},{parameter},{head}")

    def set_lcr_sweep_parallel(self, parallel=True):
        # CONFIG index 138: 0=series, 1=parallel
        self.send(f"CONFIG,138,{1 if parallel else 0}")

    def set_lcr_graph_tand(self):
        # CONFIG index 139: 1 = Tan delta / QF
        self.send("CONFIG,139,1")

    # --- channels ---
    def set_input(self, channel, kind):
        # kind: DISABLE | VOLTAGE | SHUNT
        self.send(f"INPUT,{channel},{kind}")

    def set_input_connection(self, channel, conn):
        # conn: MAIN | SECOND | DIFFER
        self.send(f"INTYPE,{channel},{conn}")

    def set_range(self, channel, ranging, min_range):
        # ranging: AUTO | UPAUTO | MANUAL
        # min_range example: "10mV"
        self.send(f"RANGE,{channel},{ranging},{min_range}")

    def set_coupling(self, channel, coupling):
        # coupling: AC+DC | ACONLY
        self.send(f"COUPLI,{channel},{coupling}")

    def set_scale(self, channel, factor):
        self.send(f"SCALE,{channel},{factor}")

    def set_shunt(self, channel, ohms):
        self.send(f"SHUNT,{channel},{ohms}")

    # --- AUX ---
    def set_aux_fixture(self, fixture):
        # Fixture values per manual CONFIG index 122:
        # 0=None, 1=LCR active head, 2=TAF01, 3=TAF02, 4=Impedance analyser interface (IAI)
        mapping = {
            "NONE": 0,
            "ACTIVE_HEAD": 1,
            "TAF01": 2,
            "TAF02": 3,
            "IAI": 4,
        }
        v = mapping.get(fixture.upper(), 4)
        self.send(f"CONFIG,122,{v}")

    def set_aux_lcr_head_shunt(self, level):
        # CONFIG index 140: 0=Low, 1=Normal, 2=High, 3=Very high
        mapping = {"LOW": 0, "NORMAL": 1, "HIGH": 2, "VHIGH": 3, "VERY HIGH": 3}
        v = mapping.get(level.upper(), 1)
        self.send(f"CONFIG,140,{v}")

    # --- generator ---
    def set_frequency(self, hz):
        self.send(f"FREQUE,{hz:g}")

    def set_amplitude(self, vpeak):
        self.send(f"AMPLIT,{vpeak:g}")

    def set_offset(self, volts):
        self.send(f"OFFSET,{volts:g}")

    def set_waveform_sine(self):
        self.send("WAVEFO,SINEWA")

    def set_waveform(self, waveform):
        # SINEWA | TRIANG | SQUARE | LEADIN | TRAILI
        self.send(f"WAVEFO,{waveform.upper()}")

    def set_frequency_step(self, value):
        self.send(f"CONFIG,52,{value:g}")

    def set_amplitude_step(self, value):
        self.send(f"CONFIG,53,{value:g}")

    def set_ceiling_dbm(self, dbm):
        # CONFIG 54: Amplitude dBm value (used when control mode is dBm)
        self.send(f"CONFIG,54,{dbm:g}")

    # --- ACQU (acquisition control) ---
    def set_speed(self, mode, window_s=None):
        # mode: FAST | MEDIUM | SLOW | VSLOW | WINDOW
        m = mode.upper()
        if m == "WINDOW" and window_s is not None:
            self.send(f"SPEED,WINDOW,{window_s:g}")
        else:
            self.send(f"SPEED,{m}")

    def set_cycles(self, n):
        self.send(f"CYCLES,{int(n)}")

    def set_delay(self, seconds):
        self.send(f"DELAY,{float(seconds):g}")

    def set_phase_ref(self, channel):
        # CH1 = phase of ch2 relative to ch1; CH2 = phase of ch1 relative to ch2
        self.send(f"PHREF,{channel.upper()}")

    def set_filter(self, filter_type, dynamics=None):
        # filter_type: NONE | NORMAL | SLOW
        # dynamics: AUTO | FIXED
        if dynamics:
            self.send(f"FILTER,{filter_type.upper()},{dynamics.upper()}")
        else:
            self.send(f"FILTER,{filter_type.upper()}")

    def set_low_frequency(self, on):
        self.send(f"LOWFRE,{'ON' if on else 'OFF'}")

    def set_datalog(self, function, interval_s=None):
        # function: DISABLE | RAM | NONVOL | RECALL | DELETE
        if interval_s is not None and function.upper() in ("RAM", "NONVOL"):
            self.send(f"DATALO,{function.upper()},{float(interval_s):g}")
        else:
            self.send(f"DATALO,{function.upper()}")

    def set_bandwidth(self, mode):
        # AUTO | WIDE | LOW
        self.send(f"BANDWI,{mode.upper()}")

    # --- TRIM ---
    def set_ac_trim(self, channel, level, tolerance):
        # channel: DISABL | CH1 | CH2 ; level in V or dBm ; tolerance in %
        self.send(f"ACTRIM,{channel.upper()},{float(level):g},{float(tolerance):g}")

    # --- SYS / system options ---
    def set_phase_convention(self, convention):
        # convention: 180 (-180..+180), -360 (0..-360), +360 (0..+360)
        mapping = {"180": 180, "-360": -360, "+360": 360,
                   "+/-180": 180, "+-180": 180}
        v = mapping.get(str(convention).upper(), 180)
        self.send(f"PHCONV,{v}")

    def set_length_unit(self, unit):
        # CONFIG 119: 0=Metres, 1=Inch
        mapping = {"M": 0, "METRES": 0, "METERS": 0, "INCH": 1, "IN": 1}
        v = mapping.get(unit.upper(), 0)
        self.send(f"CONFIG,119,{v}")

    def set_low_blanking(self, on, threshold=None):
        if on and threshold is not None:
            self.send(f"BLANKI,ON,{float(threshold):g}")
        else:
            self.send(f"BLANKI,{'ON' if on else 'OFF'}")

    def set_graph_style(self, style):
        # CONFIG 8: 0=Dots, 1=Lines
        mapping = {"DOTS": 0, "LINES": 1}
        v = mapping.get(style.upper(), 1)
        self.send(f"CONFIG,8,{v}")

    def set_step_message(self, enabled):
        # CONFIG 117: 0=Enabled, 1=Disabled (yes, inverted in the manual)
        v = 0 if enabled else 1
        self.send(f"CONFIG,117,{v}")

    def set_prog_direct(self, enabled):
        # CONFIG 66: 0=Disabled, 1=Enabled
        v = 1 if enabled else 0
        self.send(f"CONFIG,66,{v}")

    def set_control_mode(self, mode):
        # CONFIG 116: 0=Volts, 1=dBm
        mapping = {"V": 0, "VOLTS": 0, "VOLT": 0, "DBM": 1}
        v = mapping.get(mode.upper(), 0)
        self.send(f"CONFIG,116,{v}")

    def set_keyboard_beep(self, on):
        # CONFIG 9: 0 = Off, 1 = On
        self.send(f"CONFIG,9,{1 if on else 0}")

    def set_autozero(self, auto):
        # CONFIG 4: 0 = Auto, 1 = Manual
        self.send(f"CONFIG,4,{0 if auto else 1}")

    # --- Sweep extras (CONFIG-based) ---
    def set_sweep_type(self, sweep_type):
        # CONFIG 21: 0 = Single, 1 = Repeat
        mapping = {"SINGLE": 0, "REPEAT": 1}
        v = mapping.get(sweep_type.upper(), 0)
        self.send(f"CONFIG,21,{v}")

    def set_gen_after_sweep(self, on):
        # CONFIG 55: 0 = Off, 1 = On
        self.send(f"CONFIG,55,{1 if on else 0}")

    def set_graph1_scaling(self, auto):
        # CONFIG 193: 0 = Auto, 1 = Manual
        self.send(f"CONFIG,193,{0 if auto else 1}")

    def set_graph2_scaling(self, auto):
        # CONFIG 173: 0 = Auto, 1 = Manual
        self.send(f"CONFIG,173,{0 if auto else 1}")

    def set_frequency_marker(self, on, frequency_hz=None):
        # CONFIG 64: 0 = Off, 1 = On  (MARKER command also exists)
        if on and frequency_hz is not None:
            self.send(f"MARKER,ON,{frequency_hz:g}")
        else:
            self.send(f"MARKER,{'ON' if on else 'OFF'}")

    def output_on(self, gen_when_complete=False):
        """Send the simplest possible OUTPUT,ON command.

        Some PSM firmware revisions silently ignore OUTPUT,ON when the
        second 'sweep behaviour' argument is supplied, so we always send
        the bare 'OUTPUT,ON' first and follow up only if the caller asked
        for the auto-off-after-sweep behaviour.
        """
        self.send("OUTPUT,ON")
        if not gen_when_complete:
            # Tell the PSM to switch the generator off when the sweep ends.
            self.send("OUTPUT,ON,OFF")

    def output_off(self):
        self.send("OUTPUT,OFF")

    def gen_active(self):
        """Return True if the PSM reports the generator is currently on."""
        reply = self.query("RUN?")
        try:
            return (int(reply.strip()) & 0x80) != 0
        except ValueError:
            return None

    # --- system ---
    def set_phase_convention_180(self):
        self.send("PHCONV,180")

    def set_resolution_high(self):
        self.send("RESOLU,HIGH")

    # --- sweep ---
    def set_sweep(self, steps, start_hz, end_hz, log=True):
        scale = "LOGARI" if log else "LINEAR"
        self.send(f"FSWEEP,{int(steps)},{start_hz:g},{end_hz:g},{scale}")

    def start_sweep(self):
        self.send("START")

    def abort(self):
        self.send("ABORT")

    def opc(self):
        reply = self.query("*OPC?")
        return reply.strip() == "1"

    def wait_sweep_complete(self, timeout_s=120.0, poll_s=0.5):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.opc():
                return True
            time.sleep(poll_s)
        return False

    def read_lcr_sweep(self):
        """Returns list of dicts: freq, Q, TanD, Z, phase, L, C, R."""
        lines = self.query_many("LCR,SWEEP?", line_timeout=5.0)
        rows = []
        for line in lines:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) < 8:
                continue
            try:
                vals = [float(p) for p in parts[:8]]
            except ValueError:
                continue
            rows.append({
                "Frequency_Hz": vals[0],
                "Q": vals[1],
                "TanD": vals[2],
                "Impedance_Ohm": vals[3],
                "Phase_deg": vals[4],
                "L_H": vals[5],
                "C_F": vals[6],
                "R_Ohm": vals[7],
            })
        return rows

    # --- one-shot configure helper used by the GUI ---
    def apply_config(self, cfg):
        """Apply a configuration dict in the correct order."""
        # ----- Top-level mode -----
        self.set_mode(cfg.get("operation_mode", "LCR"))

        # ----- LCR meter setup -----
        # The LCR command itself only accepts AUTO / MANUAL for the conditions
        # argument; the three-way Auto-freq / Manual / Auto-shunt choice is
        # exposed separately via CONFIG 22 below.
        cond = cfg.get("lcr_conditions", "MANUAL").upper()
        lcr_cmd_cond = "AUTO" if cond == "AUTO_FREQ" else "MANUAL"
        self.set_lcr(lcr_cmd_cond, cfg["lcr_parameter"], cfg["lcr_head"])

        # Three-way Condition (CONFIG 22)
        self.set_lcr_conditions_full(cond)
        # CONFIG 23 Shunt is now driven by SYS panel only (set further down).
        # Front-panel connection (CONFIG 145)
        self.set_lcr_connection(cfg.get("lcr_connection", "SHUNT"))
        # Series / Parallel equivalent (CONFIG 138)
        self.set_lcr_sweep_parallel(cfg["lcr_sweep_parallel"])
        # Secondary graph trace (CONFIG 139)
        self.set_lcr_graph(cfg.get("lcr_graph", "TAND_QF"))

        self.set_phase_convention_180()
        self.set_resolution_high()

        self.set_input("CH1", cfg["ch1_input"])
        self.set_input("CH2", cfg["ch2_input"])
        self.set_input_connection("CH1", cfg["ch1_connection"])
        self.set_input_connection("CH2", cfg["ch2_connection"])
        self.set_range("CH1", cfg["ch1_ranging"], cfg["ch1_min_range"])
        self.set_range("CH2", cfg["ch2_ranging"], cfg["ch2_min_range"])
        self.set_coupling("CH1", cfg["ch1_coupling"])
        self.set_coupling("CH2", cfg["ch2_coupling"])
        self.set_scale("CH1", cfg["ch1_scale"])
        self.set_scale("CH2", cfg["ch2_scale"])
        self.set_shunt("CH2", cfg["ch2_shunt_ohms"])

        self.set_aux_fixture(cfg["aux_fixture"])
        self.set_aux_lcr_head_shunt(cfg["aux_lcr_head_shunt"])

        # ----- ACQU -----
        self.set_speed(cfg.get("acqu_speed", "MEDIUM"),
                       window_s=cfg.get("acqu_speed_window", 0.1))
        self.set_cycles(cfg.get("acqu_min_cycles", 1))
        self.set_delay(cfg.get("acqu_delay_s", 0))
        self.set_phase_ref(cfg.get("acqu_phase_ref", "CH1"))
        self.set_filter(cfg.get("acqu_filter", "NORMAL"),
                        cfg.get("acqu_filter_dynamics", "AUTO"))
        self.set_low_frequency(cfg.get("acqu_low_freq", False))
        self.set_bandwidth(cfg.get("acqu_bandwidth", "AUTO"))
        # datalog: only push if explicitly enabled
        dl = cfg.get("acqu_datalog", "DISABLE")
        if dl.upper() != "DISABLE":
            self.set_datalog(dl, cfg.get("acqu_datalog_interval_s", 1))
        else:
            self.set_datalog("DISABLE")

        # ----- TRIM -----
        trim_ch = cfg.get("trim_channel", "DISABL")
        if trim_ch.upper() != "DISABL":
            self.set_ac_trim(trim_ch,
                             cfg.get("trim_level", 1.0),
                             cfg.get("trim_tolerance_pct", 5))

        # ----- SYS -----
        self.set_phase_convention(cfg.get("sys_phase_convention", "180"))
        self.set_length_unit(cfg.get("sys_length_unit", "M"))
        self.set_low_blanking(cfg.get("sys_low_blanking", False))
        self.set_graph_style(cfg.get("sys_graph_style", "LINES"))
        self.set_step_message(cfg.get("sys_step_message", True))
        self.set_prog_direct(cfg.get("sys_prog_direct", False))
        self.set_control_mode(cfg.get("sys_control_mode", "VOLTS"))
        self.set_keyboard_beep(cfg.get("sys_keyboard_beep", True))
        self.set_autozero(cfg.get("sys_autozero", True))
        # SYS Shunt is the same CONFIG 23 as MODE Shunt — set from SYS so
        # MODE no longer needs to expose it.
        self.set_lcr_shunt_mode(cfg.get("sys_shunt_mode", "DEFAULT"))

        # ----- Sweep extras -----
        self.set_sweep_type(cfg.get("sweep_type", "SINGLE"))
        self.set_gen_after_sweep(cfg.get("sweep_gen_after", False))
        self.set_graph1_scaling(cfg.get("sweep_graph1_auto", True))
        self.set_graph2_scaling(cfg.get("sweep_graph2_auto", True))
        self.set_frequency_marker(
            cfg.get("sweep_freq_marker_on", False),
            cfg.get("sweep_freq_marker_hz"))

        # ----- OUT (generator) -----
        self.set_waveform(cfg.get("out_waveform", "SINEWA"))
        self.set_amplitude(cfg["amplitude_vpeak"])
        self.set_offset(cfg["offset_v"])
        self.set_frequency_step(cfg.get("out_freq_step", 2.0))
        self.set_amplitude_step(cfg.get("out_amp_step", 1.1))
        if cfg.get("sys_control_mode", "VOLTS").upper() == "DBM":
            self.set_ceiling_dbm(cfg.get("out_ceiling", 10.0))
        self.set_frequency(cfg["sweep_start_hz"])
        self.set_sweep(cfg["sweep_steps"], cfg["sweep_start_hz"],
                       cfg["sweep_end_hz"], cfg["sweep_log"])
        # Honor the OUT panel's Output toggle. Experiment loop will
        # turn output on at each sweep anyway, but this lets the user
        # park it on/off for manual work.
        if cfg.get("out_output_on", False):
            self.output_on(gen_when_complete=True)
        else:
            self.output_off()
