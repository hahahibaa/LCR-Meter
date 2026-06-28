import os
import sys
import json
import threading
import traceback
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import serial.tools.list_ports

from psm1735 import PSM1735, PSMError
from temp_reader import TempReader
from excel_writer import ExcelWriter
from experiment import Experiment

try:
    from furnace import DPI1100, FurnaceAdapter, FurnaceError
    FURNACE_OK = True
except Exception:
    FURNACE_OK = False

try:
    from keithley import Keithley2400, KeithleyError
    KEITHLEY_OK = True
except Exception:
    KEITHLEY_OK = False


class _NullTempController:
    """Stand-in temp controller for 'no heat source' mode.

    The experiment loop calls a few methods on the temp controller. For an
    IV-only experiment at ambient, we don't actually want to drive any
    heater — just satisfy the interface so the loop runs the LCR / IV
    sweeps once per logical 'setpoint' without trying to read temperature.
    """
    def is_open(self):           return True
    def open(self):              pass
    def close(self):             pass
    def latest(self):            return 25.0   # report nominal ambient
    def get(self):               return 25.0, 0.0
    def set_target(self, t):     pass
    def set_safety_limit(self, t): pass
    def force_heater_off(self):  pass
    def force_heater_on(self):   pass
    def auto_heater(self):       pass
    def send_command(self, *a):  pass
    def heater_state(self):      return None
    def last_target(self):       return None
    def error(self):             return None

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk,
    )
    MATPLOTLIB_OK = True
except Exception:
    MATPLOTLIB_OK = False


# ---------- tooltip helper ----------
class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self._tip, text=self.text, justify="left",
                       background="#ffffe0", relief="solid", borderwidth=1,
                       wraplength=420, padx=6, pady=4, font=("Segoe UI", 9))
        lbl.pack()

    def _hide(self, _):
        if self._tip:
            self._tip.destroy()
            self._tip = None


def add_row(parent, row, label_text, widget, tooltip_text=None, unit=None):
    lbl = ttk.Label(parent, text=label_text)
    lbl.grid(row=row, column=0, sticky="w", padx=4, pady=2)
    widget.grid(row=row, column=1, sticky="ew", padx=4, pady=2)
    if unit:
        ttk.Label(parent, text=unit).grid(row=row, column=2, sticky="w", padx=2)
    if tooltip_text:
        Tooltip(lbl, tooltip_text)
        Tooltip(widget, tooltip_text)


