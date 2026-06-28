import time
import traceback
import threading


class Experiment:
    """Runs the temp-loop. Two modes:

    - 'stepped': waits for capture_now() to be called by the GUI button.
    - 'continuous': auto-triggers a sweep whenever the live temperature
      crosses the next target setpoint within tolerance, respecting the
      configured direction (cooling / heating / both).
    """

    def __init__(self, psm, temp_reader, excel_writer, config, log_fn=None,
                 status_fn=None, sweep_done_fn=None):
        self.psm = psm
        self.temp_reader = temp_reader
        self.excel = excel_writer
        self.cfg = config
        self.log = log_fn or (lambda msg: None)
        self.set_status = status_fn or (lambda **kw: None)
        self.sweep_done = sweep_done_fn or (lambda target, measured, rows: None)

        self._stop = threading.Event()
        self._capture_request = threading.Event()
        self._thread = None
        self._done_targets = set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._capture_request.clear()
        self._done_targets.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.psm.abort()
        except Exception:
            pass
        try:
            self.temp_reader.force_heater_off()
        except Exception:
            pass

    def capture_now(self):
        self._capture_request.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _targets(self):
        start = float(self.cfg["temp_start_c"])
        end = float(self.cfg["temp_end_c"])
        step = abs(float(self.cfg["temp_step_c"]))
        if step <= 0:
            return [start]
        targets = []
        if end >= start:
            v = start
            while v <= end + 1e-9:
                targets.append(round(v, 4))
                v += step
        else:
            v = start
            while v >= end - 1e-9:
                targets.append(round(v, 4))
                v -= step
        return targets

    def _do_sweep(self, target_c):
        self.log(f"Starting sweep at target {target_c:+.2f} C")
        self.set_status(sweep="running")
        # Always (re)enable the generator at each setpoint. Use
        # gen_when_complete=True so it stays on between setpoints — saves
        # toggling, and a continuously-running generator is the safer
        # default if the user wants to see it on the PSM panel.
        self.psm.output_on(gen_when_complete=True)
        time.sleep(0.3)
        # Verify the generator actually came on; retry once if not.
        try:
            active = self.psm.gen_active()
        except Exception:
            active = None
        if active is False:
            self.log("  Generator not active after Output ON — retrying.")
            self.psm.output_on(gen_when_complete=True)
            time.sleep(0.5)
            try:
                active = self.psm.gen_active()
            except Exception:
                active = None
        if active is False:
            self.log(f"  Skipping {target_c:+.2f} C sweep — PSM refuses to "
                     "enable the generator (check fixture / compensation).")
            self.set_status(sweep="output failed")
            return None
        if active is True:
            self.log("  Generator confirmed ON.")
        self.psm.start_sweep()
        timeout_s = float(self.cfg.get("sweep_timeout_s", 180.0))
        t0 = time.time()
        ok = False
        last_log = t0
        while time.time() - t0 < timeout_s:
            try:
                if self.psm.opc():
                    ok = True
                    break
            except Exception as e:
                self.log(f"  OPC query error: {e}")
            now = time.time()
            if now - last_log >= 10.0:
                self.log(f"  still waiting for sweep... {int(now - t0)} s")
                last_log = now
            if self._stop.is_set():
                break
            time.sleep(0.5)
        if ok:
            self.log(f"  sweep complete in {time.time() - t0:.1f} s")
        # tiny pause before reading data — gives the PSM time to settle
        # after the heavy comms during the sweep and lets any EMI from the
        # heater switching die down.
        time.sleep(0.3)
        if not ok:
            self.log("Sweep timed out")
            self.set_status(sweep="timeout")
            try:
                self.psm.abort()
            except Exception:
                pass
            return None

        try:
            rows = self.psm.read_lcr_sweep()
        except Exception as e:
            self.log(f"  read_lcr_sweep failed: {e}")
            self.set_status(sweep="read error")
            return None
        if not rows:
            self.log("Sweep returned no data")
            self.set_status(sweep="no data")
            return None

        measured = self.temp_reader.latest()
        meta = {
            "Amplitude (Vpeak)": self.cfg["amplitude_vpeak"],
            "Sweep start (Hz)": self.cfg["sweep_start_hz"],
            "Sweep end (Hz)": self.cfg["sweep_end_hz"],
            "Sweep steps": self.cfg["sweep_steps"],
            "Sweep scale": "log" if self.cfg["sweep_log"] else "linear",
            "Mode": self.cfg["mode"],
            "Tolerance (C)": self.cfg.get("tolerance_c", ""),
            "Direction": self.cfg.get("direction", ""),
        }
        try:
            sheet = self.excel.write_sweep(target_c, measured, rows, meta=meta)
        except Exception as e:
            self.log(f"  Excel write failed: {e}. Close the file in Excel "
                     f"if it's open, then continue.")
            self.set_status(sweep="excel error")
            return None
        self.log(f"Wrote {len(rows)} points to sheet '{sheet}' (T_meas={measured})")
        self.set_status(sweep="done")
        try:
            self.sweep_done(target_c, measured, rows)
        except Exception as e:
            self.log(f"  graph update failed: {e}")
        return sheet

    def _run(self):
        mode = self.cfg["mode"]
        targets = self._targets()
        self.log(f"Mode: {mode}. {len(targets)} target points: "
                 f"{targets[0]} -> {targets[-1]}")
        # Always re-send PSM configuration at the start of the run so the
        # instrument is guaranteed to be in LCR mode with the correct sweep,
        # output etc — independent of whether the user clicked Apply earlier.
        try:
            self.log("Re-applying PSM settings...")
            self.psm.apply_config(self.cfg)
            self.log("PSM ready.")
        except Exception as e:
            self.log(f"Could not apply PSM settings: {e}")
        # Push a safety limit 20 C above the highest target in this run, so
        # the Arduino's hard cut-off can't strand the heater mid-experiment.
        try:
            limit = max(targets) + 20.0
            self.temp_reader.set_safety_limit(limit)
            self.log(f"Heater safety limit set to {limit:+.1f} C")
        except Exception as e:
            self.log(f"Could not set safety limit: {e}")
        try:
            if mode == "stepped":
                self._run_stepped(targets)
            else:
                self._run_continuous(targets)
        except Exception as e:
            self.log(f"Experiment thread crashed: {e}")
            self.log(traceback.format_exc())
        finally:
            try:
                self.psm.output_off()
            except Exception:
                pass
            self.set_status(sweep="idle")
            self.log("Experiment finished")

    def _run_stepped(self, targets):
        for target in targets:
            if self._stop.is_set():
                return
            try:
                self.temp_reader.set_target(target)
                self.log(f"Heater target set to {target:+.2f} C")
            except Exception as e:
                self.log(f"Could not set heater target: {e}")
            self.set_status(target=target,
                            msg=f"Heater target {target:+.2f} C. Press Capture when ready.")
            while not self._stop.is_set():
                if self._capture_request.wait(timeout=0.2):
                    self._capture_request.clear()
                    break
            if self._stop.is_set():
                return
            try:
                self._do_sweep(target)
            except Exception as e:
                self.log(f"Sweep at {target:+.2f} C failed: {e}")
                self.log(traceback.format_exc())

    def _run_continuous(self, targets):
        tolerance = float(self.cfg.get("tolerance_c", 0.5))
        direction = self.cfg.get("direction", "both")  # cooling | heating | both
        last_t = None
        remaining = list(targets)
        self.set_status(msg=f"Waiting for targets ({direction}, +/-{tolerance} C)")
        last_sent_target = None
        while remaining and not self._stop.is_set():
            next_target = remaining[0]
            if next_target != last_sent_target:
                try:
                    self.temp_reader.set_target(next_target)
                    last_sent_target = next_target
                    self.log(f"Heater target -> {next_target:+.2f} C")
                except Exception as e:
                    self.log(f"Could not set heater target: {e}")
            current = self.temp_reader.latest()
            if current is None:
                time.sleep(0.2)
                continue
            self.set_status(target=remaining[0], temp=current)

            # Determine crossing direction from previous reading.
            crossed_ok = True
            if last_t is not None:
                delta = current - last_t
                if direction == "cooling" and delta > 0:
                    crossed_ok = False
                elif direction == "heating" and delta < 0:
                    crossed_ok = False

            # Find any target within tolerance.
            hit_idx = None
            for i, tgt in enumerate(remaining):
                if abs(current - tgt) <= tolerance and crossed_ok:
                    hit_idx = i
                    break

            if hit_idx is not None:
                target = remaining.pop(hit_idx)
                if target in self._done_targets:
                    continue
                self._done_targets.add(target)
                try:
                    self._do_sweep(target)
                except Exception as e:
                    self.log(f"Sweep at {target:+.2f} C failed: {e}")
                    self.log(traceback.format_exc())

            last_t = current
            time.sleep(0.2)
