# Automation of Temperature-Dependent LCR Meter

A comprehensive automation and control system for an LCR (Inductance-Capacitance-Resistance) meter with real-time temperature logging and data acquisition.

**Authors:** Hiba Khan & Sohum Biswas

---

## Overview

This project automates the Newtons4th PSM1735 NumetrIQ LCR meter to measure impedance parameters while maintaining precise temperature control. The system enables frequency sweep measurements, real-time temperature logging, and automated data capture to Excel with live plotting—all from a single user-friendly application.

## What is an LCR Meter?

An LCR meter measures the electrical properties of components and materials:
- **L (Inductance)** - measured in Henries (H)
- **C (Capacitance)** - measured in Farads (F)  
- **R (Resistance)** - measured in Ohms (Ω)

Additionally, it calculates derived parameters like impedance magnitude (|Z|), phase angle, Q-factor, and loss tangent (tan δ).

### How It Works
The meter applies a small AC signal at a chosen frequency and measures the resulting voltage and current vectors, deriving all electrical parameters from the complex impedance ratio. A frequency sweep repeats measurements across a range of frequencies to build Bode or Nyquist response curves.

### Applications
- Characterization of capacitors, inductors, and dielectric materials
- Electrochemical impedance spectroscopy (EIS) of batteries
- Sensor and thin-film electrical characterization

## Hardware Specifications

### Final Working Solution (Try 3)
- **Temperature Sensor:** PT100 resistance temperature detector
- **Temperature ADC:** MAX31865 RTD-to-digital converter
- **Microcontroller:** Arduino Uno with PID control
- **Temperature Control:** Relay-based switching for temperature regulation
- **LCR Instrument:** Newtons4th PSM1735 NumetrIQ
  - Frequency Range: DC to 35 MHz
  - Interface: ASCII command over USB
  - Measurements: Full impedance analysis (L, C, R, |Z|, phase, Q, tan δ)

## Project Goals

1. **Automate** the dormant LCR measurement system
2. **Make it user-friendly** with intuitive controls
3. **Enable temperature-dependent measurements** across a wide temperature range (down to -200°C)
4. **Automate data acquisition** - sweep LCR across frequencies, log temperature, capture data to Excel
5. **Provide live visualization** - plot results in real-time during measurement

## Development Journey

### Try 1: iTherm ULT-99 Temperature Controller
- **Approach:** Use existing lab equipment
- **Challenge:** Spent significant effort decoding Modbus protocol
- **Blocker:** Device temperature limit was -99°C, but experiment needs -200°C
- **Result:** Abandoned this approach

### Try 2: Thermocouple + MAX31856 + ESP32
- **Approach:** New temperature sensing stack
- **Progress:** Wiring completed, readings verified
- **Setback:** Accidentally cut the thermocouple junction, damaged the only available sensor
- **Learning:** Makeshift junction repair wasn't sufficient
- **Delay:** 2 days lost to this mistake

### Try 3: PT100 + MAX31865 + Arduino Uno (SUCCESS ✓)
- **Solution:** Switched to industrial-grade PT100 RTD sensor
- **Implementation:** MAX31865 provides digital conversion, Arduino handles PID control
- **Result:** Stable, reliable temperature control with precise feedback
- **Status:** Working system - ready for experiments

## Features

✓ Real-time LCR impedance measurement at multiple frequencies  
✓ Automated temperature control and logging  
✓ Data export to Excel for post-processing  
✓ Live plotting of results during measurement  
✓ Wide temperature range support  
✓ User-friendly single-application interface  

## Project Structure

- `temp_reader.py` - Temperature logging and data acquisition
- `main.py` - Main application controller
- `psm1735.py` - LCR meter interface
- `experiment.py` - Experiment automation
- `build_manual.py` - Setup utilities
- `requirements.txt` - Python dependencies
- `run.bat` - Quick start script
- `PSM_Temp_Logger_Manual.pdf` - Complete documentation

## Getting Started

[Setup and usage instructions to be added]

## Credits

- Prof. Somadiya Sen - Lab access and project inspiration
- Mohd Vasim Sir - Guidance and supervision
- Lab team - Support and collaboration

---

*"It took us the 3rd try to get it right" - as they say, three times the charm! 🎯*