SWEEP_DEFAULTS = {
    "sweep_start_hz": 1000.0,
    "sweep_end_hz": 1_000_000.0,
    "sweep_steps": 32,
    "sweep_log": True,
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PSM1735 Temperature-Indexed LCR Logger")
        self.geometry("1100x780")
        try:
            self.state("zoomed")  # maximise on Windows
        except tk.TclError:
            self.attributes("-zoomed", True)  # fallback for Linux

        self.psm = None
        self.temp = None
        self.furnace = None
        self.keithley = None
        self.excel = None
        self.experiment = None
        self._active_temp_controller = None

        self._build_ui()
        self._refresh_ports()
        self.after(500, self._tick)

    # ---------- UI ----------
    def _build_ui(self):
        # ---- menu bar ----
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Save Project As…",
                              command=self._save_project)
        filemenu.add_command(label="Load Project…",
                              command=self._load_project)
        filemenu.add_separator()
        filemenu.add_command(label="Export CSV…", command=self._export_csv)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=filemenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About",
                              command=lambda: messagebox.showinfo(
                                  "About",
                                  "PSM1735 Temperature-Indexed LCR Logger\n\n"
                                  "Developed by Mohd Vasim, Hiba Khan, "
                                  "and Sohum Biswas."))
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.config(menu=menubar)

        # Pack the bottom bar FIRST so it always claims its height.
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Disconnected")
        bar = ttk.Label(bottom, textvariable=self.status_var, anchor="w",
                        relief="sunken")
        bar.pack(side="left", fill="x", expand=True)
        watermark = tk.Label(
            bottom,
            text="Developed by MOHD VASIM, HIBA KHAN and SOHUM BISWAS",
            foreground="#d8d8d8",
            font=("Segoe UI", 22, "italic"),
        )
        watermark.pack(side="right", padx=16, pady=4)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_main = ttk.Frame(notebook)
        self.tab_psm = ttk.Frame(notebook)
        notebook.add(self.tab_main, text="Experiment")
        notebook.add(self.tab_psm, text="PSM Configuration")

        # In-memory store of every sweep done so far, oldest first.
        # Each entry: {"target": float, "measured": float, "rows": [dict, ...]}
        self._all_sweeps = []

        if MATPLOTLIB_OK:
            self.tab_graph = ttk.Frame(notebook)
            notebook.add(self.tab_graph, text="Live Graph")
            self._build_graph_tab(self.tab_graph)

        self.tab_table = ttk.Frame(notebook)
        notebook.add(self.tab_table, text="Table")
        self._build_table_tab(self.tab_table)

        self.tab_realtime = ttk.Frame(notebook)
        notebook.add(self.tab_realtime, text="Realtime")
        self._build_realtime_tab(self.tab_realtime)

        if FURNACE_OK:
            self.tab_furnace = ttk.Frame(notebook)
            notebook.add(self.tab_furnace, text="Furnace (DPI-1100)")
            self._build_furnace_tab(self.tab_furnace)

        if KEITHLEY_OK:
            self.tab_keithley = ttk.Frame(notebook)
            notebook.add(self.tab_keithley, text="Keithley (2400)")
            self._build_keithley_tab(self.tab_keithley)

        # New KickStart-style LCR companion tabs
        self.tab_terminal = ttk.Frame(notebook)
        notebook.add(self.tab_terminal, text="PSM Terminal")
        self._build_terminal_tab(self.tab_terminal)

        self.tab_notes = ttk.Frame(notebook)
        notebook.add(self.tab_notes, text="Notes")
        self._build_notes_tab(self.tab_notes)

        self.tab_help = ttk.Frame(notebook)
        notebook.add(self.tab_help, text="Help")
        self._build_help_tab(self.tab_help)

        self._build_main_tab(self.tab_main)
        self._build_psm_tab(self.tab_psm)

    def _build_main_tab(self, parent):
        # ---- Connections ----
        conn = ttk.LabelFrame(parent, text="Connections")
        conn.pack(fill="x", padx=6, pady=6)
        for i in range(8):
            conn.columnconfigure(i, weight=1)

        ttk.Label(conn, text="PSM port:").grid(row=0, column=0, sticky="w", padx=4)
        self.psm_port_cb = ttk.Combobox(conn, width=10)
        self.psm_port_cb.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(conn, text="Baud:").grid(row=0, column=2, sticky="e")
        self.psm_baud_cb = ttk.Combobox(
            conn, width=8,
            values=["1200", "2400", "4800", "9600", "19200", "38400",
                    "57600", "115200"])
        self.psm_baud_cb.set("19200")
        self.psm_baud_cb.grid(row=0, column=3, sticky="ew", padx=4)
        Tooltip(self.psm_baud_cb,
                "PSM1735 RS232/USB baud rate. Default is 19200 per the comms "
                "manual. Set this to match the value selected in the PSM's "
                "MONITOR menu (COMMS).")

        ttk.Label(conn, text="Arduino port:").grid(row=0, column=4, sticky="e", padx=4)
        self.ard_port_cb = ttk.Combobox(conn, width=10)
        self.ard_port_cb.grid(row=0, column=5, sticky="ew", padx=4)
        ttk.Label(conn, text="Baud:").grid(row=0, column=6, sticky="e")
        self.ard_baud_cb = ttk.Combobox(
            conn, width=8,
            values=["9600", "19200", "38400", "57600", "115200"])
        self.ard_baud_cb.set("115200")
        self.ard_baud_cb.grid(row=0, column=7, sticky="ew", padx=4)
        Tooltip(self.ard_baud_cb,
                "Baud rate that the Arduino MAX31865 sketch uses (115200 in "
                "the supplied .ino).")

        btn_refresh = ttk.Button(conn, text="Refresh ports", command=self._refresh_ports)
        btn_refresh.grid(row=1, column=0, padx=4, pady=4, sticky="w")
        self.btn_connect = ttk.Button(conn, text="Connect", command=self._connect)
        self.btn_connect.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        self.btn_disconnect = ttk.Button(conn, text="Disconnect",
                                         command=self._disconnect, state="disabled")
        self.btn_disconnect.grid(row=1, column=2, padx=4, pady=4, sticky="ew")
        self.btn_idn = ttk.Button(conn, text="Read PSM ID", command=self._read_idn,
                                  state="disabled")
        self.btn_idn.grid(row=1, column=3, padx=4, pady=4, sticky="ew")

        # ---- Experiment ----
        exp = ttk.LabelFrame(parent, text="Experiment")
        exp.pack(fill="x", padx=6, pady=6)
        exp.columnconfigure(1, weight=1)

        # Heat source selector
        self.heat_source_var = tk.StringVar(value="ARDUINO")
        hs_frame = ttk.Frame(exp)
        hs_frame.grid(row=0, column=0, sticky="w", padx=4, pady=(0, 4),
                       columnspan=3)
        ttk.Label(hs_frame, text="Heat source:",
                  font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        hs_rb1 = ttk.Radiobutton(
            hs_frame, text="Arduino + relay heater",
            value="ARDUINO", variable=self.heat_source_var)
        hs_rb1.pack(side="left", padx=4)
        hs_rb2 = ttk.Radiobutton(
            hs_frame, text="DPI-1100 furnace",
            value="FURNACE", variable=self.heat_source_var,
            state=("normal" if FURNACE_OK else "disabled"))
        hs_rb2.pack(side="left", padx=4)
        hs_rb3 = ttk.Radiobutton(
            hs_frame, text="None (room temp)",
            value="NONE", variable=self.heat_source_var)
        hs_rb3.pack(side="left", padx=4)
        Tooltip(hs_rb1,
                "Drive temperature with the Arduino + relay heater "
                "controller. PT100 temperature is read over the Arduino "
                "serial link. Requires Arduino to be Connected.")
        Tooltip(hs_rb2,
                "Drive temperature with the Brainchild BTC-9100 inside "
                "the DPI-1100 calibrator over Modbus RTU. PV is read "
                "from the controller. Requires the Furnace to be "
                "Connected in the Furnace tab.")
        Tooltip(hs_rb3,
                "No temperature control. Useful for IV-only experiments "
                "at ambient temperature, or quick LCR measurements. "
                "Arduino is not required for this mode.")

        # Measurement-type selector — on its own row for visibility
        self.measurement_type_var = tk.StringVar(value="LCR")
        mt_frame = ttk.Frame(exp)
        mt_frame.grid(row=1, column=0, sticky="w", padx=4, pady=(0, 4),
                       columnspan=6)
        ttk.Label(mt_frame, text="Measurement:",
                  font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        for label, val, tip in [
            ("LCR sweep only", "LCR",
             "Run only the PSM LCR frequency sweep at each setpoint. "
             "Keithley not required."),
            ("IV sweep only",  "IV",
             "Run only a Keithley IV sweep at each setpoint. PSM not "
             "required for this mode."),
            ("LCR + Keithley (IV) at each setpoint", "BOTH",
             "Combined: at every temperature setpoint, first run a PSM "
             "LCR sweep, then run a Keithley IV sweep. Both saved to "
             "Excel as separate sheets per temperature."),
        ]:
            rb = ttk.Radiobutton(mt_frame, text=label, value=val,
                                 variable=self.measurement_type_var)
            rb.pack(side="left", padx=4)
            Tooltip(rb, tip)

        self.mode_var = tk.StringVar(value="stepped")
        rb1 = ttk.Radiobutton(
            exp, text="Stepped (manual capture at each setpoint)",
            value="stepped", variable=self.mode_var)
        rb1.grid(row=2, column=0, sticky="w", padx=4, columnspan=6)
        Tooltip(rb1,
                "Sample is held at each setpoint. The app waits for you to "
                "press the Capture button, runs the frequency sweep, then "
                "waits for you again at the next setpoint.")

        rb2 = ttk.Radiobutton(
            exp, text="Continuous (auto-capture when temp crosses each target)",
            value="continuous", variable=self.mode_var)
        rb2.grid(row=3, column=0, sticky="w", padx=4, columnspan=6)
        Tooltip(rb2,
                "Sample temperature drifts. The app watches the live PT100 "
                "reading and auto-triggers a sweep whenever the temperature "
                "is within tolerance of the next target.")

        self.temp_start_var = tk.DoubleVar(value=-273.0)
        self.temp_end_var = tk.DoubleVar(value=1000.0)
        self.temp_step_var = tk.DoubleVar(value=10.0)
        self.tol_var = tk.DoubleVar(value=0.5)
        self.direction_var = tk.StringVar(value="both")
        self.outfile_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Downloads",
                               "lcr_temp_log.xlsx"))

        row = 4
        e1 = ttk.Entry(exp, textvariable=self.temp_start_var, width=10)
        add_row(exp, row, "Temp start:", e1,
                "First temperature setpoint (degrees C).", "C")
        row += 1
        e2 = ttk.Entry(exp, textvariable=self.temp_end_var, width=10)
        add_row(exp, row, "Temp end:", e2,
                "Last temperature setpoint (degrees C). May be lower than "
                "start for a cooling experiment.", "C")
        row += 1
        e3 = ttk.Entry(exp, textvariable=self.temp_step_var, width=10)
        add_row(exp, row, "Temp step:", e3,
                "Spacing between setpoints. Magnitude only — direction is "
                "set by start vs end.", "C")
        row += 1
        e4 = ttk.Entry(exp, textvariable=self.tol_var, width=10)
        add_row(exp, row, "Tolerance (continuous):", e4,
                "Continuous mode only. A sweep is triggered when the "
                "measured temperature is within +/- this many degrees of a "
                "target.", "C")
        row += 1
        dir_cb = ttk.Combobox(exp, textvariable=self.direction_var, width=12,
                              values=["both", "cooling", "heating"],
                              state="readonly")
        add_row(exp, row, "Direction (continuous):", dir_cb,
                "Continuous mode only. Restricts auto-capture to a specific "
                "thermal direction so you don't double-capture on the return "
                "path.")
        row += 1

        of_frame = ttk.Frame(exp)
        of_entry = ttk.Entry(of_frame, textvariable=self.outfile_var)
        of_entry.pack(side="left", fill="x", expand=True)
        of_btn = ttk.Button(of_frame, text="Browse...", command=self._choose_outfile)
        of_btn.pack(side="left", padx=4)
        add_row(exp, row, "Excel output:", of_frame,
                "Workbook path. One sheet is created per temperature point. "
                "If the file exists, new sheets are appended.")
        row += 1

        # ---- Controls ----
        ctrl = ttk.LabelFrame(parent, text="Control")
        ctrl.pack(fill="x", padx=6, pady=6)
        for i in range(6):
            ctrl.columnconfigure(i, weight=1)

        self.btn_apply = ttk.Button(ctrl, text="Apply settings to PSM",
                                    command=self._apply_settings, state="disabled")
        self.btn_apply.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        Tooltip(self.btn_apply,
                "Sends every value from the PSM Configuration tab to the "
                "instrument over COM. Do this once before starting an "
                "experiment.")

        self.btn_output_on = ttk.Button(ctrl, text="Output ON",
                                        command=self._output_on, state="disabled")
        self.btn_output_on.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        Tooltip(self.btn_output_on,
                "Turns the PSM generator on. The signal level is whatever "
                "is set in the Output section. Used for manual testing — "
                "the experiment loop will toggle this automatically.")
        self.btn_output_off = ttk.Button(ctrl, text="Output OFF",
                                         command=self._output_off, state="disabled")
        self.btn_output_off.grid(row=0, column=2, padx=4, pady=4, sticky="ew")

        self.btn_start = ttk.Button(ctrl, text="Start experiment",
                                    command=self._start_exp, state="disabled")
        self.btn_start.grid(row=0, column=3, padx=4, pady=4, sticky="ew")
        self.btn_capture = ttk.Button(ctrl, text="Capture now",
                                      command=self._capture_now, state="disabled")
        self.btn_capture.grid(row=0, column=4, padx=4, pady=4, sticky="ew")
        Tooltip(self.btn_capture,
                "Stepped mode only. Tells the running experiment to take a "
                "sweep at the current setpoint right now.")
        self.btn_stop = ttk.Button(ctrl, text="Stop", command=self._stop_exp,
                                   state="disabled")
        self.btn_stop.grid(row=0, column=5, padx=4, pady=4, sticky="ew")

        # ---- Live status ----
        live = ttk.LabelFrame(parent, text="Live status")
        live.pack(fill="x", padx=6, pady=6)
        for i in range(4):
            live.columnconfigure(i, weight=1)

        self.cur_temp_var = tk.StringVar(value="--.-- C")
        self.target_var = tk.StringVar(value="--")
        self.sweep_status_var = tk.StringVar(value="idle")
        self.heater_var = tk.StringVar(value="--")
        self.heater_target_var = tk.StringVar(value="--")
        ttk.Label(live, text="Live temp:").grid(row=0, column=0, sticky="e")
        ttk.Label(live, textvariable=self.cur_temp_var,
                  font=("Segoe UI", 11, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(live, text="Next target:").grid(row=0, column=2, sticky="e")
        ttk.Label(live, textvariable=self.target_var).grid(row=0, column=3, sticky="w")
        ttk.Label(live, text="Sweep:").grid(row=1, column=0, sticky="e")
        ttk.Label(live, textvariable=self.sweep_status_var).grid(row=1, column=1, sticky="w")
        ttk.Label(live, text="Heater:").grid(row=1, column=2, sticky="e")
        self.heater_label = ttk.Label(live, textvariable=self.heater_var,
                                      font=("Segoe UI", 11, "bold"))
        self.heater_label.grid(row=1, column=3, sticky="w")
        ttk.Label(live, text="Heater target:").grid(row=2, column=0, sticky="e")
        ttk.Label(live, textvariable=self.heater_target_var).grid(row=2, column=1, sticky="w")

        # Manual heater override row
        hctrl = ttk.LabelFrame(parent, text="Heater manual control (for testing)")
        hctrl.pack(fill="x", padx=6, pady=6)
        for i in range(6):
            hctrl.columnconfigure(i, weight=1)
        self.manual_target_var = tk.DoubleVar(value=25.0)
        ttk.Label(hctrl, text="Target:").grid(row=0, column=0, sticky="e")
        e = ttk.Entry(hctrl, textvariable=self.manual_target_var, width=8)
        e.grid(row=0, column=1, sticky="w", padx=4)
        Tooltip(e, "Sends SET:<value> to the Arduino. The Arduino bang-bang "
                   "controller will drive the heater to this temperature.")
        self.btn_set_target = ttk.Button(hctrl, text="Send target",
                                         command=self._send_manual_target,
                                         state="disabled")
        self.btn_set_target.grid(row=0, column=2, padx=4, sticky="ew")
        self.btn_heater_on = ttk.Button(hctrl, text="Heater FORCE ON",
                                        command=self._heater_force_on,
                                        state="disabled")
        self.btn_heater_on.grid(row=0, column=3, padx=4, sticky="ew")
        Tooltip(self.btn_heater_on,
                "Forces the SSR ON regardless of target — for verifying "
                "the wiring works. Hard cut-off in the sketch still applies.")
        self.btn_heater_off = ttk.Button(hctrl, text="Heater FORCE OFF",
                                         command=self._heater_force_off,
                                         state="disabled")
        self.btn_heater_off.grid(row=0, column=4, padx=4, sticky="ew")
        self.btn_heater_auto = ttk.Button(hctrl, text="Heater AUTO",
                                          command=self._heater_auto,
                                          state="disabled")
        self.btn_heater_auto.grid(row=0, column=5, padx=4, sticky="ew")
        Tooltip(self.btn_heater_auto,
                "Returns control to the Arduino's bang-bang regulator using "
                "the last target sent.")

        # ---- Log ----
        log_frame = ttk.LabelFrame(parent, text="Log")
        log_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text = tk.Text(log_frame, height=10, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # ---------- Live Graph tab ----------
    GRAPH_PARAMS = {
        "Capacitance C (F)":     "C_F",
        "Inductance L (H)":      "L_H",
        "Resistance R (Ohm)":    "R_Ohm",
        "Impedance |Z| (Ohm)":   "Impedance_Ohm",
        "Phase (deg)":           "Phase_deg",
        "Tan delta":             "TanD",
        "Q factor":              "Q",
    }

    def _build_graph_tab(self, parent):
        # ---- controls ----
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", padx=6, pady=4)

        self.graph_mode_var = tk.StringVar(value="Bode")
        self.graph_param_var = tk.StringVar(value="Capacitance C (F)")
        self.graph_xlog_var = tk.BooleanVar(value=True)
        self.graph_ylog_var = tk.BooleanVar(value=True)
        self.graph_show_var = tk.StringVar(value="All sweeps")
        self.graph_abs_var = tk.BooleanVar(value=True)

        ttk.Label(ctrl, text="View:").pack(side="left", padx=(0, 4))
        mode_cb = ttk.Combobox(ctrl, textvariable=self.graph_mode_var,
                               values=["Bode", "Nyquist"],
                               state="readonly", width=8)
        mode_cb.pack(side="left", padx=4)
        mode_cb.bind("<<ComboboxSelected>>", lambda e: self._redraw_graph())
        Tooltip(mode_cb,
                "Bode: parameter vs frequency (logy=linear by default). "
                "Nyquist: imaginary impedance vs real impedance — useful "
                "for impedance spectroscopy / dielectric work.")

        ttk.Label(ctrl, text="Parameter:").pack(side="left", padx=(0, 4))
        cb = ttk.Combobox(ctrl, textvariable=self.graph_param_var,
                          values=list(self.GRAPH_PARAMS.keys()),
                          state="readonly", width=22)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._redraw_graph())

        ttk.Label(ctrl, text="  Show:").pack(side="left", padx=(8, 4))
        cb2 = ttk.Combobox(ctrl, textvariable=self.graph_show_var,
                           values=["All sweeps", "Latest sweep only"],
                           state="readonly", width=18)
        cb2.pack(side="left", padx=4)
        cb2.bind("<<ComboboxSelected>>", lambda e: self._redraw_graph())

        xlog_cb = ttk.Checkbutton(ctrl, text="X log",
                                  variable=self.graph_xlog_var,
                                  command=self._redraw_graph)
        xlog_cb.pack(side="left", padx=8)
        ylog_cb = ttk.Checkbutton(ctrl, text="Y log",
                                  variable=self.graph_ylog_var,
                                  command=self._redraw_graph)
        ylog_cb.pack(side="left", padx=4)
        abs_cb = ttk.Checkbutton(ctrl, text="|Y| (abs)",
                                 variable=self.graph_abs_var,
                                 command=self._redraw_graph)
        abs_cb.pack(side="left", padx=4)
        Tooltip(cb,
                "Which measured quantity to plot. Matches the PSM's "
                "'Y-axis Plot A' selection from Graph Settings.")
        Tooltip(cb2,
                "All sweeps: overlay every temperature point that's "
                "been swept so far (colour-coded by temperature). "
                "Latest sweep only: show just the most recent.")
        Tooltip(xlog_cb, "Logarithmic X-axis (frequency). Off = linear.")
        Tooltip(ylog_cb, "Logarithmic Y-axis. Off = linear.")
        Tooltip(abs_cb,
                "Plot |Y| so negative measurements (e.g. negative "
                "capacitance from open compensation) don't break log scaling.")

        ttk.Button(ctrl, text="Clear data",
                   command=self._clear_graph_data).pack(side="right", padx=4)

        # ---- figure ----
        self._fig = Figure(figsize=(8, 5), dpi=96)
        self._ax = self._fig.add_subplot(111)
        self._ax.set_xlabel("Frequency (Hz)")
        self._ax.set_ylabel(self.graph_param_var.get())
        self._ax.grid(True, which="both", alpha=0.3)

        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().pack(fill="both", expand=True,
                                          padx=6, pady=(0, 4))
        toolbar = NavigationToolbar2Tk(self._canvas, parent)
        toolbar.update()
        self._canvas.draw()

    def _clear_graph_data(self):
        self._all_sweeps.clear()
        self._redraw_graph()

    def add_sweep_to_graph(self, target_c, measured_c, rows):
        """Called from the experiment thread after each successful sweep."""
        self._all_sweeps.append({
            "target": float(target_c),
            "measured": float(measured_c) if measured_c is not None else float("nan"),
            "rows": list(rows),
        })
        # marshal back to the Tk main thread before touching widgets
        self.after(0, self._redraw_graph)
        self.after(0, self._refresh_table)

    def _redraw_graph(self):
        if not MATPLOTLIB_OK:
            return
        ax = self._ax
        ax.clear()
        ax.grid(True, which="both", alpha=0.3)

        mode = self.graph_mode_var.get()
        sweeps = self._all_sweeps
        if self.graph_show_var.get() == "Latest sweep only" and sweeps:
            sweeps = sweeps[-1:]

        if not sweeps:
            ax.text(0.5, 0.5, "No sweeps captured yet",
                    ha="center", va="center", transform=ax.transAxes,
                    color="grey", fontsize=11)
            self._canvas.draw_idle()
            return

        try:
            cmap = matplotlib.colormaps.get_cmap("viridis")
        except AttributeError:
            cmap = matplotlib.cm.get_cmap("viridis")
        temps = [s["target"] for s in sweeps]
        tmin, tmax = min(temps), max(temps)
        tspan = (tmax - tmin) if tmax > tmin else 1.0

        if mode == "Nyquist":
            import math
            ax.set_xlabel("Re(Z)  (Ohm)")
            ax.set_ylabel("-Im(Z)  (Ohm)")
            ax.set_xscale("linear")
            ax.set_yscale("linear")
            ax.set_aspect("equal", adjustable="datalim")
            for s in sweeps:
                zr, zi = [], []
                for r in s["rows"]:
                    z = r.get("Impedance_Ohm")
                    p = r.get("Phase_deg")
                    if z is None or p is None:
                        continue
                    rad = p * math.pi / 180.0
                    zr.append(z * math.cos(rad))
                    zi.append(-z * math.sin(rad))  # convention: -Im(Z) on Y
                if not zr:
                    continue
                color = cmap((s["target"] - tmin) / tspan) if len(sweeps) > 1 else cmap(0.6)
                ax.plot(zr, zi, "-o", markersize=3, linewidth=1.2,
                        color=color, label=f"{s['target']:+.1f} C")
        else:
            ax.set_xlabel("Frequency (Hz)")
            param_label = self.graph_param_var.get()
            column = self.GRAPH_PARAMS[param_label]
            ax.set_ylabel(param_label)
            ax.set_xscale("log" if self.graph_xlog_var.get() else "linear")
            ax.set_yscale("log" if self.graph_ylog_var.get() else "linear")
            for s in sweeps:
                freqs = [r["Frequency_Hz"] for r in s["rows"]]
                vals = [r.get(column) for r in s["rows"]]
                if self.graph_abs_var.get():
                    vals = [abs(v) if v is not None else None for v in vals]
                xy = [(f, v) for f, v in zip(freqs, vals) if v is not None]
                if not xy:
                    continue
                fx, fy = zip(*xy)
                color = cmap((s["target"] - tmin) / tspan) if len(sweeps) > 1 else cmap(0.6)
                ax.plot(fx, fy, "-o", markersize=3, linewidth=1.2,
                        color=color, label=f"{s['target']:+.1f} C")

        if len(sweeps) <= 12:
            ax.legend(loc="best", fontsize=8)
        self._canvas.draw_idle()

    # ---------- Table tab ----------
    TABLE_COLUMNS = [
        ("Frequency_Hz", "Frequency (Hz)", 110),
        ("C_F",          "C (F)",          110),
        ("L_H",          "L (H)",          110),
        ("R_Ohm",        "R (Ohm)",        110),
        ("Impedance_Ohm","|Z| (Ohm)",      110),
        ("Phase_deg",    "Phase (deg)",    90),
        ("Q",            "Q",              70),
        ("TanD",         "Tan delta",      90),
    ]

    # Extra columns shown when "All sweeps (live)" is selected.
    LIVE_TABLE_PREFIX = [
        ("TargetT_C",   "Target T (C)",   90),
        ("MeasuredT_C", "Measured T (C)", 100),
    ]

    def _build_table_tab(self, parent):
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", padx=6, pady=4)
        self.table_select_var = tk.StringVar(value="All sweeps (live)")
        ttk.Label(ctrl, text="Show sweep:").pack(side="left")
        self.table_sweep_cb = ttk.Combobox(
            ctrl, textvariable=self.table_select_var,
            values=["All sweeps (live)", "Latest"],
            state="readonly", width=22)
        self.table_sweep_cb.pack(side="left", padx=4)
        self.table_sweep_cb.bind("<<ComboboxSelected>>",
                                 lambda e: self._refresh_table())
        self.table_autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="Auto-scroll to latest",
                         variable=self.table_autoscroll_var).pack(
                             side="left", padx=12)
        ttk.Button(ctrl, text="Export CSV",
                   command=self._export_csv).pack(side="right", padx=4)
        Tooltip(self.table_sweep_cb,
                "Pick which captured sweep to display. 'All sweeps (live)' "
                "shows every captured point from every sweep with target / "
                "measured temperature columns — appended as each sweep "
                "completes (KickStart-style).")

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True, padx=6, pady=4)
        # The table can show two column sets depending on mode. We rebuild
        # it whenever the mode changes via _build_table_columns.
        self._table_frame = table_frame
        self.table = None
        self._build_table_columns(live_mode=True)

    def _build_table_columns(self, live_mode):
        # Tear down and rebuild the Treeview with the correct columns.
        for child in self._table_frame.winfo_children():
            child.destroy()
        if live_mode:
            col_specs = self.LIVE_TABLE_PREFIX + list(self.TABLE_COLUMNS)
        else:
            col_specs = list(self.TABLE_COLUMNS)
        cols = [c[0] for c in col_specs]
        self.table = ttk.Treeview(self._table_frame, columns=cols,
                                   show="headings")
        for key, label, width in col_specs:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="e")
        yscroll = ttk.Scrollbar(self._table_frame, orient="vertical",
                                 command=self.table.yview)
        self.table.configure(yscrollcommand=yscroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self._table_live_mode = live_mode

    def _refresh_table(self):
        if not hasattr(self, "table") or self.table is None:
            return
        sel = self.table_select_var.get()
        # update selector with all captured temperatures
        vals = ["All sweeps (live)", "Latest"] + [
            f"T = {s['target']:+.1f} C" for s in self._all_sweeps]
        self.table_sweep_cb["values"] = vals

        live = (sel == "All sweeps (live)")
        if live != self._table_live_mode:
            self._build_table_columns(live_mode=live)

        # repopulate
        for row in self.table.get_children():
            self.table.delete(row)

        if live:
            for s in self._all_sweeps:
                for r in s["rows"]:
                    self.table.insert("", "end", values=[
                        f"{s['target']:+.2f}",
                        (f"{s['measured']:+.2f}"
                         if s['measured'] == s['measured'] else "--"),
                    ] + [
                        f"{r.get(c[0], ''):.6g}"
                        if isinstance(r.get(c[0]), (int, float))
                        else r.get(c[0], "")
                        for c in self.TABLE_COLUMNS
                    ])
            if self.table_autoscroll_var.get():
                children = self.table.get_children()
                if children:
                    self.table.see(children[-1])
            return

        # single-sweep mode
        sweep = None
        if sel == "Latest" and self._all_sweeps:
            sweep = self._all_sweeps[-1]
        elif sel.startswith("T ="):
            try:
                t = float(sel.split("=")[1].strip().rstrip(" C"))
                for s in self._all_sweeps:
                    if abs(s["target"] - t) < 0.01:
                        sweep = s
                        break
            except Exception:
                pass
        if sweep is None:
            return
        for r in sweep["rows"]:
            self.table.insert("", "end", values=[
                f"{r.get(c[0], ''):.6g}" if isinstance(r.get(c[0]), (int, float))
                else r.get(c[0], "")
                for c in self.TABLE_COLUMNS
            ])

    def _export_csv(self):
        if not self._all_sweeps:
            messagebox.showinfo("No data",
                                "Run an experiment first — nothing to export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("TargetTemp_C,MeasuredTemp_C,"
                        + ",".join(c[0] for c in self.TABLE_COLUMNS) + "\n")
                for s in self._all_sweeps:
                    for r in s["rows"]:
                        f.write(f"{s['target']:.3f},{s['measured']:.3f},")
                        f.write(",".join(
                            str(r.get(c[0], "")) for c in self.TABLE_COLUMNS
                        ))
                        f.write("\n")
            self.log(f"CSV exported to {path}")
            messagebox.showinfo("Exported", f"Written to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ---------- PSM Terminal tab ----------
    def _build_terminal_tab(self, parent):
        intro = ttk.Label(
            parent,
            text=("Free-form PSM ASCII command terminal. Type any command "
                  "(e.g. *IDN?, OUTPUT,ON, LCR?, FSWEEP,?,1,?,?) and press "
                  "Enter or Send. Replies appear in the log below. "
                  "Up / Down arrows recall history."),
            wraplength=900, foreground="#444")
        intro.pack(fill="x", padx=6, pady=(6, 2))

        cmd_row = ttk.Frame(parent)
        cmd_row.pack(fill="x", padx=6, pady=4)
        ttk.Label(cmd_row, text="Cmd:").pack(side="left")
        self.term_cmd_var = tk.StringVar()
        self.term_entry = ttk.Entry(cmd_row, textvariable=self.term_cmd_var)
        self.term_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.term_entry.bind("<Return>", lambda e: self._term_send())
        self.term_entry.bind("<Up>", lambda e: self._term_history(-1))
        self.term_entry.bind("<Down>", lambda e: self._term_history(+1))

        self.term_expect_reply_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cmd_row, text="Expect reply",
                         variable=self.term_expect_reply_var).pack(
                             side="left", padx=4)
        ttk.Button(cmd_row, text="Send",
                   command=self._term_send).pack(side="left", padx=4)
        ttk.Button(cmd_row, text="Clear log",
                   command=self._term_clear).pack(side="left", padx=4)
        Tooltip(self.term_entry,
                "Tip: query commands like *IDN?, LCR?, OPC? return data — "
                "leave 'Expect reply' on for those. Action commands like "
                "OUTPUT,ON return nothing — turn it off to avoid a "
                "read-timeout.")

        # quick-pick buttons for common commands
        qp = ttk.LabelFrame(parent, text="Quick commands")
        qp.pack(fill="x", padx=6, pady=4)
        quick_cmds = [
            ("*IDN?",        "*IDN?",        True),
            ("OPC?",         "*OPC?",        True),
            ("Output ON",    "OUTPUT,ON",    False),
            ("Output OFF",   "OUTPUT,OFF",   False),
            ("Start sweep",  "FSWEEP",       False),
            ("Abort",        "ABORT",        False),
            ("Read LCR",     "LCR?",         True),
            ("Reset",        "*RST",         False),
        ]
        for i, (label, cmd, expects) in enumerate(quick_cmds):
            def make(c=cmd, e=expects):
                return lambda: self._term_send_specific(c, e)
            ttk.Button(qp, text=label, command=make(),
                       width=12).grid(row=i // 4, column=i % 4,
                                      padx=4, pady=4, sticky="ew")
        for c in range(4):
            qp.columnconfigure(c, weight=1)

        # log
        log_frame = ttk.LabelFrame(parent, text="Terminal log")
        log_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.term_log = tk.Text(log_frame, wrap="word",
                                 font=("Consolas", 10),
                                 background="#1e1e1e", foreground="#dcdcdc",
                                 insertbackground="#dcdcdc")
        ys = ttk.Scrollbar(log_frame, orient="vertical",
                            command=self.term_log.yview)
        self.term_log.configure(yscrollcommand=ys.set)
        self.term_log.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.term_log.tag_config("tx", foreground="#9cdcfe")
        self.term_log.tag_config("rx", foreground="#b5cea8")
        self.term_log.tag_config("err", foreground="#f48771")
        self.term_log.tag_config("ts", foreground="#808080")
        self._term_history_buf = []
        self._term_history_idx = 0

    def _term_log_line(self, kind, text):
        if not hasattr(self, "term_log"):
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.term_log.insert("end", f"[{ts}] ", "ts")
        prefix = {"tx": "→ ", "rx": "← ", "err": "! "}.get(kind, "  ")
        self.term_log.insert("end", prefix + text + "\n", kind)
        self.term_log.see("end")

    def _term_clear(self):
        self.term_log.delete("1.0", "end")

    def _term_history(self, delta):
        if not self._term_history_buf:
            return "break"
        self._term_history_idx = max(
            0, min(len(self._term_history_buf),
                   self._term_history_idx + delta))
        if self._term_history_idx >= len(self._term_history_buf):
            self.term_cmd_var.set("")
        else:
            self.term_cmd_var.set(
                self._term_history_buf[self._term_history_idx])
        self.term_entry.icursor("end")
        return "break"

    def _term_send(self):
        cmd = self.term_cmd_var.get().strip()
        if not cmd:
            return
        expects = self.term_expect_reply_var.get()
        self._term_send_specific(cmd, expects)
        self._term_history_buf.append(cmd)
        self._term_history_idx = len(self._term_history_buf)
        self.term_cmd_var.set("")

    def _term_send_specific(self, cmd, expects_reply):
        if not self.psm or not self.psm.is_open():
            self._term_log_line(
                "err",
                "PSM not connected — connect on the Experiment tab first.")
            return
        self._term_log_line("tx", cmd)
        try:
            if expects_reply:
                reply = self.psm.query(cmd)
                self._term_log_line("rx", reply if reply else "(empty)")
            else:
                self.psm.send(cmd)
                self._term_log_line("rx", "(sent, no reply expected)")
        except Exception as e:
            self._term_log_line("err", f"{type(e).__name__}: {e}")

    # ---------- Notes tab ----------
    def _build_notes_tab(self, parent):
        intro = ttk.Label(
            parent,
            text=("Free-text notes for this experiment. Saved into the "
                  "active Excel workbook as a 'Notes' sheet each time you "
                  "click Save to Excel, and included in saved project "
                  "files. Use 'Insert timestamp' (Ctrl+T) to mark a moment."),
            wraplength=900, foreground="#444")
        intro.pack(fill="x", padx=6, pady=(6, 2))

        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", padx=6, pady=4)
        ttk.Button(ctrl, text="Insert timestamp",
                   command=self._notes_insert_ts).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Save to Excel",
                   command=self._notes_save_to_excel).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Save to .txt",
                   command=self._notes_save_to_txt).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Clear",
                   command=lambda: self.notes_text.delete(
                       "1.0", "end")).pack(side="right", padx=4)

        nf = ttk.LabelFrame(parent, text="Notes")
        nf.pack(fill="both", expand=True, padx=6, pady=6)
        self.notes_text = tk.Text(nf, wrap="word",
                                   font=("Segoe UI", 11), undo=True)
        ys = ttk.Scrollbar(nf, orient="vertical",
                            command=self.notes_text.yview)
        self.notes_text.configure(yscrollcommand=ys.set)
        self.notes_text.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.notes_text.bind("<Control-t>",
                              lambda e: (self._notes_insert_ts(), "break")[1])

    def _notes_insert_ts(self):
        ts = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]  ")
        self.notes_text.insert("insert", ts)

    def _notes_get(self):
        return self.notes_text.get("1.0", "end").rstrip()

    def _notes_save_to_excel(self):
        text = self._notes_get()
        if not text:
            messagebox.showinfo("Empty",
                                 "Nothing to save — notes are empty.")
            return
        if not self.excel:
            messagebox.showwarning(
                "No workbook",
                "There's no active experiment workbook yet. Notes will "
                "still be saved when the next experiment writes its file.")
            return
        try:
            self.excel.write_notes(text)
            self.log("Notes saved to Excel workbook.")
            messagebox.showinfo("Saved", "Notes written to the workbook.")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _notes_save_to_txt(self):
        text = self._notes_get()
        if not text:
            messagebox.showinfo("Empty",
                                 "Nothing to save — notes are empty.")
            return
        path = filedialog.asksaveasfilename(
            title="Save notes to text file",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.log(f"Notes saved to {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # ---------- Help tab ----------
    def _build_help_tab(self, parent):
        text = tk.Text(parent, wrap="word",
                        font=("Segoe UI", 11), padx=12, pady=12,
                        background="#fcfcfc", relief="flat")
        ys = ttk.Scrollbar(parent, orient="vertical",
                            command=text.yview)
        text.configure(yscrollcommand=ys.set)
        text.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

        text.tag_config("h1", font=("Segoe UI", 16, "bold"),
                         foreground="#1a4f8a", spacing3=8)
        text.tag_config("h2", font=("Segoe UI", 13, "bold"),
                         foreground="#1a4f8a", spacing1=8, spacing3=4)
        text.tag_config("body", font=("Segoe UI", 11), spacing3=4)
        text.tag_config("code", font=("Consolas", 10),
                         background="#eef0f3")

        def line(t, tag): text.insert("end", t + "\n", tag)
        def code(t):       text.insert("end", t + "\n", "code")

        line("PSM Logger — Quick Reference", "h1")
        line("Workflow", "h2")
        line("1. Connect to the PSM (Experiment tab → Connect).", "body")
        line("2. Configure the PSM (PSM Configuration tab → set Sweep, "
             "OUT, CH1/CH2, MODE, ACQU, TRIM, SYS → Apply settings to PSM).",
             "body")
        line("3. Pick heat source and measurement type (Experiment tab).",
             "body")
        line("4. Enter temperature start / end / step + Excel output path.",
             "body")
        line("5. Press Start. Watch the Live Graph, Table and Realtime tabs.",
             "body")

        line("Tabs at a glance", "h2")
        line("• Experiment — connections + experiment setup + live log",
             "body")
        line("• PSM Configuration — every PSM setting in one place",
             "body")
        line("• Live Graph — Bode + Nyquist plots, updated each sweep",
             "body")
        line("• Table — captured points; 'All sweeps (live)' mode appends "
             "as each sweep completes (KickStart-style sheet)", "body")
        line("• Realtime — park PSM at one frequency, poll continuously",
             "body")
        line("• Furnace — DPI-1100 (Brainchild BTC-9100) Modbus control",
             "body")
        line("• Keithley — 2400-series source meter (VISA / RS-232)",
             "body")
        line("• PSM Terminal — free-form ASCII command terminal",
             "body")
        line("• Notes — free-text notes saved into the Excel workbook",
             "body")

        line("Most-used PSM ASCII commands", "h2")
        code("*IDN?                 instrument identification")
        code("*RST                  reset to power-on state")
        code("OUTPUT,ON | OUTPUT,OFF  enable / disable generator")
        code("FSWEEP                 start a frequency sweep")
        code("ABORT                  cancel an in-progress sweep")
        code("OPC?                   sweep complete? returns 1 or 0")
        code("LCR?                   read latest L, C, R, |Z|, Phase…")
        code("LCR,SWEEP?             read entire sweep dataset")

        line("Configuration sections (PSM Configuration tab)", "h2")
        line("Sweep — start / end frequency, # steps, log or linear.",
             "body")
        line("OUT — generator amplitude, ceiling, frequency, offset, "
             "waveform.", "body")
        line("CH1 / CH2 — input type, connection, range, coupling, "
             "scale, shunt.", "body")
        line("AUX — fixture (IAI / Active head / TAF / None).", "body")
        line("MODE — operation mode, parameter, conditions, sweep model.",
             "body")
        line("ACQU — speed, cycles, delay, phase ref, filter, datalog, "
             "bandwidth.", "body")
        line("TRIM — AC trim channel for amplitude compression.", "body")
        line("SYS — phase convention, length unit, beep, autozero.",
             "body")

        line("Troubleshooting", "h2")
        line("Generator won't enable (Output ON keeps failing): check "
             "the AUX fixture matches your wiring, and the CH1/CH2 "
             "ranges aren't fighting compliance limits.", "body")
        line("Sweep returns no data: confirm OPC? returns 1; if not, the "
             "sweep timed out — increase 'sweep_timeout_s' (3 min default) "
             "or reduce # of steps.", "body")
        line("Excel write failed: close the workbook in Excel — the file "
             "lock prevents writes. Then the next sweep will retry.",
             "body")
        line("Terminal shows 'PSM not connected': connect via Experiment "
             "tab first.", "body")

        line("File menu", "h2")
        line("Save Project As… — write every setting and the notes to "
             "a single JSON file you can reload later.", "body")
        line("Load Project… — restore from that JSON file.", "body")
        line("Export CSV… — one long-form CSV of every captured point "
             "across every sweep.", "body")

        text.configure(state="disabled")

    # ---------- Project save / load ----------
    def _save_project(self):
        path = filedialog.asksaveasfilename(
            title="Save project",
            defaultextension=".kstx",
            filetypes=[("PSM Logger Project", "*.kstx"),
                        ("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            cfg = self._gather_config()
        except Exception as e:
            messagebox.showerror("Save failed",
                                  f"Could not gather settings:\n{e}")
            return
        project = {
            "file_format": "psm_logger_project",
            "format_version": 1,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "psm_config": cfg,
            "experiment": {
                "heat_source": self.heat_source_var.get(),
                "measurement_type": self.measurement_type_var.get(),
                "mode": self.mode_var.get(),
                "temp_start_c": cfg["temp_start_c"],
                "temp_end_c": cfg["temp_end_c"],
                "temp_step_c": cfg["temp_step_c"],
                "tolerance_c": cfg["tolerance_c"],
                "direction": cfg["direction"],
            },
            "notes": (self._notes_get()
                      if hasattr(self, "notes_text") else ""),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(project, f, indent=2)
            self.log(f"Project saved to {path}")
            messagebox.showinfo("Saved", f"Project written to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _load_project(self):
        path = filedialog.askopenfilename(
            title="Load project",
            filetypes=[("PSM Logger Project", "*.kstx"),
                        ("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                project = json.load(f)
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        if project.get("file_format") != "psm_logger_project":
            messagebox.showwarning(
                "Unknown format",
                "This file isn't a PSM Logger project. Loading anyway "
                "may not populate every field.")

        applied = 0
        cfg = project.get("psm_config", {})

        # Map of cfg key -> tk variable attribute name on self.
        # Values are converted to strings where the var is a StringVar.
        key_to_var = {
            "sweep_start_hz":        ("sweep_start_var", float),
            "sweep_end_hz":          ("sweep_end_var", float),
            "sweep_steps":           ("sweep_steps_var", int),
            "sweep_type":            ("sweep_type_var", str),
            "amplitude_vpeak":       ("amp_var", float),
            "offset_v":              ("offset_var", float),
            "ch1_input":             ("ch1_input_var", str),
            "ch1_connection":        ("ch1_conn_var", str),
            "ch1_min_range":         ("ch1_minrange_var", str),
            "ch1_ranging":           ("ch1_ranging_var", str),
            "ch1_coupling":          ("ch1_coupling_var", str),
            "ch1_scale":             ("ch1_scale_var", float),
            "ch2_input":             ("ch2_input_var", str),
            "ch2_connection":        ("ch2_conn_var", str),
            "ch2_min_range":         ("ch2_minrange_var", str),
            "ch2_ranging":           ("ch2_ranging_var", str),
            "ch2_coupling":          ("ch2_coupling_var", str),
            "ch2_scale":             ("ch2_scale_var", float),
            "ch2_shunt_ohms":        ("ch2_shunt_var", float),
            "aux_fixture":           ("fixture_var", str),
            "operation_mode":        ("op_mode_var", str),
            "lcr_conditions":        ("lcr_cond_var", str),
            "lcr_parameter":         ("lcr_param_var", str),
            "lcr_sweep_model":       ("lcr_sweep_model_var", str),
            "lcr_shunt_mode":        ("lcr_shunt_var", str),
            "lcr_connection":        ("lcr_connection_var", str),
            "lcr_graph":             ("lcr_graph_var", str),
            "acqu_speed":            ("acqu_speed_var", str),
            "acqu_min_cycles":       ("acqu_mincycles_var", int),
            "acqu_delay_s":          ("acqu_delay_var", float),
            "acqu_phase_ref":        ("acqu_phaseref_var", str),
            "acqu_filter":           ("acqu_filter_var", str),
            "acqu_bandwidth":        ("acqu_bandwidth_var", str),
            "trim_channel":          ("trim_channel_var", str),
            "trim_level":            ("trim_level_var", float),
            "trim_tolerance_pct":    ("trim_tol_var", float),
            "out_waveform":          ("out_waveform_var", str),
            "out_freq_step":         ("out_freq_step_var", float),
            "out_amp_step":          ("out_amp_step_var", float),
            "out_ceiling":           ("out_ceiling_var", float),
            "sys_phase_convention":  ("sys_phconv_var", str),
            "sys_length_unit":       ("sys_length_var", str),
            "sys_graph_style":       ("sys_graph_var", str),
            "sys_shunt_mode":        ("sys_shunt_var", str),
            "sys_control_mode":      ("sys_control_var", str),
            "temp_start_c":          ("temp_start_var", float),
            "temp_end_c":            ("temp_end_var", float),
            "temp_step_c":           ("temp_step_var", float),
            "tolerance_c":           ("tol_var", float),
            "direction":             ("direction_var", str),
            "mode":                  ("mode_var", str),
        }
        for key, (attr, conv) in key_to_var.items():
            if key not in cfg:
                continue
            var = getattr(self, attr, None)
            if var is None:
                continue
            try:
                value = conv(cfg[key])
                var.set(value)
                applied += 1
            except Exception:
                pass

        # Experiment-tab radios
        exp = project.get("experiment", {})
        if "heat_source" in exp:
            self.heat_source_var.set(exp["heat_source"])
        if "measurement_type" in exp:
            self.measurement_type_var.set(exp["measurement_type"])

        # Notes
        notes = project.get("notes", "")
        if hasattr(self, "notes_text") and notes:
            self.notes_text.delete("1.0", "end")
            self.notes_text.insert("1.0", notes)

        self.log(f"Project loaded from {path} — {applied} settings applied.")
        messagebox.showinfo(
            "Loaded",
            f"Project loaded.\n{applied} PSM settings restored.\n"
            f"Saved at: {project.get('saved_at', '(unknown)')}")

    # ---------- Realtime tab ----------
    REALTIME_LABELS = [
        ("Frequency_Hz",   "Frequency", "Hz"),
        ("C_F",            "C",         "F"),
        ("L_H",            "L",         "H"),
        ("R_Ohm",          "R",         "Ohm"),
        ("Impedance_Ohm",  "|Z|",       "Ohm"),
        ("Phase_deg",      "Phase",     "deg"),
        ("Q",              "Q",         ""),
        ("TanD",           "Tan delta", ""),
    ]

    def _build_realtime_tab(self, parent):
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", padx=6, pady=6)
        self.rt_running = False
        self.rt_freq_var = tk.DoubleVar(value=1000.0)
        ttk.Label(ctrl, text="Frequency:").pack(side="left")
        ttk.Entry(ctrl, textvariable=self.rt_freq_var, width=10).pack(
            side="left", padx=4)
        ttk.Label(ctrl, text="Hz").pack(side="left")
        self.btn_rt_start = ttk.Button(ctrl, text="Start realtime",
                                       command=self._rt_start, state="disabled")
        self.btn_rt_start.pack(side="left", padx=8)
        self.btn_rt_stop = ttk.Button(ctrl, text="Stop",
                                      command=self._rt_stop, state="disabled")
        self.btn_rt_stop.pack(side="left", padx=4)
        Tooltip(self.btn_rt_start,
                "Park the PSM at the chosen frequency and poll LCR? "
                "twice a second. Useful for checking your DUT and "
                "settling temperature before kicking off a sweep.")

        # readout grid
        grid = ttk.LabelFrame(parent, text="Live LCR readout")
        grid.pack(fill="both", expand=True, padx=6, pady=6)
        self.rt_value_vars = {}
        for i, (key, label, unit) in enumerate(self.REALTIME_LABELS):
            ttk.Label(grid, text=label + ":",
                      font=("Segoe UI", 12)).grid(
                row=i, column=0, sticky="e", padx=8, pady=6)
            var = tk.StringVar(value="--")
            self.rt_value_vars[key] = var
            ttk.Label(grid, textvariable=var,
                      font=("Segoe UI", 14, "bold"),
                      foreground="#1a4f8a").grid(
                row=i, column=1, sticky="w", padx=4, pady=6)
            ttk.Label(grid, text=unit,
                      font=("Segoe UI", 11)).grid(
                row=i, column=2, sticky="w", padx=4, pady=6)

    def _rt_start(self):
        if not self.psm:
            messagebox.showerror("Not connected",
                                 "Connect to the PSM first.")
            return
        try:
            f = float(self.rt_freq_var.get())
            self.psm.set_mode_lcr()
            self.psm.set_frequency(f)
            self.psm.output_on(gen_when_complete=True)
        except Exception as e:
            messagebox.showerror("Realtime start failed", str(e))
            return
        self.rt_running = True
        self.btn_rt_start.config(state="disabled")
        self.btn_rt_stop.config(state="normal")
        self.log(f"Realtime LCR @ {f:g} Hz started")
        self._rt_tick()

    def _rt_stop(self):
        self.rt_running = False
        self.btn_rt_start.config(state="normal")
        self.btn_rt_stop.config(state="disabled")
        try:
            self.psm.output_off()
        except Exception:
            pass
        self.log("Realtime stopped")

    def _rt_tick(self):
        if not self.rt_running or not self.psm:
            return
        try:
            reply = self.psm.query("LCR?")
            parts = [p.strip() for p in reply.split(",") if p.strip()]
            if len(parts) >= 14:
                # LCR? reply (series form): freq, mag1, mag2, |Z|, phase,
                # series_R, series_C, series_L, //R, //C, //L, tan d, Q, reactance
                vals = [float(p) for p in parts[:14]]
                self.rt_value_vars["Frequency_Hz"].set(f"{vals[0]:.4g}")
                self.rt_value_vars["Impedance_Ohm"].set(f"{vals[3]:.4g}")
                self.rt_value_vars["Phase_deg"].set(f"{vals[4]:+.3f}")
                self.rt_value_vars["R_Ohm"].set(f"{vals[5]:.4g}")
                self.rt_value_vars["C_F"].set(f"{vals[6]:.4g}")
                self.rt_value_vars["L_H"].set(f"{vals[7]:.4g}")
                self.rt_value_vars["TanD"].set(f"{vals[11]:.4g}")
                self.rt_value_vars["Q"].set(f"{vals[12]:.4g}")
        except Exception as e:
            self.log(f"Realtime read error: {e}")
        if self.rt_running:
            self.after(500, self._rt_tick)

    # ---------- Keithley tab (Keithley 2400/2401/2410 source meter) ----------
    def _build_keithley_tab(self, parent):
        # Connection block
        conn = ttk.LabelFrame(parent, text="Keithley connection")
        conn.pack(fill="x", padx=6, pady=6)
        for i in range(4):
            conn.columnconfigure(i, weight=1)

        self.kt_iface_var = tk.StringVar(value="VISA")
        self.kt_resource_var = tk.StringVar()
        self.kt_serial_port_var = tk.StringVar()
        self.kt_serial_baud_var = tk.StringVar(value="9600")

        ttk.Label(conn, text="Interface:").grid(row=0, column=0, sticky="e", padx=4)
        iface_cb = ttk.Combobox(conn, textvariable=self.kt_iface_var,
                                 values=["VISA", "Serial (RS-232)"],
                                 state="readonly", width=18)
        iface_cb.grid(row=0, column=1, sticky="w", padx=4)
        Tooltip(iface_cb,
                "VISA: USB-TMC / GPIB / Ethernet via pyvisa - the modern way.\n"
                "Serial: directly via a COM port if the unit has RS-232 enabled.")

        ttk.Label(conn, text="VISA resource:").grid(row=1, column=0, sticky="e", padx=4)
        self.kt_resource_cb = ttk.Combobox(conn, textvariable=self.kt_resource_var,
                                            width=50)
        self.kt_resource_cb.grid(row=1, column=1, columnspan=2, sticky="ew", padx=4)
        ttk.Button(conn, text="Scan VISA",
                   command=self._kt_scan_visa).grid(row=1, column=3, padx=4)
        Tooltip(self.kt_resource_cb,
                "VISA address string. Click 'Scan VISA' to populate the list, "
                "or paste e.g. 'USB0::0x05E6::0x2400::123::INSTR'.")

        ttk.Label(conn, text="Serial port:").grid(row=2, column=0, sticky="e", padx=4)
        self.kt_serial_cb = ttk.Combobox(conn, textvariable=self.kt_serial_port_var,
                                          width=10)
        self.kt_serial_cb.grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(conn, text="Baud:").grid(row=2, column=2, sticky="e")
        ttk.Combobox(conn, textvariable=self.kt_serial_baud_var, width=8,
                     values=["4800", "9600", "19200", "38400", "57600"],
                     state="readonly").grid(row=2, column=3, sticky="ew", padx=4)

        self.btn_kt_connect = ttk.Button(conn, text="Connect",
                                          command=self._kt_connect)
        self.btn_kt_connect.grid(row=3, column=0, sticky="ew", padx=4, pady=4)
        self.btn_kt_disconnect = ttk.Button(conn, text="Disconnect",
                                             command=self._kt_disconnect,
                                             state="disabled")
        self.btn_kt_disconnect.grid(row=3, column=1, sticky="ew", padx=4)
        self.btn_kt_idn = ttk.Button(conn, text="*IDN?",
                                      command=self._kt_idn, state="disabled")
        self.btn_kt_idn.grid(row=3, column=2, sticky="ew", padx=4)
        ttk.Button(conn, text="Refresh COMs",
                   command=self._refresh_ports).grid(row=3, column=3,
                                                     sticky="ew", padx=4)

        # Manual source / measure block
        ctrl = ttk.LabelFrame(parent, text="Manual source / measure")
        ctrl.pack(fill="x", padx=6, pady=6)
        for i in range(6):
            ctrl.columnconfigure(i, weight=1)

        self.kt_src_func_var = tk.StringVar(value="voltage")
        self.kt_level_var = tk.DoubleVar(value=1.0)
        self.kt_compliance_var = tk.DoubleVar(value=0.1)
        self.kt_meas_v_var = tk.StringVar(value="-- V")
        self.kt_meas_i_var = tk.StringVar(value="-- A")

        ttk.Label(ctrl, text="Source:").grid(row=0, column=0, sticky="e")
        sf = ttk.Combobox(ctrl, textvariable=self.kt_src_func_var,
                          values=["voltage", "current"], state="readonly",
                          width=10)
        sf.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(ctrl, text="Level:").grid(row=0, column=2, sticky="e")
        ttk.Entry(ctrl, textvariable=self.kt_level_var, width=10).grid(
            row=0, column=3, sticky="w", padx=4)
        ttk.Label(ctrl, text="Compliance:").grid(row=0, column=4, sticky="e")
        comp_entry = ttk.Entry(ctrl, textvariable=self.kt_compliance_var,
                                width=10)
        comp_entry.grid(row=0, column=5, sticky="w", padx=4)
        Tooltip(comp_entry,
                "Compliance = safety limit on the *measured* quantity. If "
                "sourcing voltage, this is the max current allowed (A). "
                "If sourcing current, this is the max voltage allowed (V).")

        self.btn_kt_output_on = ttk.Button(ctrl, text="Output ON",
                                            command=self._kt_output_on,
                                            state="disabled")
        self.btn_kt_output_on.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        self.btn_kt_output_off = ttk.Button(ctrl, text="Output OFF",
                                             command=self._kt_output_off,
                                             state="disabled")
        self.btn_kt_output_off.grid(row=1, column=1, padx=4, sticky="ew")
        self.btn_kt_read = ttk.Button(ctrl, text="Measure once",
                                       command=self._kt_read,
                                       state="disabled")
        self.btn_kt_read.grid(row=1, column=2, padx=4, sticky="ew")
        ttk.Label(ctrl, text="V:").grid(row=1, column=3, sticky="e")
        ttk.Label(ctrl, textvariable=self.kt_meas_v_var,
                  font=("Segoe UI", 11, "bold")).grid(row=1, column=4,
                                                       sticky="w", padx=4)
        ttk.Label(ctrl, text="I:").grid(row=1, column=5, sticky="e")
        ttk.Label(ctrl, textvariable=self.kt_meas_i_var,
                  font=("Segoe UI", 11, "bold")).grid(row=1, column=6,
                                                       sticky="w", padx=4)

        # IV sweep block
        sw = ttk.LabelFrame(parent, text="IV sweep")
        sw.pack(fill="x", padx=6, pady=6)
        for i in range(8):
            sw.columnconfigure(i, weight=1)

        self.kt_sw_source_var = tk.StringVar(value="voltage")
        self.kt_sw_start_var = tk.DoubleVar(value=0.0)
        self.kt_sw_stop_var = tk.DoubleVar(value=1.0)
        self.kt_sw_points_var = tk.IntVar(value=51)
        self.kt_sw_compliance_var = tk.DoubleVar(value=0.1)
        self.kt_sw_settle_var = tk.DoubleVar(value=0.05)

        ttk.Label(sw, text="Source:").grid(row=0, column=0, sticky="e")
        ttk.Combobox(sw, textvariable=self.kt_sw_source_var,
                     values=["voltage", "current"], state="readonly",
                     width=10).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(sw, text="Start:").grid(row=0, column=2, sticky="e")
        ttk.Entry(sw, textvariable=self.kt_sw_start_var, width=10).grid(
            row=0, column=3, sticky="w")
        ttk.Label(sw, text="Stop:").grid(row=0, column=4, sticky="e")
        ttk.Entry(sw, textvariable=self.kt_sw_stop_var, width=10).grid(
            row=0, column=5, sticky="w")
        ttk.Label(sw, text="Points:").grid(row=0, column=6, sticky="e")
        ttk.Entry(sw, textvariable=self.kt_sw_points_var, width=8).grid(
            row=0, column=7, sticky="w")

        ttk.Label(sw, text="Compliance:").grid(row=1, column=0, sticky="e")
        ttk.Entry(sw, textvariable=self.kt_sw_compliance_var, width=10).grid(
            row=1, column=1, sticky="w")
        ttk.Label(sw, text="Settle (s):").grid(row=1, column=2, sticky="e")
        ttk.Entry(sw, textvariable=self.kt_sw_settle_var, width=8).grid(
            row=1, column=3, sticky="w")

        self.btn_kt_sweep = ttk.Button(sw, text="Run IV sweep",
                                        command=self._kt_run_sweep,
                                        state="disabled")
        self.btn_kt_sweep.grid(row=1, column=5, padx=4, sticky="ew")
        self.btn_kt_export_iv = ttk.Button(sw, text="Export IV to CSV",
                                            command=self._kt_export_iv,
                                            state="disabled")
        self.btn_kt_export_iv.grid(row=1, column=6, columnspan=2, padx=4,
                                    sticky="ew")

        # Stored IV sweeps for plotting / export
        self._iv_sweeps = []

        info = ttk.Label(parent, foreground="#555555", wraplength=900,
                          text=("Keithley 2400-series source meter. Connects via "
                                "USB-TMC (VISA), GPIB, Ethernet or RS-232. Used "
                                "stand-alone for IV characterisation, or "
                                "alongside the PSM for combined LCR+IV runs in "
                                "the Experiment tab."))
        info.pack(fill="x", padx=8, pady=6)

    # ---- Keithley actions ----
    def _kt_scan_visa(self):
        resources = Keithley2400.list_visa_resources()
        self.kt_resource_cb["values"] = resources
        if resources and not self.kt_resource_var.get():
            self.kt_resource_var.set(resources[0])
        self.log(f"VISA scan: {len(resources)} resource(s) found")

    def _kt_set_buttons(self, connected):
        s = "normal" if connected else "disabled"
        self.btn_kt_disconnect.config(state=s)
        self.btn_kt_idn.config(state=s)
        self.btn_kt_output_on.config(state=s)
        self.btn_kt_output_off.config(state=s)
        self.btn_kt_read.config(state=s)
        self.btn_kt_sweep.config(state=s)
        self.btn_kt_connect.config(state="disabled" if connected else "normal")

    def _kt_connect(self):
        iface = self.kt_iface_var.get()
        try:
            if iface == "VISA":
                resource = self.kt_resource_var.get().strip()
                if not resource:
                    messagebox.showerror("Missing resource",
                                         "Pick or paste a VISA resource string.")
                    return
                self.keithley = Keithley2400(resource=resource)
            else:
                port = self.kt_serial_port_var.get().strip()
                if not port:
                    messagebox.showerror("Missing port", "Pick a serial port.")
                    return
                self.keithley = Keithley2400(
                    port=port, baudrate=int(self.kt_serial_baud_var.get()))
            self.keithley.open()
        except Exception as e:
            messagebox.showerror("Keithley connect failed", str(e))
            self.keithley = None
            return
        self._kt_set_buttons(True)
        self.btn_kt_export_iv.config(
            state=("normal" if self._iv_sweeps else "disabled"))
        self.log(f"Keithley opened ({iface})")

    def _kt_disconnect(self):
        if self.keithley:
            try:
                self.keithley.close()
            except Exception:
                pass
            self.keithley = None
        self._kt_set_buttons(False)
        self.kt_meas_v_var.set("-- V")
        self.kt_meas_i_var.set("-- A")
        self.log("Keithley disconnected")

    def _kt_idn(self):
        if not self.keithley:
            return
        try:
            self.log("Keithley *IDN? -> " + self.keithley.identify())
        except Exception as e:
            messagebox.showerror("IDN query failed", str(e))

    def _kt_output_on(self):
        if not self.keithley:
            return
        try:
            func = self.kt_src_func_var.get()
            lvl = float(self.kt_level_var.get())
            comp = float(self.kt_compliance_var.get())
            if func == "voltage":
                self.keithley.configure_source_voltage(lvl, comp)
            else:
                self.keithley.configure_source_current(lvl, comp)
            self.keithley.output_on()
            self.log(f"Keithley output ON ({func}={lvl}, compliance={comp})")
        except Exception as e:
            messagebox.showerror("Output ON failed", str(e))

    def _kt_output_off(self):
        try:
            self.keithley.output_off()
            self.log("Keithley output OFF")
        except Exception as e:
            messagebox.showerror("Output OFF failed", str(e))

    def _kt_read(self):
        try:
            v, i = self.keithley.read()
            self.kt_meas_v_var.set(f"{v:+.5e} V")
            self.kt_meas_i_var.set(f"{i:+.5e} A")
            self.log(f"Keithley READ -> V={v:+.5e} V, I={i:+.5e} A")
        except Exception as e:
            messagebox.showerror("Read failed", str(e))

    def _kt_run_sweep(self):
        if not self.keithley:
            return
        try:
            rows = self.keithley.sweep_iv(
                source=self.kt_sw_source_var.get(),
                start=float(self.kt_sw_start_var.get()),
                stop=float(self.kt_sw_stop_var.get()),
                points=int(self.kt_sw_points_var.get()),
                compliance=float(self.kt_sw_compliance_var.get()),
                settle_s=float(self.kt_sw_settle_var.get()),
                progress_fn=lambda k, n, v, i:
                    self.log(f"  [{k}/{n}] V={v:+.4e}  I={i:+.4e}"),
            )
        except Exception as e:
            messagebox.showerror("IV sweep failed", str(e))
            return
        self._iv_sweeps.append({
            "label": f"sweep {len(self._iv_sweeps) + 1}",
            "rows": rows,
        })
        self.btn_kt_export_iv.config(state="normal")
        self.log(f"IV sweep done - {len(rows)} points captured")

    def _kt_export_iv(self):
        if not self._iv_sweeps:
            messagebox.showinfo("No data", "Run an IV sweep first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export IV to CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("Sweep,Point,V,I,R\n")
                for s in self._iv_sweeps:
                    for j, r in enumerate(s["rows"]):
                        f.write(f"{s['label']},{j},{r['V']},{r['I']},"
                                f"{'' if r['R'] is None else r['R']}\n")
            self.log(f"IV CSV exported to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ---------- Furnace tab (DPI-1100 / BTC-9100 over Modbus RTU) ----------
    def _build_furnace_tab(self, parent):
        # Connection row
        conn = ttk.LabelFrame(parent, text="Furnace connection")
        conn.pack(fill="x", padx=6, pady=6)
        for i in range(6):
            conn.columnconfigure(i, weight=1)

        self.furn_port_var = tk.StringVar()
        self.furn_baud_var = tk.StringVar(value="9600")
        self.furn_slave_var = tk.IntVar(value=1)
        self.furn_parity_var = tk.StringVar(value="N")

        ttk.Label(conn, text="Port:").grid(row=0, column=0, sticky="e", padx=4)
        self.furn_port_cb = ttk.Combobox(conn, textvariable=self.furn_port_var,
                                          width=10)
        self.furn_port_cb.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(conn, text="Baud:").grid(row=0, column=2, sticky="e")
        ttk.Combobox(conn, textvariable=self.furn_baud_var,
                     values=["2400", "4800", "9600", "14400",
                             "19200", "28800", "38400"],
                     width=8, state="readonly").grid(
                     row=0, column=3, sticky="ew", padx=4)
        ttk.Label(conn, text="Slave:").grid(row=0, column=4, sticky="e")
        ttk.Entry(conn, textvariable=self.furn_slave_var, width=5).grid(
            row=0, column=5, sticky="w", padx=4)
        ttk.Label(conn, text="Parity:").grid(row=1, column=0, sticky="e",
                                              padx=4)
        ttk.Combobox(conn, textvariable=self.furn_parity_var,
                     values=["N", "E", "O"], width=4,
                     state="readonly").grid(row=1, column=1, sticky="w", padx=4)

        self.btn_furn_connect = ttk.Button(conn, text="Connect",
                                            command=self._furn_connect)
        self.btn_furn_connect.grid(row=1, column=2, sticky="ew", padx=4, pady=4)
        self.btn_furn_disconnect = ttk.Button(conn, text="Disconnect",
                                               command=self._furn_disconnect,
                                               state="disabled")
        self.btn_furn_disconnect.grid(row=1, column=3, sticky="ew", padx=4)
        ttk.Button(conn, text="Refresh ports",
                   command=self._refresh_ports).grid(
                   row=1, column=4, sticky="ew", padx=4)

        # Live readout
        live = ttk.LabelFrame(parent, text="Live readout")
        live.pack(fill="x", padx=6, pady=6)
        for i in range(4):
            live.columnconfigure(i, weight=1)
        self.furn_pv_var = tk.StringVar(value="-- C")
        self.furn_sv_var = tk.StringVar(value="-- C")
        self.furn_mode_var = tk.StringVar(value="--")
        ttk.Label(live, text="PV (current):").grid(row=0, column=0, sticky="e")
        ttk.Label(live, textvariable=self.furn_pv_var,
                  font=("Segoe UI", 16, "bold"),
                  foreground="#1a4f8a").grid(row=0, column=1, sticky="w")
        ttk.Label(live, text="SV (active SP):").grid(row=0, column=2,
                                                       sticky="e")
        ttk.Label(live, textvariable=self.furn_sv_var,
                  font=("Segoe UI", 14)).grid(row=0, column=3, sticky="w")
        ttk.Label(live, text="Mode/alarm:").grid(row=1, column=0, sticky="e")
        ttk.Label(live, textvariable=self.furn_mode_var).grid(
            row=1, column=1, sticky="w")

        # Setpoint control
        ctrl = ttk.LabelFrame(parent, text="Setpoint control (SP1)")
        ctrl.pack(fill="x", padx=6, pady=6)
        for i in range(6):
            ctrl.columnconfigure(i, weight=1)
        self.furn_sp_var = tk.DoubleVar(value=25.0)
        ttk.Label(ctrl, text="Target:").grid(row=0, column=0, sticky="e")
        ttk.Entry(ctrl, textvariable=self.furn_sp_var, width=10).grid(
            row=0, column=1, sticky="w", padx=4)
        ttk.Label(ctrl, text="C").grid(row=0, column=2, sticky="w")
        self.btn_furn_send_sp = ttk.Button(ctrl, text="Write SP1",
                                            command=self._furn_write_sp,
                                            state="disabled")
        self.btn_furn_send_sp.grid(row=0, column=3, sticky="ew", padx=4)
        self.btn_furn_reset = ttk.Button(ctrl, text="Reset",
                                          command=self._furn_reset,
                                          state="disabled")
        self.btn_furn_reset.grid(row=0, column=4, sticky="ew", padx=4)
        self.btn_furn_autotune = ttk.Button(ctrl, text="Auto-tune",
                                             command=self._furn_autotune,
                                             state="disabled")
        self.btn_furn_autotune.grid(row=0, column=5, sticky="ew", padx=4)

        info = ttk.Label(
            parent,
            text=("Brainchild BTC-9100 PID controller inside Divya DPI-1100. "
                  "Modbus RTU at the configured baud rate / slave address. "
                  "DPI-1100 working range is 50-650 C; the SP entry is "
                  "clamped to that range when sent."),
            wraplength=900, foreground="#555555")
        info.pack(fill="x", padx=8, pady=6)

    def _furn_set_buttons(self, connected):
        s = "normal" if connected else "disabled"
        self.btn_furn_disconnect.config(state=s)
        self.btn_furn_send_sp.config(state=s)
        self.btn_furn_reset.config(state=s)
        self.btn_furn_autotune.config(state=s)
        self.btn_furn_connect.config(state="disabled" if connected else "normal")

    def _furn_connect(self):
        port = self.furn_port_var.get().strip()
        if not port:
            messagebox.showerror("Missing port", "Pick a furnace COM port.")
            return
        try:
            self.furnace = DPI1100(
                port=port,
                baudrate=int(self.furn_baud_var.get()),
                slave=int(self.furn_slave_var.get()),
                parity=self.furn_parity_var.get(),
            )
            self.furnace.open()
        except Exception as e:
            messagebox.showerror("Furnace connect failed", str(e))
            self.furnace = None
            return
        self._furn_set_buttons(True)
        self.log(f"Furnace opened on {port} @ {self.furn_baud_var.get()} baud, "
                 f"slave {self.furn_slave_var.get()}")
        self._furn_tick()

    def _furn_disconnect(self):
        if self.furnace:
            try:
                self.furnace.close()
            except Exception:
                pass
            self.furnace = None
        self._furn_set_buttons(False)
        self.furn_pv_var.set("-- C")
        self.furn_sv_var.set("-- C")
        self.furn_mode_var.set("--")
        self.log("Furnace disconnected")

    def _furn_tick(self):
        if not self.furnace or not self.furnace.is_open():
            return
        try:
            pv, sv, mode = self.furnace.status_snapshot()
            self.furn_pv_var.set(f"{pv:+.2f} C")
            self.furn_sv_var.set(f"{sv:+.2f} C")
            alarm = "ALARM" if (mode & 0x000F) else "ok"
            top = (mode >> 4) & 0x000F
            mode_name = {0: "Normal", 1: "Cal", 2: "Autotune",
                          3: "Manual", 4: "Failure"}.get(top, f"0x{mode:04X}")
            self.furn_mode_var.set(f"{mode_name} / {alarm}")
        except Exception as e:
            self.log(f"Furnace poll error: {e}")
        self.after(1500, self._furn_tick)

    def _furn_write_sp(self):
        if not self.furnace:
            return
        try:
            t = float(self.furn_sp_var.get())
            t = max(50.0, min(650.0, t))   # clamp to DPI-1100 working range
            self.furnace.write_sp1(t)
            self.log(f"Furnace SP1 written: {t:+.2f} C")
        except Exception as e:
            messagebox.showerror("Furnace SP write failed", str(e))

    def _furn_reset(self):
        try:
            self.furnace.reset()
            self.log("Furnace Reset command sent")
        except Exception as e:
            messagebox.showerror("Furnace Reset failed", str(e))

    def _furn_autotune(self):
        try:
            self.furnace.autotune()
            self.log("Furnace Auto-tune started")
        except Exception as e:
            messagebox.showerror("Furnace Auto-tune failed", str(e))

    def _build_psm_tab(self, parent):
        # Make the whole PSM Configuration tab scrollable — with this many
        # sections it won't fit on one screen.
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(win_id, width=e.width)
        )
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # mouse-wheel scrolling
        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _wheel)
        parent = inner   # the rest of the function builds inside `parent`

        # Sweep
        sweep = ttk.LabelFrame(parent, text="Sweep")
        sweep.pack(fill="x", padx=6, pady=6)
        sweep.columnconfigure(1, weight=1)

        self.sweep_start_var = tk.DoubleVar(value=SWEEP_DEFAULTS["sweep_start_hz"])
        self.sweep_end_var = tk.DoubleVar(value=SWEEP_DEFAULTS["sweep_end_hz"])
        self.sweep_steps_var = tk.IntVar(value=SWEEP_DEFAULTS["sweep_steps"])
        self.sweep_step_scale_var = tk.StringVar(value="LOGARI")
        self.sweep_type_var = tk.StringVar(value="SINGLE")
        self.sweep_gen_after_var = tk.StringVar(value="OFF")
        self.sweep_graph1_var = tk.StringVar(value="AUTO")
        self.sweep_graph2_var = tk.StringVar(value="AUTO")
        self.sweep_marker_var = tk.StringVar(value="OFF")
        self.sweep_marker_freq_var = tk.DoubleVar(value=1000.0)

        add_row(sweep, 0, "Start frequency:",
                ttk.Entry(sweep, textvariable=self.sweep_start_var),
                "Lower frequency of the sweep, in Hz. Range 10 uHz to 35 MHz "
                "for the PSM1735. Default 1e3 (1 kHz).", "Hz")
        add_row(sweep, 1, "End frequency:",
                ttk.Entry(sweep, textvariable=self.sweep_end_var),
                "Upper frequency of the sweep, in Hz. Default 1e6 (1 MHz).", "Hz")
        add_row(sweep, 2, "Steps:",
                ttk.Entry(sweep, textvariable=self.sweep_steps_var),
                "Number of frequency points in the sweep. Default 32.")
        add_row(sweep, 3, "Step scale:",
                ttk.Combobox(sweep, textvariable=self.sweep_step_scale_var,
                             values=["LOGARI", "LINEAR"], state="readonly"),
                "How frequency points are spaced (FSWEEP 4th argument). "
                "LOGARI = log-spaced (matches the PSM 'Logarithmic' "
                "default). LINEAR = evenly spaced in Hz.")
        add_row(sweep, 4, "Type:",
                ttk.Combobox(sweep, textvariable=self.sweep_type_var,
                             values=["SINGLE", "REPEAT"], state="readonly"),
                "Sweep type (CONFIG 21). SINGLE: run one sweep per "
                "trigger. REPEAT: keep restarting after each completion.")
        add_row(sweep, 5, "Gen. when complete:",
                ttk.Combobox(sweep, textvariable=self.sweep_gen_after_var,
                             values=["OFF", "ON"], state="readonly"),
                "What the generator does after the sweep finishes "
                "(CONFIG 55). OFF = stop output; ON = keep output "
                "running at the last sweep frequency.")
        add_row(sweep, 6, "Graph 1 scaling:",
                ttk.Combobox(sweep, textvariable=self.sweep_graph1_var,
                             values=["AUTO", "MANUAL"], state="readonly"),
                "Y-axis scaling of the PSM screen's primary graph "
                "(CONFIG 193). AUTO = autoscale; MANUAL = use the limits "
                "set on the PSM. Cosmetic for the PSM display only.")
        add_row(sweep, 7, "Graph 2 scaling:",
                ttk.Combobox(sweep, textvariable=self.sweep_graph2_var,
                             values=["AUTO", "MANUAL"], state="readonly"),
                "Y-axis scaling of the PSM screen's secondary trace "
                "(CONFIG 173). Cosmetic for the PSM display only.")
        add_row(sweep, 8, "Frequency marker:",
                ttk.Combobox(sweep, textvariable=self.sweep_marker_var,
                             values=["OFF", "ON"], state="readonly"),
                "Cursor at a specific frequency on the PSM screen "
                "(MARKER). Useful for spotting a resonance.")
        add_row(sweep, 9, "Marker frequency:",
                ttk.Entry(sweep, textvariable=self.sweep_marker_freq_var),
                "Only used when Frequency marker is ON.", "Hz")
        ttk.Button(sweep, text="Reset to default (1 kHz - 1 MHz, 32 log)",
                   command=self._reset_sweep_defaults).grid(
            row=10, column=0, columnspan=3, padx=4, pady=4, sticky="w")

        # OUT — combined Generator + extras (matches PSMComm2 OUT panel)
        out = ttk.LabelFrame(parent, text="OUT (generator output)")
        out.pack(fill="x", padx=6, pady=6)
        out.columnconfigure(1, weight=1)

        self.amp_var = tk.DoubleVar(value=2.0)
        self.offset_var = tk.DoubleVar(value=0.0)
        self.out_freq_var = tk.DoubleVar(value=1000.0)
        self.out_waveform_var = tk.StringVar(value="SINEWA")
        self.out_freq_step_var = tk.DoubleVar(value=2.0)
        self.out_amp_step_var = tk.DoubleVar(value=1.1)
        self.out_ceiling_var = tk.DoubleVar(value=10.0)
        self.out_output_on_var = tk.StringVar(value="OFF")

        add_row(out, 0, "Amplitude:",
                ttk.Entry(out, textvariable=self.amp_var),
                "Peak amplitude of the waveform (AMPLIT). In dBm if "
                "SYS Control mode = DBM.", "V peak")
        add_row(out, 1, "Ceiling:",
                ttk.Entry(out, textvariable=self.out_ceiling_var),
                "Maximum allowed amplitude in dBm mode (CONFIG 54). "
                "Ignored in Volts mode. Default 10 dBm.", "dBm")
        add_row(out, 2, "Frequency:",
                ttk.Entry(out, textvariable=self.out_freq_var),
                "Generator frequency (FREQUE) when not sweeping.", "Hz")
        add_row(out, 3, "Offset:",
                ttk.Entry(out, textvariable=self.offset_var),
                "DC offset applied to the generator output (OFFSET). "
                "Default 0 V.", "V")
        add_row(out, 4, "Waveform:",
                ttk.Combobox(out, textvariable=self.out_waveform_var,
                             values=["SINEWA", "SQUARE", "TRIANG",
                                     "LEADIN", "TRAILI"],
                             state="readonly"),
                "Generator output waveform (WAVEFO). Sine is required "
                "for any LCR measurement.")
        add_row(out, 5, "Amplitude step:",
                ttk.Entry(out, textvariable=self.out_amp_step_var),
                "Multiplier used when nudging amplitude up/down from "
                "the PSM front panel (CONFIG 53). Default 1.1.")
        add_row(out, 6, "Frequency step:",
                ttk.Entry(out, textvariable=self.out_freq_step_var),
                "Multiplier used when nudging frequency up/down from "
                "the PSM front panel (CONFIG 52). Default 2.0.")
        add_row(out, 7, "Output:",
                ttk.Combobox(out, textvariable=self.out_output_on_var,
                             values=["OFF", "ON"], state="readonly"),
                "Generator master enable (OUTPUT,ON / OUTPUT,OFF). "
                "Leave OFF here — the experiment loop turns it on at "
                "each setpoint automatically. Switch to ON only for "
                "manual realtime work.")

        # CH1
        ch1 = ttk.LabelFrame(parent, text="CH1 (Input 1)")
        ch1.pack(fill="x", padx=6, pady=6)
        ch1.columnconfigure(1, weight=1)
        self.ch1_input_var = tk.StringVar(value="VOLTAGE")
        self.ch1_conn_var = tk.StringVar(value="MAIN")
        self.ch1_minrange_var = tk.StringVar(value="10mV")
        self.ch1_ranging_var = tk.StringVar(value="AUTO")
        self.ch1_coupling_var = tk.StringVar(value="AC+DC")
        self.ch1_scale_var = tk.DoubleVar(value=1.0)
        self._build_channel(ch1, "CH1", self.ch1_input_var, self.ch1_conn_var,
                            self.ch1_minrange_var, self.ch1_ranging_var,
                            self.ch1_coupling_var, self.ch1_scale_var,
                            shunt_var=None)

        # CH2
        ch2 = ttk.LabelFrame(parent, text="CH2 (Input 2)")
        ch2.pack(fill="x", padx=6, pady=6)
        ch2.columnconfigure(1, weight=1)
        self.ch2_input_var = tk.StringVar(value="SHUNT")
        self.ch2_conn_var = tk.StringVar(value="MAIN")
        self.ch2_minrange_var = tk.StringVar(value="10mV")
        self.ch2_ranging_var = tk.StringVar(value="AUTO")
        self.ch2_coupling_var = tk.StringVar(value="AC+DC")
        self.ch2_scale_var = tk.DoubleVar(value=1.0)
        self.ch2_shunt_var = tk.DoubleVar(value=50.0)
        self._build_channel(ch2, "CH2", self.ch2_input_var, self.ch2_conn_var,
                            self.ch2_minrange_var, self.ch2_ranging_var,
                            self.ch2_coupling_var, self.ch2_scale_var,
                            shunt_var=self.ch2_shunt_var)

        # AUX
        aux = ttk.LabelFrame(parent, text="AUX")
        aux.pack(fill="x", padx=6, pady=6)
        aux.columnconfigure(1, weight=1)
        self.fixture_var = tk.StringVar(value="IAI")
        self.head_shunt_var = tk.StringVar(value="NORMAL")
        add_row(aux, 0, "Fixture:",
                ttk.Combobox(aux, textvariable=self.fixture_var,
                             values=["NONE", "IAI", "ACTIVE_HEAD",
                                     "TAF01", "TAF02"],
                             state="readonly"),
                "Test fixture attached to the PSM AUX port.\n"
                " - IAI: Impedance Analyser Interface (the unit shown in your "
                "screenshots).\n"
                " - ACTIVE_HEAD: N4L LCR Active Head (different accessory).\n"
                " - TAF01/02: transformer test fixtures.\n"
                " - NONE: bare BNC inputs only.")
        # LCR head shunt is no longer exposed here — PSMComm2's AUX panel
        # only shows the Fixture. The shunt level is still sent in
        # apply_config using the hidden head_shunt_var default (NORMAL).

        # MODE — mirrors the PSMComm2 "Configuration: LCR Meter" panel
        modef = ttk.LabelFrame(parent, text="MODE")
        modef.pack(fill="x", padx=6, pady=6)
        modef.columnconfigure(1, weight=1)
        self.op_mode_var = tk.StringVar(value="LCR")
        self.lcr_param_var = tk.StringVar(value="AUTO")
        self.lcr_cond_var = tk.StringVar(value="MANUAL")
        self.lcr_sweep_model_var = tk.StringVar(value="PARALLEL")
        self.lcr_shunt_var = tk.StringVar(value="DEFAULT")
        self.lcr_connection_var = tk.StringVar(value="SHUNT")
        self.lcr_graph_var = tk.StringVar(value="TAND_QF")

        add_row(modef, 0, "Operation mode:",
                ttk.Combobox(modef, textvariable=self.op_mode_var,
                             values=["LCR", "GAINPH", "VECTOR", "VRMS",
                                     "POWER", "HARMON", "TXA", "SIGGEN"],
                             state="readonly"),
                "Top-level instrument mode (PSM 'MODE' command).\n"
                " - LCR: LCR meter (default for this experiment).\n"
                " - GAINPH: Frequency Response / gain-phase analyser.\n"
                " - VECTOR: vector voltmeter / phase angle voltmeter.\n"
                " - VRMS: true-RMS voltmeter.\n"
                " - POWER: power meter.\n"
                " - HARMON: harmonic analyser.\n"
                " - TXA: transformer analyser.\n"
                " - SIGGEN: signal generator only.")
        add_row(modef, 1, "Parameter:",
                ttk.Combobox(modef, textvariable=self.lcr_param_var,
                             values=["AUTO", "CAPACITANCE", "INDUCTANCE",
                                     "IMPEDANCE", "ADMITTANCE"],
                             state="readonly"),
                "Primary parameter the LCR meter optimises for.\n"
                " - AUTO: PSM picks the best for the DUT (matches the "
                "default in PSMComm2).\n"
                " - CAPACITANCE / INDUCTANCE / IMPEDANCE / ADMITTANCE: "
                "force a specific quantity.")
        add_row(modef, 2, "Condition:",
                ttk.Combobox(modef, textvariable=self.lcr_cond_var,
                             values=["AUTO_FREQ", "MANUAL", "AUTO_SHUNT"],
                             state="readonly"),
                "How the LCR meter sets up each measurement (CONFIG 22).\n"
                " - AUTO_FREQ: PSM auto-selects measurement frequency.\n"
                " - MANUAL: use the frequency / drive level you set "
                "above (right choice when running a frequency sweep).\n"
                " - AUTO_SHUNT: PSM auto-selects the LCR-head shunt.")
        add_row(modef, 3, "Sweep model:",
                ttk.Combobox(modef, textvariable=self.lcr_sweep_model_var,
                             values=["SERIES", "PARALLEL"], state="readonly"),
                "Equivalent-circuit model used to derive L / C / R from the "
                "measured impedance (CONFIG 138). PARALLEL matches the "
                "'Sweep: Parallel' setting from your PSM screen.")
        # Shunt selector lives in SYS now (per the PSMComm2 layout).
        add_row(modef, 5, "Connection:",
                ttk.Combobox(modef, textvariable=self.lcr_connection_var,
                             values=["SHUNT", "DIVIDER_LOW", "DIVIDER_HIGH"],
                             state="readonly"),
                "How the DUT is connected to the front panel (CONFIG 145).\n"
                " - SHUNT: current shunt across CH2 (your setup).\n"
                " - DIVIDER_LOW: Zx in low side of voltage divider.\n"
                " - DIVIDER_HIGH: Zx in high side.")
        add_row(modef, 6, "Graph display:",
                ttk.Combobox(modef, textvariable=self.lcr_graph_var,
                             values=["SINGLE", "TAND_QF", "RESISTANCE"],
                             state="readonly"),
                "What the PSM's secondary graph trace shows (CONFIG 139). "
                "TAND_QF matches the screen you sent. Pure cosmetic — "
                "doesn't affect the data we log.")

        # =============== ACQU ===============
        acqu = ttk.LabelFrame(parent, text="ACQU (acquisition control)")
        acqu.pack(fill="x", padx=6, pady=6)
        acqu.columnconfigure(1, weight=1)
        self.acqu_speed_var = tk.StringVar(value="MEDIUM")
        self.acqu_window_var = tk.DoubleVar(value=0.1)
        self.acqu_mincycles_var = tk.IntVar(value=1)
        self.acqu_delay_var = tk.DoubleVar(value=0.0)
        self.acqu_phaseref_var = tk.StringVar(value="CH1")
        self.acqu_filter_var = tk.StringVar(value="NORMAL")
        self.acqu_filter_dyn_var = tk.StringVar(value="AUTO")
        self.acqu_lowfreq_var = tk.BooleanVar(value=False)
        self.acqu_datalog_var = tk.StringVar(value="DISABLE")
        self.acqu_datalog_int_var = tk.DoubleVar(value=1.0)
        self.acqu_bandwidth_var = tk.StringVar(value="AUTO")
        add_row(acqu, 0, "Speed:",
                ttk.Combobox(acqu, textvariable=self.acqu_speed_var,
                             values=["FAST", "MEDIUM", "SLOW", "VSLOW"],
                             state="readonly"),
                "Measurement speed (CONFIG 13 / SPEED).\n"
                " - FAST: short window, fastest, noisiest.\n"
                " - MEDIUM (default): balanced.\n"
                " - SLOW / VSLOW: longer averaging, quieter.")
        add_row(acqu, 2, "Min cycles:",
                ttk.Entry(acqu, textvariable=self.acqu_mincycles_var),
                "Minimum number of signal cycles per measurement window "
                "(CYCLES). Extends the window at low frequencies. 1 = use "
                "whatever Speed dictates.")
        add_row(acqu, 3, "Inter-step delay:",
                ttk.Entry(acqu, textvariable=self.acqu_delay_var),
                "Settling time inserted between frequency steps during a "
                "sweep (DELAY). Use a positive value if your DUT needs "
                "time to settle after a frequency change.", "s")
        add_row(acqu, 4, "Phase reference:",
                ttk.Combobox(acqu, textvariable=self.acqu_phaseref_var,
                             values=["CH1", "CH2"], state="readonly"),
                "Which channel is the phase reference (PHREF). CH1 = "
                "phase of CH2 relative to CH1; CH2 = phase of CH1 "
                "relative to CH2.")
        add_row(acqu, 5, "Filter:",
                ttk.Combobox(acqu, textvariable=self.acqu_filter_var,
                             values=["NONE", "NORMAL", "SLOW"],
                             state="readonly"),
                "Output filter time constant (FILTER). SLOW is for very "
                "noisy / slowly varying signals; NORMAL is the default; "
                "NONE disables filtering.")
        add_row(acqu, 6, "Filter dynamics:",
                ttk.Combobox(acqu, textvariable=self.acqu_filter_dyn_var,
                             values=["AUTO", "FIXED"], state="readonly"),
                "How the filter responds to changes. AUTO resets on big "
                "input changes (faster settling); FIXED uses a constant "
                "time-constant.")
        add_row(acqu, 7, "Low frequency mode:",
                ttk.Checkbutton(acqu, variable=self.acqu_lowfreq_var),
                "LOWFRE ON applies extra digital filtering useful below "
                "a few hundred Hz. Leave OFF for the 1 kHz - 1 MHz "
                "default sweep.")
        add_row(acqu, 8, "Datalog:",
                ttk.Combobox(acqu, textvariable=self.acqu_datalog_var,
                             values=["DISABLE", "RAM", "NONVOL"],
                             state="readonly"),
                "On-instrument data logger (DATALO). DISABLE = off "
                "(default — we log to Excel ourselves). RAM = volatile "
                "PSM memory. NONVOL = non-volatile PSM store.")
        add_row(acqu, 9, "Datalog interval:",
                ttk.Entry(acqu, textvariable=self.acqu_datalog_int_var),
                "Only used when Datalog != DISABLE. Seconds between "
                "logger samples on the PSM.", "s")
        add_row(acqu, 10, "Bandwidth:",
                ttk.Combobox(acqu, textvariable=self.acqu_bandwidth_var,
                             values=["AUTO", "WIDE", "LOW"], state="readonly"),
                "Selective vs wideband measurement (BANDWI). AUTO is the "
                "default. WIDE disables the heterodyning filter (caps "
                "freq at 1 MHz). LOW restricts to low frequency mode.")

        # =============== TRIM ===============
        trim = ttk.LabelFrame(parent, text="TRIM (amplitude compression / AC trim)")
        trim.pack(fill="x", padx=6, pady=6)
        trim.columnconfigure(1, weight=1)
        self.trim_channel_var = tk.StringVar(value="DISABL")
        # Trim level / tolerance not exposed in the UI per PSMComm2's TRIM
        # panel. Kept as hidden defaults sent only when channel != DISABL.
        self.trim_level_var = tk.DoubleVar(value=1.0)
        self.trim_tol_var = tk.DoubleVar(value=5.0)
        add_row(trim, 0, "AC trim:",
                ttk.Combobox(trim, textvariable=self.trim_channel_var,
                             values=["DISABL", "CH1", "CH2"],
                             state="readonly"),
                "AC trim (amplitude compression) target (ACTRIM). The PSM "
                "adjusts the generator amplitude so the chosen input "
                "channel reads the requested level. DISABL = off (use "
                "fixed output amplitude).")

        # =============== SYS ===============
        sys = ttk.LabelFrame(parent, text="SYS (system options)")
        sys.pack(fill="x", padx=6, pady=6)
        sys.columnconfigure(1, weight=1)
        self.sys_phconv_var = tk.StringVar(value="180")
        self.sys_length_var = tk.StringVar(value="M")
        self.sys_lowblank_var = tk.BooleanVar(value=False)
        self.sys_graph_var = tk.StringVar(value="LINES")
        self.sys_shunt_var = tk.StringVar(value="DEFAULT")
        self.sys_step_msg_var = tk.BooleanVar(value=True)
        self.sys_prog_direct_var = tk.BooleanVar(value=False)
        self.sys_control_var = tk.StringVar(value="VOLTS")
        self.sys_kbd_beep_var = tk.BooleanVar(value=True)
        self.sys_autozero_var = tk.BooleanVar(value=True)
        add_row(sys, 0, "Phase convention:",
                ttk.Combobox(sys, textvariable=self.sys_phconv_var,
                             values=["180", "-360", "+360"],
                             state="readonly"),
                "How phase is reported (PHCONV).\n"
                " - 180: -180 deg to +180 deg (default in PSMComm2).\n"
                " - -360: 0 deg to -360 deg.\n"
                " - +360: 0 deg to +360 deg.")
        add_row(sys, 1, "Length unit:",
                ttk.Combobox(sys, textvariable=self.sys_length_var,
                             values=["M", "INCH"], state="readonly"),
                "Units used in LVDT mode (CONFIG 119). Cosmetic "
                "elsewhere.")
        add_row(sys, 2, "Low blanking:",
                ttk.Checkbutton(sys, variable=self.sys_lowblank_var),
                "Suppress display of very small values (BLANKI). "
                "Useful when measuring near zero. OFF by default.")
        add_row(sys, 3, "Graph style:",
                ttk.Combobox(sys, textvariable=self.sys_graph_var,
                             values=["DOTS", "LINES"], state="readonly"),
                "How the PSM screen draws the graph (CONFIG 8). "
                "LINES is the default.")
        add_row(sys, 4, "Shunt:",
                ttk.Combobox(sys, textvariable=self.sys_shunt_var,
                             values=["DEFAULT", "MANUAL"], state="readonly"),
                "Current shunt source (CONFIG 23). DEFAULT = let the PSM "
                "auto-pick. MANUAL = use the value set in the CH2 "
                "External shunt field.")
        add_row(sys, 5, "Step message:",
                ttk.Checkbutton(sys, variable=self.sys_step_msg_var),
                "Show 'Step n of N' messages during a sweep on the PSM "
                "display (CONFIG 117).")
        add_row(sys, 6, "Prog 1-6 direct:",
                ttk.Checkbutton(sys, variable=self.sys_prog_direct_var),
                "Front-panel PROG 1-6 keys load setups directly without "
                "a confirm prompt (CONFIG 66). Off by default to avoid "
                "accidental config changes.")
        add_row(sys, 7, "Control:",
                ttk.Combobox(sys, textvariable=self.sys_control_var,
                             values=["VOLTS", "DBM"], state="readonly"),
                "How output amplitude is specified (CONFIG 116). VOLTS "
                "is the default for LCR work. DBM mode lets you set the "
                "amplitude in dBm and enables the OUT Ceiling field above.")
        add_row(sys, 8, "Keyboard beep:",
                ttk.Checkbutton(sys, variable=self.sys_kbd_beep_var),
                "Beep on PSM front-panel key presses (CONFIG 9). "
                "On by default.")
        add_row(sys, 9, "Autozero:",
                ttk.Checkbutton(sys, variable=self.sys_autozero_var),
                "Automatic offset zeroing (CONFIG 4). On = the PSM "
                "periodically re-zeros itself; Off = manual zero only.")

    def _build_channel(self, parent, name, input_var, conn_var, minrange_var,
                       ranging_var, coupling_var, scale_var, shunt_var):
        add_row(parent, 0, "Input type:",
                ttk.Combobox(parent, textvariable=input_var,
                             values=["DISABLE", "VOLTAGE", "SHUNT"],
                             state="readonly"),
                f"{name} input mode. VOLTAGE measures the signal directly "
                "(use for CH1). SHUNT divides by an external shunt resistor "
                "to read current (use for CH2 when wired to the shunt).")
        add_row(parent, 1, "Connection:",
                ttk.Combobox(parent, textvariable=conn_var,
                             values=["MAIN", "SECOND", "DIFFER"],
                             state="readonly"),
                "Physical input used. MAIN = right BNC (default for IAI "
                "fixture). SECOND = left. DIFFER = differential.")
        add_row(parent, 2, "Minimum range:",
                ttk.Combobox(parent, textvariable=minrange_var,
                             values=["1mV", "3mV", "10mV", "30mV", "100mV",
                                     "300mV", "1V", "3V", "10V"],
                             state="readonly"),
                "Smallest input range the autoranger is allowed to use. "
                "Default 10 mV matches the screen capture.")
        add_row(parent, 3, "Autoranging:",
                ttk.Combobox(parent, textvariable=ranging_var,
                             values=["AUTO", "UPAUTO", "MANUAL"],
                             state="readonly"),
                "AUTO = full autorange (default). UPAUTO = only switches "
                "up. MANUAL = locked to the minimum range above.")
        add_row(parent, 4, "Coupling:",
                ttk.Combobox(parent, textvariable=coupling_var,
                             values=["AC+DC", "ACONLY"], state="readonly"),
                "Input coupling. AC+DC matches the screen capture.")
        add_row(parent, 5, "Scale factor:",
                ttk.Entry(parent, textvariable=scale_var),
                "Multiplier applied to the reading (e.g. for a 10:1 probe). "
                "Default 1.0.")
        if shunt_var is not None:
            add_row(parent, 6, "External shunt:",
                    ttk.Entry(parent, textvariable=shunt_var),
                    "Resistance of the external shunt in ohms, used when "
                    "this channel is in SHUNT mode. The PSM screen capture "
                    "shows 50 ohm.", "ohm")

    # ---------- helpers ----------
    def log(self, msg):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.psm_port_cb["values"] = ports
        self.ard_port_cb["values"] = ports
        if hasattr(self, "furn_port_cb"):
            self.furn_port_cb["values"] = ports
        if hasattr(self, "kt_serial_cb"):
            self.kt_serial_cb["values"] = ports
        if not self.psm_port_cb.get() and "COM12" in ports:
            self.psm_port_cb.set("COM12")
        if not self.ard_port_cb.get() and "COM11" in ports:
            self.ard_port_cb.set("COM11")

    def _reset_sweep_defaults(self):
        self.sweep_start_var.set(SWEEP_DEFAULTS["sweep_start_hz"])
        self.sweep_end_var.set(SWEEP_DEFAULTS["sweep_end_hz"])
        self.sweep_steps_var.set(SWEEP_DEFAULTS["sweep_steps"])
        self.sweep_step_scale_var.set(
            "LOGARI" if SWEEP_DEFAULTS["sweep_log"] else "LINEAR")

    def _choose_outfile(self):
        path = filedialog.asksaveasfilename(
            title="Save Excel workbook as",
            defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx")],
        )
        if path:
            self.outfile_var.set(path)

    def _set_connected(self, ok):
        # Buttons that depend only on the PSM being connected
        psm_state = "normal" if ok else "disabled"
        self.btn_disconnect.config(state=psm_state)
        self.btn_idn.config(state=psm_state)
        self.btn_apply.config(state=psm_state)
        self.btn_output_on.config(state=psm_state)
        self.btn_output_off.config(state=psm_state)
        self.btn_start.config(state=psm_state)
        if hasattr(self, "btn_rt_start"):
            self.btn_rt_start.config(state=psm_state)
        # Heater-control buttons require Arduino to be connected
        ard_state = "normal" if (ok and self.temp is not None) else "disabled"
        self.btn_set_target.config(state=ard_state)
        self.btn_heater_on.config(state=ard_state)
        self.btn_heater_off.config(state=ard_state)
        self.btn_heater_auto.config(state=ard_state)
        self.btn_connect.config(state="disabled" if ok else "normal")

    # ---------- actions ----------
    def _connect(self):
        psm_port = self.psm_port_cb.get().strip()
        ard_port = self.ard_port_cb.get().strip()
        if not psm_port:
            messagebox.showerror("Missing port", "Pick a PSM COM port.")
            return
        try:
            self.psm = PSM1735(psm_port, baudrate=int(self.psm_baud_cb.get()))
            self.psm.open()
            self.log(f"PSM opened on {psm_port}")
        except Exception as e:
            messagebox.showerror("PSM connect failed", str(e))
            self.psm = None
            return
        # Arduino is now OPTIONAL - skip silently if no port picked.
        # The experiment loop will refuse to start in 'Arduino' heat-source
        # mode if it's not connected, but other modes (Furnace, None) work
        # fine without it.
        if ard_port:
            try:
                self.temp = TempReader(ard_port,
                                       baudrate=int(self.ard_baud_cb.get()))
                self.temp.open()
                self.log(f"Arduino opened on {ard_port}")
            except Exception as e:
                self.log(f"Arduino connect FAILED ({e}). Heat source "
                         "'Arduino + relay heater' will be unavailable; other "
                         "modes still work.")
                self.temp = None
        else:
            self.temp = None
            self.log("Arduino port not picked - Arduino heat source mode "
                     "will be unavailable.")
        self._set_connected(True)
        ard_status = f"Arduino={ard_port}" if self.temp else "Arduino=skipped"
        self.status_var.set(f"Connected: PSM={psm_port}, {ard_status}")

    def _disconnect(self):
        if self.experiment and self.experiment.is_running():
            self.experiment.stop()
        if self.psm:
            try:
                self.psm.close()
            except Exception:
                pass
            self.psm = None
        if self.temp:
            try:
                self.temp.close()
            except Exception:
                pass
            self.temp = None
        self._set_connected(False)
        self.btn_capture.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.status_var.set("Disconnected")
        self.log("Disconnected")

    def _read_idn(self):
        if not self.psm:
            return
        try:
            self.log("PSM *IDN? -> " + self.psm.idn())
        except Exception as e:
            messagebox.showerror("Query failed", str(e))

    def _gather_config(self):
        return {
            "sweep_start_hz": float(self.sweep_start_var.get()),
            "sweep_end_hz": float(self.sweep_end_var.get()),
            "sweep_steps": int(self.sweep_steps_var.get()),
            "sweep_log": self.sweep_step_scale_var.get() == "LOGARI",
            "sweep_type": self.sweep_type_var.get(),
            "sweep_gen_after": self.sweep_gen_after_var.get() == "ON",
            "sweep_graph1_auto": self.sweep_graph1_var.get() == "AUTO",
            "sweep_graph2_auto": self.sweep_graph2_var.get() == "AUTO",
            "sweep_freq_marker_on": self.sweep_marker_var.get() == "ON",
            "sweep_freq_marker_hz": float(self.sweep_marker_freq_var.get()),
            "amplitude_vpeak": float(self.amp_var.get()),
            "offset_v": float(self.offset_var.get()),
            "out_output_on": self.out_output_on_var.get() == "ON",
            "ch1_input": self.ch1_input_var.get(),
            "ch1_connection": self.ch1_conn_var.get(),
            "ch1_min_range": self.ch1_minrange_var.get(),
            "ch1_ranging": self.ch1_ranging_var.get(),
            "ch1_coupling": self.ch1_coupling_var.get(),
            "ch1_scale": float(self.ch1_scale_var.get()),
            "ch2_input": self.ch2_input_var.get(),
            "ch2_connection": self.ch2_conn_var.get(),
            "ch2_min_range": self.ch2_minrange_var.get(),
            "ch2_ranging": self.ch2_ranging_var.get(),
            "ch2_coupling": self.ch2_coupling_var.get(),
            "ch2_scale": float(self.ch2_scale_var.get()),
            "ch2_shunt_ohms": float(self.ch2_shunt_var.get()),
            "aux_fixture": self.fixture_var.get(),
            "aux_lcr_head_shunt": self.head_shunt_var.get(),
            "operation_mode": self.op_mode_var.get(),
            "lcr_conditions": self.lcr_cond_var.get(),
            "lcr_parameter": self.lcr_param_var.get(),
            "lcr_head": self.head_shunt_var.get(),
            "lcr_sweep_model": self.lcr_sweep_model_var.get(),
            "lcr_sweep_parallel": self.lcr_sweep_model_var.get() == "PARALLEL",
            "lcr_shunt_mode": self.lcr_shunt_var.get(),
            "lcr_connection": self.lcr_connection_var.get(),
            "lcr_graph": self.lcr_graph_var.get(),
            # ACQU
            "acqu_speed": self.acqu_speed_var.get(),
            "acqu_min_cycles": int(self.acqu_mincycles_var.get()),
            "acqu_delay_s": float(self.acqu_delay_var.get()),
            "acqu_phase_ref": self.acqu_phaseref_var.get(),
            "acqu_filter": self.acqu_filter_var.get(),
            "acqu_filter_dynamics": self.acqu_filter_dyn_var.get(),
            "acqu_low_freq": bool(self.acqu_lowfreq_var.get()),
            "acqu_datalog": self.acqu_datalog_var.get(),
            "acqu_datalog_interval_s": float(self.acqu_datalog_int_var.get()),
            "acqu_bandwidth": self.acqu_bandwidth_var.get(),
            # TRIM
            "trim_channel": self.trim_channel_var.get(),
            "trim_level": float(self.trim_level_var.get()),
            "trim_tolerance_pct": float(self.trim_tol_var.get()),
            # OUT extension
            "out_waveform": self.out_waveform_var.get(),
            "out_freq_step": float(self.out_freq_step_var.get()),
            "out_amp_step": float(self.out_amp_step_var.get()),
            "out_ceiling": float(self.out_ceiling_var.get()),
            # SYS
            "sys_phase_convention": self.sys_phconv_var.get(),
            "sys_length_unit": self.sys_length_var.get(),
            "sys_low_blanking": bool(self.sys_lowblank_var.get()),
            "sys_graph_style": self.sys_graph_var.get(),
            "sys_shunt_mode": self.sys_shunt_var.get(),
            "sys_step_message": bool(self.sys_step_msg_var.get()),
            "sys_prog_direct": bool(self.sys_prog_direct_var.get()),
            "sys_control_mode": self.sys_control_var.get(),
            "sys_keyboard_beep": bool(self.sys_kbd_beep_var.get()),
            "sys_autozero": bool(self.sys_autozero_var.get()),
            "temp_start_c": float(self.temp_start_var.get()),
            "temp_end_c": float(self.temp_end_var.get()),
            "temp_step_c": float(self.temp_step_var.get()),
            "tolerance_c": float(self.tol_var.get()),
            "direction": self.direction_var.get(),
            "mode": self.mode_var.get(),
            "sweep_timeout_s": 180.0,
        }

    def _apply_settings(self):
        if not self.psm:
            return
        try:
            cfg = self._gather_config()
            self.psm.apply_config(cfg)
            self.log("PSM configuration applied.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Apply settings failed", str(e))

    def _output_on(self):
        try:
            self.psm.output_on(gen_when_complete=True)
            # Read back so we know whether the PSM actually enabled it.
            try:
                active = self.psm.gen_active()
            except Exception:
                active = None
            if active is True:
                self.log("Output ON  (PSM confirms generator active)")
            elif active is False:
                self.log("Output ON sent, but PSM reports generator NOT active. "
                         "Check fixture / compensation.")
            else:
                self.log("Output ON sent (no status reply).")
        except Exception as e:
            messagebox.showerror("Output ON failed", str(e))

    def _output_off(self):
        try:
            self.psm.output_off()
            self.log("Output OFF")
        except Exception as e:
            messagebox.showerror("Output OFF failed", str(e))

    def _send_manual_target(self):
        if not self.temp:
            return
        try:
            t = float(self.manual_target_var.get())
            self.temp.set_target(t)
            self.log(f"Heater target sent: {t:+.2f} C")
        except Exception as e:
            messagebox.showerror("Set target failed", str(e))

    def _heater_force_on(self):
        if not self.temp:
            return
        try:
            self.temp.force_heater_on()
            self.log("Heater FORCED ON")
        except Exception as e:
            messagebox.showerror("Heater ON failed", str(e))

    def _heater_force_off(self):
        if not self.temp:
            return
        try:
            self.temp.force_heater_off()
            self.log("Heater FORCED OFF")
        except Exception as e:
            messagebox.showerror("Heater OFF failed", str(e))

    def _heater_auto(self):
        if not self.temp:
            return
        try:
            self.temp.auto_heater()
            self.log("Heater AUTO mode")
        except Exception as e:
            messagebox.showerror("Heater AUTO failed", str(e))

    def _start_exp(self):
        measurement = self.measurement_type_var.get()
        # PSM only required if we're doing LCR (alone or combined with IV).
        if measurement in ("LCR", "BOTH") and not self.psm:
            messagebox.showerror("PSM not connected",
                                 "LCR measurement needs the PSM. "
                                 "Connect to PSM first.")
            return
        # Keithley only required if we're doing IV (alone or combined).
        if measurement in ("IV", "BOTH"):
            if not self.keithley or not self.keithley.is_open():
                messagebox.showerror(
                    "Keithley not connected",
                    "IV measurement needs the Keithley. Open the "
                    "Keithley tab and Connect first.")
                return

        # Pick the temperature controller based on the Heat source selector.
        heat = self.heat_source_var.get()
        if heat == "FURNACE":
            if not self.furnace or not self.furnace.is_open():
                messagebox.showerror(
                    "Furnace not connected",
                    "Heat source is set to DPI-1100 furnace, but the "
                    "furnace is not connected. Open the Furnace tab "
                    "and click Connect first.")
                return
            try:
                temp_controller = FurnaceAdapter(self.furnace)
                temp_controller.open()
            except Exception as e:
                messagebox.showerror("Furnace adapter failed", str(e))
                return
            self._active_temp_controller = temp_controller
            self.log("Heat source: DPI-1100 furnace (Modbus RTU)")
        elif heat == "ARDUINO":
            if not self.temp or not self.temp.is_open():
                messagebox.showerror(
                    "Arduino not connected",
                    "Heat source is set to Arduino + relay heater, but "
                    "the Arduino is not connected. Pick the Arduino COM "
                    "port and Connect first.")
                return
            temp_controller = self.temp
            self._active_temp_controller = None  # don't close the shared one
            self.log("Heat source: Arduino + relay heater")
        else:   # NONE
            temp_controller = _NullTempController()
            self._active_temp_controller = None
            self.log("Heat source: NONE (room temperature)")

        out = self.outfile_var.get().strip()
        if not out:
            messagebox.showerror("Missing file", "Pick an output Excel file.")
            return
        try:
            self.excel = ExcelWriter(out)
        except Exception as e:
            messagebox.showerror("Excel open failed", str(e))
            return
        cfg = self._gather_config()
        self.experiment = Experiment(
            self.psm, temp_controller, self.excel, cfg,
            log_fn=self.log,
            status_fn=self._set_exp_status,
            sweep_done_fn=self.add_sweep_to_graph,
        )
        self.experiment.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_capture.config(state="normal" if cfg["mode"] == "stepped"
                                else "disabled")
        self.log(f"Experiment started -> {out}")

    def _capture_now(self):
        if self.experiment:
            self.experiment.capture_now()
            self.log("Capture requested")

    def _stop_exp(self):
        if self.experiment:
            self.experiment.stop()
        # Shut down the per-experiment adapter (only created when using
        # the furnace as heat source).
        if self._active_temp_controller:
            try:
                self._active_temp_controller.close()
            except Exception:
                pass
            self._active_temp_controller = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_capture.config(state="disabled")

    def _set_exp_status(self, target=None, temp=None, sweep=None, msg=None):
        if target is not None:
            self.target_var.set(f"{target:+.2f} C")
        if sweep is not None:
            self.sweep_status_var.set(sweep)
        if msg is not None:
            self.status_var.set(msg)

    # ---------- background ticks ----------
    def _tick(self):
        if self.temp and self.temp.is_open():
            v = self.temp.latest()
            if v is not None:
                self.cur_temp_var.set(f"{v:+.2f} C")
            h = self.temp.heater_state()
            if h is True:
                self.heater_var.set("ON")
                try:
                    self.heater_label.configure(foreground="red")
                except Exception:
                    pass
            elif h is False:
                self.heater_var.set("OFF")
                try:
                    self.heater_label.configure(foreground="black")
                except Exception:
                    pass
            tgt = self.temp.last_target()
            if tgt is not None:
                self.heater_target_var.set(f"{tgt:+.2f} C")
        if self.experiment and not self.experiment.is_running():
            if self.btn_stop["state"] == "normal":
                self.btn_start.config(state="normal")
                self.btn_stop.config(state="disabled")
                self.btn_capture.config(state="disabled")
        self.after(250, self._tick)


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", lambda: (_safe_close(app), app.destroy()))
    app.mainloop()


def _safe_close(app):
    try:
        if app.experiment and app.experiment.is_running():
            app.experiment.stop()
    except Exception:
        pass
    try:
        if app.psm:
            app.psm.close()
    except Exception:
        pass
    try:
        if app.temp:
            app.temp.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
