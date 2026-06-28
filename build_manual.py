"""Generate PSM_Temp_Logger_Manual.pdf — A4 user manual.

All table cells are wrapped in Paragraph objects so text wraps inside the
column width instead of overflowing into adjacent cells.
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

OUTPUT = "PSM_Temp_Logger_Manual.pdf"

# ====================== styles ======================
ss = getSampleStyleSheet()

title_style = ParagraphStyle(
    "Title", parent=ss["Title"], fontSize=22, leading=26,
    textColor=colors.HexColor("#1a4f8a"), alignment=TA_CENTER, spaceAfter=6,
)
subtitle_style = ParagraphStyle(
    "Subtitle", parent=ss["Normal"], fontSize=12, leading=14,
    alignment=TA_CENTER, textColor=colors.grey, spaceAfter=24,
)
h1 = ParagraphStyle(
    "H1", parent=ss["Heading1"], fontSize=15, leading=19,
    textColor=colors.HexColor("#1a4f8a"), spaceBefore=14, spaceAfter=8,
    keepWithNext=True,
)
h2 = ParagraphStyle(
    "H2", parent=ss["Heading2"], fontSize=12, leading=15,
    textColor=colors.HexColor("#333333"), spaceBefore=10, spaceAfter=4,
    keepWithNext=True,
)
body = ParagraphStyle(
    "Body", parent=ss["BodyText"], fontSize=10, leading=13.5,
    alignment=TA_JUSTIFY, spaceAfter=6,
)
bullet = ParagraphStyle(
    "Bullet", parent=body, leftIndent=14, bulletIndent=4, spaceAfter=2,
)
code_style = ParagraphStyle(
    "Code", parent=ss["Code"], fontSize=8.5, leading=11,
    backColor=colors.HexColor("#f4f4f4"),
    borderColor=colors.HexColor("#d0d0d0"), borderWidth=0.5,
    borderPadding=4, spaceBefore=4, spaceAfter=8,
    leftIndent=4, rightIndent=4,
)
note_style = ParagraphStyle(
    "Note", parent=body, leftIndent=10, rightIndent=10,
    backColor=colors.HexColor("#fff8dc"),
    borderColor=colors.HexColor("#e0c060"), borderWidth=0.5,
    borderPadding=6, spaceAfter=8,
)

# Cell styles — Paragraph-based so text wraps inside the cell
cell = ParagraphStyle(
    "Cell", parent=ss["BodyText"], fontSize=8.5, leading=10.5,
    alignment=TA_LEFT, spaceAfter=0, spaceBefore=0,
)
cell_b = ParagraphStyle(
    "CellBold", parent=cell, fontName="Helvetica-Bold",
)
cell_header = ParagraphStyle(
    "CellHeader", parent=cell, fontName="Helvetica-Bold",
    textColor=colors.whitesmoke,
)


def P(text, style=cell):
    """Wrap any cell text in a Paragraph so it word-wraps inside the cell."""
    if text is None:
        text = ""
    return Paragraph(str(text), style)


def make_table(rows, col_widths):
    """Build a styled Table where every cell wraps automatically."""
    wrapped = []
    for r, row in enumerate(rows):
        if r == 0:
            wrapped.append([P(c, cell_header) for c in row])
        else:
            wrapped.append([P(c, cell) for c in row])

    t = Table(wrapped, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a4f8a")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#ffffff"), colors.HexColor("#f4f6f9")]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


# ====================== header / footer ======================
def page_decorations(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.grey)
    canvas.drawString(20*mm, 12*mm, "PSM Temp Logger - User Manual")
    canvas.drawRightString(A4[0] - 20*mm, 12*mm, f"Page {doc.page}")
    canvas.setStrokeColor(colors.HexColor("#cccccc"))
    canvas.line(20*mm, 14*mm, A4[0] - 20*mm, 14*mm)
    canvas.restoreState()


# ====================== content ======================
story = []

# ---- Title page ----
story.append(Spacer(1, 55*mm))
story.append(Paragraph("PSM Temp Logger", title_style))
story.append(Paragraph(
    "Temperature-dependent LCR and I-V characterisation suite<br/>"
    "for the Newtons4th PSM1735, DPI-1100 furnace and Keithley 2400-series",
    subtitle_style,
))
story.append(Spacer(1, 35*mm))
story.append(Paragraph(
    "<i>User Manual</i>",
    ParagraphStyle("c", parent=body, alignment=TA_CENTER, fontSize=12),
))
story.append(Spacer(1, 10*mm))
story.append(Paragraph(
    "Developed by<br/><b>MOHD VASIM, HIBA KHAN and SOHUM BISWAS</b>",
    ParagraphStyle("c2", parent=body, alignment=TA_CENTER),
))
story.append(PageBreak())

# ---- Acknowledgement ----
story.append(Paragraph("Acknowledgement", h1))
story.append(Paragraph(
    "This project - the complete automation of the Newtons4th PSM1735 "
    "NumetriQ LCR meter, integration with the Divya DPI-1100 dry-block "
    "temperature calibrator and the Keithley 2400-series source meter, "
    "and the development of a single host application that drives all "
    "three for temperature-dependent measurements - was designed and "
    "brought to completion by <b>Mohd Vasim, PhD</b>, together with his "
    "interns <b>Hiba Khan</b> and <b>Sohum Biswas</b>.",
    body,
))
story.append(Paragraph(
    "Their work combined hardware bring-up (instrument communication, "
    "RS-232 / RS-485 / Modbus RTU / VISA / Arduino firmware) with the "
    "design and implementation of a unified Python host application "
    "capable of running unattended experiments across the full range of "
    "-273 to +1000 deg C, capturing LCR and I-V responses at every "
    "setpoint and writing the results directly to an Excel workbook for "
    "downstream analysis.",
    body,
))
story.append(Paragraph(
    "The authors are gratefully acknowledged for the time, persistence "
    "and care that went into the project - without which the workflow "
    "described in this manual would not exist.",
    body,
))
story.append(PageBreak())

# ---- 1. Overview ----
story.append(Paragraph("1. Overview", h1))
story.append(Paragraph(
    "The PSM Temp Logger automates electrical characterisation across "
    "user-defined temperature ranges. At each setpoint the application "
    "can run an LCR frequency sweep on the PSM1735, an I-V sweep on a "
    "Keithley 2400-series source meter, or both - with the temperature "
    "either held by a small Arduino-driven relay heater or by a Divya "
    "DPI-1100 dry-block calibrator over Modbus RTU. All captured data "
    "is written to an Excel workbook (one sheet per setpoint) and "
    "displayed live in the application.", body))

story.append(Paragraph("Key features", h2))
for txt in [
    "Bidirectional control of the PSM1735 over USB-serial (N4L ASCII).",
    "PT100 temperature reading via MAX31865 + Arduino, OR via the "
    "BTC-9100 PID controller inside a DPI-1100 calibrator over Modbus RTU.",
    "Bang-bang heater control with hysteresis, host watchdog, and a "
    "hard safety cut-off configurable at run-time.",
    "Keithley 2400 / 2401 / 2410 / 2420 source-meter support over "
    "VISA (USB-TMC, GPIB, Ethernet) or RS-232.",
    "Three measurement modes: LCR sweep only, I-V sweep only, "
    "or combined LCR + I-V at each setpoint.",
    "Live Bode and Nyquist plots (colour-coded by temperature) and "
    "tabular sweep viewer with CSV export.",
    "Real-time single-frequency LCR readout for sample alignment.",
    "Excel output with one sheet per setpoint, ready for post-processing.",
]:
    story.append(Paragraph("&bull; " + txt, bullet))

# ---- 2. Hardware setup ----
story.append(Paragraph("2. Hardware setup", h1))
story.append(Paragraph(
    "The full system can use any subset of the following instruments - "
    "the application requires only the ones relevant to the experiment "
    "you are running. PSM is required for LCR work; Keithley for I-V; "
    "Arduino or DPI-1100 (or neither, for room-temperature only) for the "
    "heat source.", body))

story.append(make_table([
    ["Instrument", "Connects via", "Used for"],
    ["Newtons4th PSM1735",
     "USB (FTDI virtual COM)",
     "LCR / impedance measurements"],
    ["Arduino + MAX31865 + PT100 + relay",
     "USB (CH340 / FT232 virtual COM)",
     "PT100 temperature read; relay-switched heater control"],
    ["Divya DPI-1100 (with internal BTC-9100)",
     "RS-232 or RS-485 via USB adapter (Modbus RTU)",
     "Dry-block temperature calibrator (range 50-650 deg C)"],
    ["Keithley 2400 / 2401 / 2410",
     "USB-TMC (VISA), GPIB, Ethernet, or RS-232",
     "Source-measure unit for I-V characterisation"],
], col_widths=[55*mm, 55*mm, 60*mm]))

story.append(Paragraph("Arduino + heater pinout", h2))
story.append(make_table([
    ["Arduino pin", "Connects to", "Notes"],
    ["D10", "MAX31865 CS", "SPI chip select"],
    ["D11 / D12 / D13", "MAX31865 SDI / SDO / SCK", "Hardware SPI bus"],
    ["5V / GND", "MAX31865 and relay module VCC / GND", ""],
    ["D7", "Relay module IN1",
     "Polarity set by SSR_ACTIVE_HIGH in the firmware"],
], col_widths=[30*mm, 55*mm, 85*mm]))

story.append(Paragraph("Relay wiring", h2))
story.append(Paragraph(
    "The heater is connected through the relay's <b>COM</b> and "
    "<b>NO</b> (Normally Open) terminals so that when the Arduino is "
    "unpowered the relay is de-energised and the heater is OFF. "
    "Do NOT use NC for the heater - that would leave the heater on if "
    "the controller dies.", body))
story.append(Paragraph(
    "DC supply (+) &rarr; relay COM &rarr; relay NO &rarr; "
    "heater (+) &rarr; heater (-) &rarr; DC supply (-).", code_style))
story.append(Paragraph(
    "<b>Safety note:</b> the firmware enforces a hard temperature "
    "ceiling (default 1050 deg C, overridden by the app per experiment) "
    "and a 60 s host watchdog. If the PC loses contact with the Arduino "
    "for more than 60 s the heater is automatically forced OFF.",
    note_style,
))

# ---- 3. Software installation ----
story.append(PageBreak())
story.append(Paragraph("3. Software installation", h1))
story.append(Paragraph("Required on the PC (one-time):", body))
for txt in [
    "<b>Python 3.9 or later</b> - download from python.org. Tick "
    "<i>Add Python to PATH</i> during install.",
    "<b>Python libraries</b> - open PowerShell or Command Prompt and run:",
]:
    story.append(Paragraph("&bull; " + txt, bullet))
story.append(Paragraph(
    "pip install pyserial openpyxl matplotlib pymodbus pyvisa pyvisa-py",
    code_style,
))
for txt in [
    "<b>Arduino IDE</b> - from arduino.cc. Install the "
    "<i>Adafruit MAX31865</i> library via "
    "<i>Tools &rarr; Manage Libraries</i>.",
    "<b>USB-serial drivers</b> - usually auto-installed by Windows. If "
    "an instrument does not appear as a COM port in Device Manager, "
    "install FTDI VCP drivers (for the PSM), CH340 drivers (for cheap "
    "Arduino / RS-485 adapters) or Prolific drivers (for PL2303 RS-232 "
    "adapters - install the older 3.3.2.105 driver for clone chips).",
    "<b>NI-VISA (optional)</b> - only needed if you are connecting a "
    "Keithley over GPIB. For USB-TMC and Ethernet the pure-Python "
    "pyvisa-py backend is sufficient.",
]:
    story.append(Paragraph("&bull; " + txt, bullet))

# ---- 4. Arduino firmware ----
story.append(Paragraph("4. Arduino firmware", h1))
story.append(Paragraph(
    "Open <code>arduino\\max31865_temp\\max31865_temp.ino</code> in Arduino "
    "IDE, select the correct board (<i>Tools &rarr; Board</i>) and serial "
    "port (<i>Tools &rarr; Port</i>), then click <b>Upload</b>.", body))
story.append(Paragraph(
    "Edit the constants at the top of the sketch if your hardware differs "
    "(SSR_PIN for the relay control pin, SSR_ACTIVE_HIGH for the relay "
    "polarity, RTD_NOMINAL / REF_RESISTOR for PT100 vs PT1000, WIRES for "
    "the PT100 wiring, MAX_SAFE_TEMP_DEFAULT for the safety ceiling).",
    body))

story.append(Paragraph("Serial commands the sketch accepts", h2))
story.append(make_table([
    ["Command", "Argument", "Effect"],
    ["SET:&lt;value&gt;", "Target deg C",
     "Switch to AUTO mode, regulate to the specified value"],
    ["LIMIT:&lt;value&gt;", "Hard cut-off deg C",
     "Change the safety ceiling at run time"],
    ["H:1", "(none)", "Force heater ON (overrides target)"],
    ["H:0", "(none)", "Force heater OFF"],
    ["AUTO", "(none)", "Return to SET-driven control"],
    ["PING", "(none)", "Heartbeat - resets the watchdog"],
], col_widths=[35*mm, 35*mm, 100*mm]))

story.append(Paragraph("Streaming output from the sketch", h2))
story.append(make_table([
    ["Line", "Meaning", "Example"],
    ["T:&lt;value&gt;", "Temperature in deg C, approx 5 Hz", "T:27.482"],
    ["H:&lt;0|1&gt;", "Current heater state", "H:1"],
    ["ERR:&lt;hex&gt;", "MAX31865 fault flags", "ERR:84"],
    ["#&lt;text&gt;", "Diagnostic comment (ignored by host app)", "# pong"],
], col_widths=[35*mm, 80*mm, 55*mm]))

# ---- 5. DPI-1100 / BTC-9100 furnace ----
story.append(PageBreak())
story.append(Paragraph("5. DPI-1100 / BTC-9100 furnace", h1))
story.append(Paragraph(
    "The Divya DPI-1100 dry-block temperature calibrator contains a "
    "Brainchild BTC-9100 PID controller. Communication is over Modbus "
    "RTU - either RS-232 or RS-485 depending on which optional "
    "interface module is fitted (CM97-2 = RS-232, CM97-1 = RS-485).",
    body))

story.append(Paragraph("Default communication settings", h2))
story.append(make_table([
    ["Parameter on controller", "Default value", "Set in the app"],
    ["COMM", "RTU (= Modbus RTU)", "(implicit)"],
    ["ADDR", "1", "Slave"],
    ["BAUD", "9.6 (= 9600 bps)", "Baud"],
    ["DATA", "8", "(fixed: 8 data bits)"],
    ["PARI", "EVN (= Even parity)", "Parity = E"],
    ["STOP", "1", "(fixed: 1 stop bit)"],
], col_widths=[55*mm, 55*mm, 60*mm]))

story.append(Paragraph("Wiring (RS-232 with USB-RS232 adapter)", h2))
story.append(make_table([
    ["Controller terminal", "DB9 pin on PC adapter", "Function"],
    ["Pin 13 (TXD)", "Pin 2 (RD)", "Controller transmits to PC"],
    ["Pin 14 (RXD)", "Pin 3 (TD)", "PC transmits to controller"],
    ["Pin 15 (COM)", "Pin 5 (GND)", "Common ground"],
    ["-", "Pin 1 to Pin 6 (jumper)",
     "DCD-DSR loopback (handshake bypass)"],
    ["-", "Pin 7 to Pin 8 (jumper)",
     "RTS-CTS loopback (handshake bypass)"],
], col_widths=[40*mm, 50*mm, 80*mm]))

story.append(Paragraph("Wiring (RS-485 with USB-RS485 adapter)", h2))
story.append(make_table([
    ["Controller terminal", "Adapter terminal", "Function"],
    ["Pin 13 (TX1)", "D+ (sometimes labelled A)",
     "Differential pair, polarity not fixed - swap if no response"],
    ["Pin 14 (TX2)", "D- (sometimes labelled B)",
     "Differential pair (other half)"],
    ["Pin 15 (COM)", "GND (optional)",
     "Recommended for noise reduction"],
], col_widths=[40*mm, 50*mm, 80*mm]))

story.append(Paragraph(
    "In the application, switch to the <b>Furnace (DPI-1100)</b> tab to "
    "connect to the controller and to read PV / write SP1 manually. "
    "Once connected, switch the Heat source selector on the Experiment "
    "tab to <b>DPI-1100 furnace</b> and the temperature loop will use "
    "the BTC-9100 as the thermal controller instead of the Arduino.",
    body,
))

# ---- 6. Keithley 2400 source meter ----
story.append(Paragraph("6. Keithley 2400-series source meter", h1))
story.append(Paragraph(
    "The application supports the Keithley 2400 / 2401 / 2410 / 2420 / "
    "2430 / 2440 source meter family. They share a common SCPI command "
    "set. Communication is via VISA (USB-TMC, GPIB, Ethernet) or "
    "directly via RS-232.", body))

story.append(Paragraph("Connection options", h2))
story.append(make_table([
    ["Interface", "VISA resource example", "Notes"],
    ["USB-TMC",
     "USB0::0x05E6::0x2400::123456::INSTR",
     "Click Scan VISA in the Keithley tab to auto-detect"],
    ["GPIB",
     "GPIB0::24::INSTR",
     "Requires NI-VISA installed (pyvisa-py does not support GPIB)"],
    ["Ethernet",
     "TCPIP0::192.168.1.50::INSTR",
     "Newer 2400 units with the LXI option"],
    ["RS-232",
     "(no VISA resource; pick COM port instead)",
     "Use the Serial (RS-232) interface mode in the Keithley tab"],
], col_widths=[28*mm, 65*mm, 77*mm]))

story.append(Paragraph("Keithley tab features", h2))
story.append(make_table([
    ["Section", "Purpose"],
    ["Keithley connection",
     "Pick Interface (VISA or Serial), select resource string or "
     "serial port, click Connect. *IDN? button verifies the link."],
    ["Manual source / measure",
     "Configure source function (voltage or current), level and "
     "compliance, turn Output ON / OFF, take a single READ to display "
     "V and I."],
    ["IV sweep",
     "Run a point-by-point sweep from Start to Stop over N points, "
     "with compliance and settle time. Each point is streamed to the "
     "log. Captured sweeps can be exported to CSV."],
], col_widths=[55*mm, 115*mm]))

story.append(Paragraph(
    "When the Keithley is connected, switch the Measurement selector on "
    "the Experiment tab to <b>IV sweep only</b> for standalone I-V "
    "characterisation, or <b>LCR + Keithley (IV) at each setpoint</b> "
    "to combine both instruments inside the temperature loop.",
    body,
))

story.append(Paragraph(
    "<b>Safety note:</b> always set a compliance limit appropriate for "
    "the device under test before turning the output on. Compliance is "
    "the safety limit on the <i>measured</i> quantity (current if "
    "sourcing voltage, voltage if sourcing current). The Keithley will "
    "clamp at this limit if the DUT impedance forces it to exceed.",
    note_style,
))

# ---- 7. Launching the application ----
story.append(PageBreak())
story.append(Paragraph("7. Launching the application", h1))
story.append(Paragraph("From PowerShell:", body))
story.append(Paragraph(
    'cd "C:\\Users\\HIBA KHAN\\Downloads\\psm_temp_logger"\n'
    "python app\\main.py", code_style))
story.append(Paragraph(
    "Or double-click <code>run.bat</code>. The application opens "
    "maximised with the following tabs:", body))
story.append(make_table([
    ["Tab", "Purpose"],
    ["Experiment",
     "Heat source and measurement type selectors, temperature range, "
     "Excel output path, Start / Stop / Capture controls, live status, "
     "heater manual override, message log."],
    ["PSM Configuration",
     "All PSM1735 parameters organised by section (Sweep, OUT, CH1, "
     "CH2, AUX, MODE, ACQU, TRIM, SYS). Scrollable. Tooltips on every "
     "field. Sent to the instrument via the Apply settings button."],
    ["Live Graph",
     "Matplotlib-embedded Bode or Nyquist plot. Updates after each "
     "captured sweep, colour-coded by temperature. Log / linear axes, "
     "show all sweeps or only the latest, abs value toggle."],
    ["Table",
     "Tabular view of any captured sweep. Per-row CSV export of the "
     "entire dataset."],
    ["Realtime",
     "Live single-frequency LCR readout updated approximately twice "
     "per second. Used for sample alignment and quick checks before "
     "starting a sweep."],
    ["Furnace (DPI-1100)",
     "Connect to the BTC-9100 controller over Modbus RTU. Live PV, "
     "active setpoint, mode, alarm status, manual SP1 write, Reset "
     "and Auto-tune commands."],
    ["Keithley (2400)",
     "Connect to a Keithley source meter over VISA or serial. Manual "
     "source / measure, IV sweep configuration and execution, CSV "
     "export."],
], col_widths=[45*mm, 125*mm]))

# ---- 8. Running an experiment ----
story.append(Paragraph("8. Running an experiment", h1))
story.append(Paragraph("Step-by-step:", h2))
steps = [
    "<b>Connect</b> - In the Experiment tab, click Refresh ports, pick "
    "the PSM port (default COM12) and (optionally) the Arduino port "
    "(default COM11), click Connect. If running an IV-only experiment "
    "you do not need to connect the Arduino.",
    "<b>Configure instruments</b> - Switch to PSM Configuration and "
    "sanity-check Sweep / OUT / AUX / MODE for the LCR run. If using "
    "the Keithley or Furnace, also connect those in their respective "
    "tabs.",
    "<b>Pick Heat source</b> on the Experiment tab: Arduino + relay, "
    "DPI-1100 furnace, or None (room temperature only).",
    "<b>Pick Measurement</b>: LCR sweep only, IV sweep only, or "
    "LCR + Keithley (IV) at each setpoint.",
    "<b>Pick experiment mode</b>: Stepped (manual Capture at each "
    "setpoint) or Continuous (auto-capture when measured temperature "
    "crosses each target within the tolerance band).",
    "<b>Enter temperature range</b> - Start, End and Step. The "
    "application generates a list of setpoints from Start to End in "
    "increments of Step.",
    "<b>Pick the Excel output file</b> - Browse to the destination. "
    "Parent folders are created automatically; a placeholder workbook "
    "is saved immediately so the file appears even before any sweep "
    "completes.",
    "<b>Start experiment</b> - The application sends the configuration "
    "to all selected instruments, sets a safety LIMIT on the heater, "
    "and begins the loop. The Log panel streams progress.",
    "<b>Monitor</b> - Switch to Live Graph, Table or Realtime tabs "
    "while the experiment runs. Heater state and live temperature "
    "are shown in the Experiment tab's status panel.",
    "<b>Stop</b> - Either let it complete all setpoints, or click "
    "Stop. Stop also sends H:0 / SP1=min so the heater (or furnace) "
    "is brought to a safe state.",
]
for i, s in enumerate(steps, 1):
    story.append(Paragraph(f"{i}. {s}", body))

# ---- 9. Output files ----
story.append(PageBreak())
story.append(Paragraph("9. Output files", h1))
story.append(Paragraph(
    "After each successful sweep the application appends a new "
    "worksheet to the Excel workbook. Sheet names are formatted "
    "as <code>T_+25.0C</code>, <code>T_-50.0C</code>, etc. for LCR "
    "data; combined LCR + I-V runs append a second sheet per setpoint "
    "with an <code>_IV</code> suffix.", body))

story.append(Paragraph("Per-sheet layout (LCR)", h2))
story.append(make_table([
    ["Cell", "Content", "Example"],
    ["A1, B1", "Timestamp", "2026-06-20 16:42:11"],
    ["A2, B2", "Target temperature (deg C)", "+40.0"],
    ["A3, B3", "Measured temperature (deg C)", "+40.12"],
    ["A4 onwards", "Experiment metadata",
     "Amplitude, sweep range, mode, tolerance, direction"],
    ["Header row", "Column labels (8 columns)",
     "Frequency_Hz, Q, TanD, Impedance_Ohm, "
     "Phase_deg, L_H, C_F, R_Ohm"],
    ["Data rows", "One row per frequency point",
     "32 rows by default"],
], col_widths=[28*mm, 60*mm, 82*mm]))

story.append(Paragraph(
    "CSV export from the Table tab writes the full dataset (all sweeps "
    "for all temperatures) into a single long-form CSV file with "
    "TargetTemp_C and MeasuredTemp_C columns prepended.", body))
story.append(Paragraph(
    "CSV export from the Keithley tab writes captured I-V sweeps "
    "with columns Sweep, Point, V, I, R.", body))

# ---- 10. Credits ----
story.append(Paragraph("10. Credits", h1))
story.append(Paragraph(
    "Automation of the PSM1735 NumetriQ LCR meter, integration with "
    "the DPI-1100 / BTC-9100 dry-block calibrator, and Keithley "
    "2400-series source-meter support - including all hardware "
    "integration, firmware, host application and documentation - "
    "completed by <b>Mohd Vasim, PhD</b> and his interns "
    "<b>Hiba Khan</b> and <b>Sohum Biswas</b>.", body))
story.append(Paragraph(
    "Reference documents: Newtons4th PSM1700 / PSM1735 Communications "
    "Manual (Rev 1.54), Brainchild Classic Premium 100 Series User's "
    "Manual UM91001I (chapter 7), Keithley 2400-series SCPI command "
    "reference.", body))
story.append(Paragraph(
    "Built on Python (Tkinter, pyserial, openpyxl, matplotlib, "
    "pymodbus, pyvisa, pyvisa-py) and the Adafruit MAX31865 Arduino "
    "library.", body))
story.append(Spacer(1, 8))
story.append(Paragraph(
    "This manual was generated automatically from the project source. "
    "For the latest version of the code, sync the folder "
    "<code>psm_temp_logger\\</code>.", body))

# ====================== build ======================
doc = SimpleDocTemplate(
    OUTPUT, pagesize=A4,
    leftMargin=20*mm, rightMargin=20*mm,
    topMargin=20*mm, bottomMargin=22*mm,
    title="PSM Temp Logger - User Manual",
    author="Mohd Vasim, Hiba Khan, Sohum Biswas",
)
doc.build(story, onFirstPage=page_decorations,
          onLaterPages=page_decorations)
print(f"Wrote {OUTPUT}")
