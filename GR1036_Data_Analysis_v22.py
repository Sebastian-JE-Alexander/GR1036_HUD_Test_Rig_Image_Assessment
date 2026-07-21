"""
GR1036 HUD Test Rig
Image Assessment GUI and PLC / NI Vision Builder communications

This Python script was built for deployment onto the HUD Test Rig IPC that worked in conjunction with an Allen-Bradley PLC
and an inspection running in NI Vision Builder.

On launching the script, a tcp connection is made to the external PLC and the local Vision builder program.
The script listens and reacts to changes in the data structure that is being sent by the PLC.
All station controls relating to this script are done through using the HMI and Pushbutton controls as the PLC will pass
along the corresponding action bytes in the data structure.

This script then communicates the instructions to Vision Builder so that it knows when to acquire an image of the barcode/Glass,
what position the robot is in and what side of the glass is being inspected.

After completing the full run in Vision Builder, the script looks into the data export directory of vision builder to perform
all the necessary calculations (Customer requested metrics can be seen below) and display pass/fail for all the metrics according
to set tolerances.
The option to view a chart for a position that can be used to reference the magnitude and direction of ghosting
found at a specific robot position within the eye box on the glass.

1)Image Size
2) Image Rotation
3) Trapezoidal Distortion
4) Aspect Ratio
5) Translation
6) Smile Distortion
7) Ghosting Distance


The data structure between Python(LH) / PLC(RH):
Byte 0.0 = Heartbeat
     0.1 = Error
     0.2 = Barcode Capture Pass / Capture Barcode
     0.3 = Barcode Capture Fail / Trigger Camera
     0.4 = Trigger Camera Pass / LHS - Variant
     0.5 = Trigger Camera Fail / RHS - Variant
     0.6 = Capture Complete / Capture Results
     0.7 = Ready /

Byte 1 = Reserved
Byte 2.0 =  / Barcode Required LH
     2.1 =  / Barcode Required RH

Byte 3 = Reserved
Byte 4-5 = Error Code (integer)
Byte 6-7 = Robot Position Echo / Robot Position Number
Byte 8-57 = Scanned Barcode
Byte 58-77 = Master CSV echo / Master CSV
Byte 78-79 = Recipe Selection Echo / Recipe Selection


The data structure between Python(LH) / Vision Builder(RH):
Byte 0.0 = camera trigger / camera ready
     0.1 = LHS active / trigger complete
     0.2 = RHS active / trigger fail
     0.3 = LH barcode trigger / barcode complete
     0.4 = RH barcode trigger / barcode fail

Byte 1 = Reserved
Byte 2-3 = Position Number / Position Echo
Byte 4-53 =  / Scanned Barcode

"""
# ====================================== Library Imports ===========================================================
# These are all the Python library imports used for running this script.
# Please note that not all are standard Python libraries and before running this script,
# you need to ensure that all libraries listed below are installed on the PC/dev environment on PATH
# for python to be able to correctly locate them and start the script.

# Additional note: You will also need to install Python itself onto the deployment pc to run the script.
#                  During python installation, ensure that the option to place Python on PATH is selected.

# All non-standard Python libraries that need to be .pip installed are separated below.


import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import time
from datetime import datetime
import csv
import os
import threading
import socket
import select
import struct
from queue import Queue, Empty

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image, ImageTk


# =================================================== Global Variables ===========================================================
# Here we declare all Globals that are used throughout the script, any variables not declared here are considered local and
# declared within their respective functions and sections.


# ---------------------------- Global Variables to Store our Data States -----------------------------------------------------

g_master_positions_db = {}  # Keyed 1-5, each value is a dataframe for that position's master baseline.
g_watch_directory = "C:\\Test Data Log"  # Default fallback path.
g_master_csv_directory = "C:\\Master CSV files"  # Folder where master/tolerance CSVs the PLC references by filename live.
g_auto_save_directory = "C:\\Assessment Reports"  # Folder where automatic end-of-cycle assessment reports are saved.
g_MM_PER_PX = 25.4 / 96.0  # Sets the scaling for how many mm per pixel, calculated using the DPI of the images that the camera produces

# ------------------------------------- Databases to Hold Dataframes for up to 5 Robot Positions each ----------------------

g_lh_positions_db = {}
g_rh_positions_db = {}
g_lh_results_db = {}
g_rh_results_db = {}

# ------------------------------------------Thread-safe Communication Channel ------------------------------------------------------

g_gui_queue = Queue()

# -------------------------------------- Global Python to PLC Communication Variables ----------------------------------------------

g_plc_tx_heartbeat = False
g_plc_tx_error = False
g_plc_tx_barcode_pass = False
g_plc_tx_barcode_fail = False
g_plc_tx_camera_pass = False
g_plc_tx_camera_fail = False
g_plc_tx_capture_complete = False
g_capture_results_armed = False  # Private to thread_plc - tracks Capture Results edge for proper one-shot ingest + capture_complete clearing.
g_plc_tx_ready = False  # byte0.7 - true only while no PLC triggers active and no pass/fail pending.
g_plc_tx_error_code = 0
g_plc_tx_position_echo = 0
g_plc_tx_recipe_echo = 0
g_plc_tx_barcode_string = ""
g_plc_tx_master_csv_string = ""

# -------------------------------------- Global PLC to Python Communication Variables -------------------------------------------------------

g_plc_rx_heartbeat = False
g_plc_rx_error = False
g_plc_rx_capture_barcode = False
g_plc_rx_trigger_camera = False
g_plc_rx_lhs_sequence_active = False
g_plc_rx_rhs_sequence_active = False
g_plc_rx_capture_results = False
g_plc_rx_lh_barcode_req = False
g_plc_rx_rh_barcode_req = False
g_plc_rx_error_code = 0
g_plc_rx_position = ""
g_plc_rx_recipe = ""
g_plc_rx_barcode = ""
g_plc_rx_master_csv = ""


# ------------------------------------------- Global Python to VB Communication Variables ---------------------------------------------------

g_vb_tx_trigger_camera = False
g_vb_tx_lhs = False
g_vb_tx_rhs = False
g_vb_tx_lh_barcode = False
g_vb_tx_rh_barcode = False
g_vb_tx_position = ""

# ------------------------------------------ Global VB to Python Communication Variables -------------------------------------------------------

g_vb_rx_camera_ready = False
g_vb_rx_trigger_complete = False
g_vb_rx_trigger_fail = False
g_vb_rx_barcode_complete = False
g_vb_rx_barcode_fail = False
g_vb_rx_position_echo = 0
g_vb_rx_scanned_barcode = ""


# ---------------------------------------------- Shared Cross-Thread Safe Sockets ---------------------------------------------------------------

g_connection_vb = None
vbai_lock = threading.Lock()
g_system_running = True
g_run_btn = None
g_manual_pos_entry = None  # Entry widget reference for the Manual VBAI Test Panel.
g_master_dir_lbl = None  # Label widget reference for the Master CSV directory display in Settings.
g_auto_save_dir_lbl = None  # Label widget reference for the auto-save directory display in Settings.

# ------------------------------------------------- Network Configuration Parameters -----------------------------------------------------------

# In the event that TCP ports, IP addresses and send/recieve rates need adjusting in the system, make sure to match those changes here so that
# the script can continue to function correctly.
# Note that the PLC IP isn't set here as we are using the special socket '0.0.0.0' for connecting to the PLC TCP Port.
PLC_PORT = 9005
VBAI_IP = "127.0.0.1"
VBAI_PORT = 9006
plc_send_rate = 0.200
plc_receive_rate = 0.0


# -------------------------------------- Global References to Keep Logo Image Objects Alive in Memory -------------------------------------------

g_logo1_img = None
g_logo2_img = None

# ---------------------------------------------------- Other Globals for thread_vb --------------------------------------------------------------

g_vb_send_done = 0
g_vb_mode = 0
g_vb_lhs = 0
g_vb_rhs = 0
g_vb_lh_bc_trigger = 0
g_vb_rh_bc_trigger = 0
g_vb_position = int
loaded_lh = 0
loaded_rh = 0

# ================================================== File loading ======================================================
# Here we create the functions needed for pulling in external files into Python and setting directory paths so that
# Python knows where to look on the PC for them.

def load_data(file_path):
    """
    Reads and cleans the CSV data to extract only the XY co-ordinates for the Primary and Ghost points.
    """
    skip_rows = 0
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for i, line in enumerate(f):
            if "Center.X" in line or "Primary" in line:
                skip_rows = i
                break

    df = pd.read_csv(file_path, sep=';', skiprows=skip_rows)
    df.columns = df.columns.str.strip()

    if len(df.columns) >= 9:
        df = df.rename(columns={
            df.columns[5]: 'x_prim',
            df.columns[6]: 'y_prim',
            df.columns[7]: 'x_ghost',
            df.columns[8]: 'y_ghost'
        })
    else:
        raise ValueError("The CSV structure is invalid.")

    target_cols = ['x_prim', 'y_prim', 'x_ghost', 'y_ghost']
    for col in target_cols:
        df[col] = df[col].astype(str).str.replace(',', '.')
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=target_cols)
    if len(df) != 77:
        raise ValueError(f"Grid integrity check failed. Expected exactly 77 points, found {len(df)}.")
    return df


# ---------------------------------------------- Change Directories ----------------------------------------------------------

def change_watch_directory():
    """
    Used within the GUI for manually changing the directory for where the vision builder exports are without having to edit code.
    Please note that the watch directory is also referenced and set within vision builders data logging step.
    """
    global g_watch_directory
    selected_dir = filedialog.askdirectory(title="Select Vision Builder Output Directory")
    if selected_dir:
        g_watch_directory = os.path.normpath(selected_dir)
        dir_lbl.config(text=f"Watching: Dataset", fg="blue")

def change_master_csv_directory():
    """
    Same as idea as the previous function, only this one is much more likely to have changed as it's only referenced by the Python script.
    """
    global g_master_csv_directory
    selected_dir = filedialog.askdirectory(title="Select Master CSV Directory (where PLC-named files live)")
    if selected_dir:
        g_master_csv_directory = os.path.normpath(selected_dir)
        if g_master_dir_lbl is not None:
            g_master_dir_lbl.config(text=f"Master CSV folder: {g_master_csv_directory}", fg="blue")
        log_message(f"Master CSV directory set to: {g_master_csv_directory}")

def change_auto_save_directory():
    """
    Again same idea as previous watch directory functions, this one relates to where the Python script chooses to export all the saved report .csv files to.
    """
    global g_auto_save_directory
    selected_dir = filedialog.askdirectory(title="Select Auto-Save Reports Directory")
    if selected_dir:
        g_auto_save_directory = os.path.normpath(selected_dir)
        if g_auto_save_dir_lbl is not None:
            g_auto_save_dir_lbl.config(text=f"Auto-save folder: {g_auto_save_directory}", fg="blue")
        log_message(f"Auto-save directory set to: {g_auto_save_directory}")

# ------------------------------------------ Load and Select Files --------------------------------------------------------------------

def _load_master_positions(base_name, folder, mode="BOTH"):
    """
    Given a base name (e.g. 'Master123') and folder, loads master files for the requested mode.
    Files are named Master123_lhs_pos1.csv ... Master123_lhs_pos5.csv (LHS) and
    Master123_rhs_pos1.csv ... Master123_rhs_pos5.csv (RHS).
    g_master_positions_db is keyed by ("LHS", pos_idx) and ("RHS", pos_idx) tuples so LHS and RHS
    masters are kept separate and each variant's assessment uses only its own master baseline.
    """
    global g_master_positions_db
    g_master_positions_db.clear()
    sides = []
    if mode == "LHS" or mode == "BOTH": sides.append("lhs")
    if mode == "RHS" or mode == "BOTH": sides.append("rhs")
    for side in sides:
        variant = side.upper()
        for i in range(1, 6):
            candidate = os.path.join(folder, f"{base_name}_{side}_pos{i}.csv")
            if os.path.exists(candidate):
                try:
                    g_master_positions_db[(variant, i)] = load_data(candidate)
                except Exception as e:
                    log_message(f"[MASTER] Failed to load '{os.path.basename(candidate)}': {e}")
    return g_master_positions_db


def select_master_file():
    """
    Somewhat depreciated function but was kept as it can still have its uses when debugging.
    Allows the user to load in an individual master file for comparing to target data.
    It's depreciated as we can debug simply by creating a recipe on the HMI to test with.
    """
    global g_master_positions_db
    # User picks any one of the master files - the base name is extracted and all 5 positions are loaded.
    file_path = filedialog.askopenfilename(title="Select Any Master CSV File (base name used to find all 5 positions)",
                                           filetypes=[("CSV files", "*.csv")])
    if file_path:
        folder = os.path.dirname(file_path)
        basename = os.path.splitext(os.path.basename(file_path))[0]
        # Strip any trailing _pos suffix so we always work from the clean base name.
        import re
        base = re.sub(r'_(lhs|rhs)_pos\d+$', '', basename, flags=re.IGNORECASE)
        base = re.sub(r'_pos\d+$', '', base, flags=re.IGNORECASE)
        loaded = _load_master_positions(base, folder)
        if loaded:
            master_label.config(text=f"Master: {base} ({len(loaded)}/10)", fg="green", font=("Arial", 9, "bold"))
            log_message(f"[MASTER] Loaded {len(loaded)} position files for '{base}'")
        else:
            master_label.config(text="Master: No position files found", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"No files matching '{base}_lhs_pos1.csv' ... '{base}_rhs_pos5.csv' found in {folder}")
        check_run_conditions()

#============================================== Network Queues ======================================================
# Here we handle all the event queues for the Python script to determine what processes need to happen depending on
# what commands are being sent over the TCP connections.


# --------------------------------------------------- Data Loading --------------------------------------------------

def auto_ingest_pipeline(mode="BOTH"):
    """
    This is queue deals with the loading of the correct data into the script from the Vision Builder watch directory.
    It works off thread_plc, where the plc sends a two byte integer value in the structure to indicate recipe selection.
    Checking the which recipe was received allows for Python to know which database to load.
    We have two different databases, one for LH 5 positions and one for RH 5 positions.
    We run a retry short retry loop to let Python scan through the folder and correctly fill the required data slots.
    """
    global g_lh_positions_db, g_rh_positions_db, loaded_lh, loaded_rh
    if not os.path.exists(g_watch_directory): return

    g_lh_positions_db.clear()  # Always clear both sides regardless of mode - prevents stale data from a
    g_rh_positions_db.clear()  # previous BOTH run persisting into a subsequent LHS-only or RHS-only run.

    expected_lh = 5 if (mode == "LHS" or mode == "BOTH") else 0 # Sets how many slots are needed to be filled with data.
    expected_rh = 5 if (mode == "RHS" or mode == "BOTH") else 0

    # Retry loop: VB writes files after triggering Capture Results, so they may not all exist yet.
    # Keep scanning until all expected slots are filled or 3 seconds elapses.
    # Should never actually need more than a second but extra time ensures that we account for Windows being a resource hog and slow our reads.
    # Note: the GUI will actually freeze during this retry loop, however the time is entirely reliant on the CPU read speed to get the files
    #       from the folder, so it shouldn't be noticeable to the user.
    deadline = datetime.now().timestamp() + 3.0
    while datetime.now().timestamp() < deadline:
        all_raw_files = []
        for entry in os.listdir(g_watch_directory):
            full_path = os.path.join(g_watch_directory, entry)
            if os.path.isfile(full_path) and entry.lower().endswith('.csv'):
                all_raw_files.append((full_path, entry.lower(), os.path.getmtime(full_path)))

        loaded_lh = 0
        if mode == "LHS" or mode == "BOTH":
            g_lh_positions_db.clear()
            lhs_candidates = [f for f in all_raw_files if "lhs_pos" in f[1]]
            lhs_candidates.sort(key=lambda x: x[2], reverse=True)
            # Group by position and limit to the 3 most recent files per slot - matching VB's 3-attempt retry process.
            # If all 3 fail the 77-point check the slot stays empty, preventing Python from
            # falling back to a file from a previous run and filling a failed position.
            lhs_by_pos = {}
            for path, filename, mtime in lhs_candidates:
                detected_pos = None
                for i in range(1, 6):
                    if f"pos{i}" in filename: detected_pos = i; break
                if detected_pos:
                    lhs_by_pos.setdefault(detected_pos, []).append((path, filename, mtime))
            for pos_idx in range(1, 6):
                for path, filename, _ in lhs_by_pos.get(pos_idx, [])[:3]:
                    try:
                        g_lh_positions_db[pos_idx] = load_data(path)
                        loaded_lh += 1
                        break  # Found a valid file for this position - stop looking
                    except Exception as e:
                        log_message(f"[INGEST] Skipped LHS pos{pos_idx} '{os.path.basename(path)}': {e}")

        loaded_rh = 0
        if mode == "RHS" or mode == "BOTH":
            g_rh_positions_db.clear()
            rhs_candidates = [f for f in all_raw_files if "rhs_pos" in f[1]]
            rhs_candidates.sort(key=lambda x: x[2], reverse=True)
            rhs_by_pos = {}
            for path, filename, mtime in rhs_candidates:
                detected_pos = None
                for i in range(1, 6):
                    if f"pos{i}" in filename: detected_pos = i; break
                if detected_pos:
                    rhs_by_pos.setdefault(detected_pos, []).append((path, filename, mtime))
            for pos_idx in range(1, 6):
                for path, filename, _ in rhs_by_pos.get(pos_idx, [])[:3]:
                    try:
                        g_rh_positions_db[pos_idx] = load_data(path)
                        loaded_rh += 1
                        break  # Found a valid file for this position, so can stop scanning for it.
                    except Exception as e:
                        log_message(f"[INGEST] Skipped RHS pos{pos_idx} '{os.path.basename(path)}': {e}")

        if loaded_lh >= expected_lh and loaded_rh >= expected_rh:
            break  # All expected files found and valid, so no need to keep waiting.

        time.sleep(0.5)  # Wait 0.5s before retrying.

        # This last section checks the recipe number and then displays on the amount of files that were successfully
        # loaded for that run. Also writes an overall status message to the log box.
    if mode == "BOTH":
        test_label.config(text=f"Matrix: {loaded_lh} LHS / {loaded_rh} RHS (10 Files)", fg="green",
                          font=("Arial", 9, "bold"))
    elif mode == "LHS":
        test_label.config(text=f"Matrix: {loaded_lh} LHS Only", fg="#0dcaf0", font=("Arial", 9, "bold"))
    elif mode == "RHS":
        test_label.config(text=f"Matrix: {loaded_rh} RHS Only", fg="#ffc107", font=("Arial", 9, "bold"))

    check_run_conditions()
    if len(g_master_positions_db) > 0:
        log_message(f"[PLC] Recipe '{mode}' ingest complete ({loaded_lh} LHS / {loaded_rh} RHS) - running assessment")
        execute_assessment()
    else:
        log_message(f"[PLC] Recipe '{mode}' ingest complete ({loaded_lh} LHS / {loaded_rh} RHS) - "
                     f"no master CSVs loaded, assessment skipped")


# ------------------------------------------ Event Handler Queue -----------------------------------------------------

def status_network():
    global g_master_positions_db
    try:
        while True:
            event_type, payload = g_gui_queue.get_nowait()
            if event_type == "PLC_CONNECTION":
                plc_status_lbl.config(text="PLC LINK ACTIVE" if payload == "CONNECTED" else "PLC DISCONNECTED",
                                      bg="green" if payload == "CONNECTED" else "red")
            elif event_type == "VBAI_CONNECTION":
                vbai_status_lbl.config(text="VBAI LINK ACTIVE" if payload == "CONNECTED" else "VBAI DISCONNECTED",
                                       bg="green" if payload == "CONNECTED" else "red")
            elif event_type == "AUTO_INGEST_TRIGGER":
                auto_ingest_pipeline(mode=payload)
            elif event_type == "CYCLE_START":
                # A new cycle has started, so then we clear displayed results so the dashboard doesn't show
                # stale pass/fail data from the previous run while the new one is in progress.
                # Does not clear g_master_positions_db or the position databases, only the display.
                for key in ui_rows:
                    ui_rows[key]['master'].config(text="-")
                    ui_rows[key]['test'].config(text="-")
                    ui_rows[key]['variance'].config(text="-")
                    ui_rows[key]['status'].config(bg="lightgray", text=" IDLE ", fg="black")
                for i in range(1, 6):
                    lh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")
                    rh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")
                overall_status_lbl.config(text="CYCLE IN PROGRESS", bg="#0c447c", fg="white",
                                          font=("Arial", 20, "bold"), width=20, borderwidth=2, relief="solid")
            elif event_type == "PLC_CAPTURE_RESULTS":
                if len(g_master_positions_db) > 0: execute_assessment()
            elif event_type == "PLC_MASTER_CSV":
                base_name = payload if not payload.lower().endswith('.csv') else payload[:-4]
                loaded = _load_master_positions(base_name, g_master_csv_directory)
                if loaded:
                    master_label.config(text=f"Master: {base_name} ({len(loaded)}/10)", fg="green")
                    log_message(f"[PLC] Auto-loaded master '{base_name}': {len(loaded)} position files")
                else:
                    log_message(f"[PLC] No position files found for '{base_name}' in {g_master_csv_directory}")
            g_gui_queue.task_done()
    except Empty:
        pass
    if g_system_running: root.after(50, status_network)


#=========================================== Assessment Results =================================================



# ----------------------------------------- Creating Files -----------------------------------------------------

def _write_report_csv(file_path):
    """
    Shared CSV writing logic used by both manual and automatic save paths.
    """
    with open(file_path, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["GR1036 HUD Test Rig - Quality Assessment Report"])
        writer.writerow(["Timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        cleaned_barcode = g_plc_tx_barcode_string.strip('\x00\r\n') if g_plc_tx_barcode_string else ""
        writer.writerow(["Scanned Barcode", cleaned_barcode if cleaned_barcode and cleaned_barcode != "0" else "N/A"])
        writer.writerow([])
        writer.writerow(["Variant Side", "Position Number", "Evaluation Metric", "Master Baseline", "Test Target",
                         "Allowed Tolerance", "Calculated Variance", "Status Result"])
        for pos, metrics in sorted(g_lh_results_db.items()):
            for key, data in metrics.items():
                label, master_txt, test_txt, variance_txt, status_txt = data
                writer.writerow(["LHS", f"Position {pos}", label, master_txt, test_txt,
                                 tol_inputs[key].get(), variance_txt, status_txt.strip()])
        for pos, metrics in sorted(g_rh_results_db.items()):
            for key, data in metrics.items():
                label, master_txt, test_txt, variance_txt, status_txt = data
                writer.writerow(["RHS", f"Position {pos}", label, master_txt, test_txt,
                                 tol_inputs[key].get(), variance_txt, status_txt.strip()])

# ------------------------------------------- Saving Created files ------------------------------------------------------------

def auto_save_report():
    """
    Called automatically at end of each cycle once capture_complete is set by the PLC.
    Saves a csv file that collects all the results displayed on the GUI.
    """
    if not g_lh_results_db and not g_rh_results_db: return
    try:
        os.makedirs(g_auto_save_directory, exist_ok=True)
        cleaned_bc = g_plc_tx_barcode_string.strip('\x00\r\n') if g_plc_tx_barcode_string else ""
        raw_bc = cleaned_bc if cleaned_bc and cleaned_bc != "0" else "NO_BC"
        # Strip characters Windows won't allow in filenames.
        safe_bc = "".join(c for c in raw_bc if c not in r'\/:*?"<>|')
        filename = f"HUD_Report_{safe_bc}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        file_path = os.path.join(g_auto_save_directory, filename)
        _write_report_csv(file_path)
        log_message(f"[AUTO-SAVE] Report saved: {filename}")
    except Exception as e:
        messagebox.showerror("Auto-Save Failed", f"Failed to save assessment report:\n\n{e}\n\nPath: {g_auto_save_directory}")


def save_assessment_report():
    """
    Manual save triggered from Settings menu — lets the operator choose location and filename.
    Not used during auto cycle or even manual mode but a nice to have for debugging.
    """
    if not g_lh_results_db and not g_rh_results_db:
        messagebox.showwarning("Save Report", "No processed assessment data available to save.")
        return
    file_path = filedialog.asksaveasfilename(
        title="Save Assessment Report", defaultextension=".csv",
        filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
        initialfile=f"HUD_Assessment_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    if not file_path: return
    try:
        _write_report_csv(file_path)
        messagebox.showinfo("Save Report", "Assessment report successfully archived.")
    except Exception as e:
        messagebox.showerror("Export Failure", str(e))

def clear_all_data():
    """
    This button is hidden away in the settings menu of the GUI. Allows for the User to wipe the current data loaded onto the GUI.
    Clears all labels and loaded databases, and essentially resets the GUI to the state of when you first open it.
    """
    global g_master_positions_db, g_lh_positions_db, g_rh_positions_db, g_lh_results_db, g_rh_results_db
    if not messagebox.askyesno("Clear Dashboard", "Reset data arrays?"): return
    g_master_positions_db.clear()
    g_lh_positions_db.clear()
    g_rh_positions_db.clear()
    g_lh_results_db.clear()
    g_rh_results_db.clear()
    master_label.config(text="Master File Empty", fg="red")
    test_label.config(text="Test Files Empty", fg="red")
    check_run_conditions()
    for key in ui_rows:
        ui_rows[key]['master'].config(text="-")
        ui_rows[key]['test'].config(text="-")
        ui_rows[key]['variance'].config(text="-")
        ui_rows[key]['status'].config(bg="lightgray", text=" IDLE ", fg="black")
    for i in range(1, 6):
        lh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")
        rh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")
    overall_status_lbl.config(text="SYSTEM IDLE", bg="lightgray", fg="black", font=("Arial", 10, "bold"))




def load_tolerances_from_template():
    """
    Used for the tolerance load button to allow for the user to manually upload a tolerance template file.
    Note: the Tolerance file is automatically loaded on startup, and can be individual values can be adjusted
          on the GUI screen. Also worth noting that if the User uploads a tolerance file it will only be loaded
          whilst the GUI is open for that session, once you close it or hit the clear data button it resets back
          to the default loaded tolerances.
    """
    target_file = filedialog.askopenfilename(title="Open Tolerance Settings Template File",
                                             filetypes=[("Text Documents", "*.txt"), ("All Files", "*.*")])
    if not target_file: return
    key_mapping = {"image size": ["size"], "image rotation": ["rotation"], "trapezoidal dist. h": ["trap_h"],
                   "trapezoidal dist. v": ["trap_v"], "aspect ratio": ["ar"], "translation x": ["trans_x"],
                   "translation y": ["trans_y"], "smile distortion h": ["smile_h"],
                   "smile distortion v": ["smile_v"], "ghosting distance": ["ghosting"]}
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip() or line.startswith("#") or "=" not in line: continue
                raw_key, raw_val = line.split("=", 1)
                clean_key = raw_key.strip().lower()
                if clean_key in key_mapping:
                    for ui_tag in key_mapping[clean_key]:
                        if ui_tag in tol_inputs:
                            tol_inputs[ui_tag].delete(0, tk.END)
                            tol_inputs[ui_tag].insert(0, raw_val.strip())
        if len(g_master_positions_db) > 0: execute_assessment()
    except Exception as e:
        messagebox.showerror("Template Error", str(e))



# ----------------------------------------------------- Application Shutdown Process --------------------------------------------------------------

def shutdown_application():
    """
    Defines what happens when the GUI is shutdown. In this case we close the TCP connection cleanly and run a root.destroy to close the entire GUI
    """
    global g_system_running
    g_system_running = False
    with vbai_lock:
        if g_connection_vb: g_connection_vb.close()
    root.destroy()


# ========================================================== Image Assessment ===========================================================================
# Here we handle getting the XY co-ordinates, using them for calculations and then assessing the results of the calculations of the image assessment


# ---------------------------------------------------------- Calculations --------------------------------------------------------

def get_grid_points(df):
    """
    Due to how vision builder grabs the points in an image during processing, our csv to work with
    is not in order so we need to clean the csv file and arrange all the XY co-ordinates into something
    we can work with.
    The image vision builder is grabbing from is a 77-point grid, meaning we can arrange all these points
    into the same.
    With the grid remade, we can then target specific points in the grid to pull their XY values for use in
    calculations.

    Note: Due to this function relying on there being an expected number of points, the moment vision builder
    returns a failed capture with less than or more than 77 points the result of the function will get ignored and
    dealt with by the rest of the code as a form of error handling.

    """
    df = df.copy()  # avoid mutating the stored master/test dataframe across repeated calls
    y_min, y_max = df['y_prim'].min(), df['y_prim'].max()
    total_height = y_max - y_min
    approx_row_spacing = total_height / 6
    df['row_group'] = ((df['y_prim'] - y_min) / approx_row_spacing).round()
    sorted_df = df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True)
    return {
        "top_left": sorted_df.iloc[0],
        "top_mid": sorted_df.iloc[5],
        "top_right": sorted_df.iloc[10],
        "left_mid": sorted_df.iloc[33],
        "center": sorted_df.iloc[38],
        "right_mid": sorted_df.iloc[43],
        "bottom_left": sorted_df.iloc[66],
        "bottom_mid": sorted_df.iloc[71],
        "bottom_right": sorted_df.iloc[76]
    }


def run_all_calculations(df):
    """
    Main function where all calculations are handled using the loaded data points from Vision Builder.
    This function gets called by a Top Level function alongside others during the assessment stage.
    """
    p = get_grid_points(df)
    w_size, h_size = df['x_prim'].max() - df['x_prim'].min(), df['y_prim'].max() - df['y_prim'].min()           # Image Size Calculation.
    w_ar, h_ar = p['top_right']['x_prim'] - p['top_left']['x_prim'], p['bottom_left']['y_prim'] - p['top_left'][
        'y_prim']
    ar = w_ar / h_ar if h_ar != 0 else 0                                                                        # Aspect Ratio Calculation.
    smile_v = p['top_mid']['y_prim'] - ((p['top_left']['y_prim'] + p['top_right']['y_prim']) / 2)               # Vertical Smile Calculation.
    smile_h = p['left_mid']['x_prim'] - ((p['top_left']['x_prim'] + p['bottom_left']['x_prim']) / 2)            # Horizontal Smile Calculation.
    try:
        tl, tr = df.loc[(df['x_prim'] + df['y_prim']).idxmin()], df.loc[(df['x_prim'] - df['y_prim']).idxmax()] # Image Rotation Calculation.
        rot = np.degrees(np.arctan2(tr['y_prim'] - tl['y_prim'], tr['x_prim'] - tl['x_prim']))
    except Exception:
        rot = 0.0
    try:
        tl, br = df.loc[(df['x_prim'] + df['y_prim']).idxmin()], df.loc[(df['x_prim'] + df['y_prim']).idxmax()] # Trapezoidal Distortion Calculation.
        tr, bl = df.loc[(df['x_prim'] - df['y_prim']).idxmax()], df.loc[(df['x_prim'] - df['y_prim']).idxmin()]
        tw, bw, lh, rh = tr['x_prim'] - tl['x_prim'], br['x_prim'] - bl['x_prim'], bl['y_prim'] - tl['y_prim'], br[
            'y_prim'] - tr['y_prim']
        trap = ((abs(tw - bw) / min(tw, bw)) * 100, (abs(lh - rh) / min(lh, rh)) * 100)
    except Exception:
        trap = (0.0, 0.0)
    ghost = np.mean(
        np.sqrt((df['x_ghost'] - df['x_prim']) ** 2 + (df['y_ghost'] - df['y_prim']) ** 2)) if not df.empty else 0.0  # Average Ghosting Calculation.

    # Here we assign all the results of the above calculations to their respective labels to called in other parts of the code.
    return {'image_size': (w_size, h_size), 'aspect_ratio': ar, 'smile': (smile_h, smile_v), 'rotation': rot,
            'translation': (p['center']['x_prim'], p['center']['y_prim']), 'trap_dist': trap, 'avg_ghosting': ghost}

# ------------------------------------------------------ Executing Calculations ----------------------------------------------------------------

def execute_assessment():
    """
    This is our top level function that calls all the needed parts to run our calculations for all the required points
    and update status labels on the GUI by looking at the results returned by the sub functions.
    """
    global g_lh_results_db, g_rh_results_db, g_plc_tx_capture_complete
    if len(g_master_positions_db) == 0: return
    log_message("[ASSESS] Starting LHS assessment...")
    try:
        lh_failed = process_variant_database(g_lh_positions_db, g_lh_results_db, g_master_positions_db, lh_overview_buttons, "LHS")
    except Exception as e:
        log_message(f"[ASSESS] LHS EXCEPTION: {e}")
        import traceback
        log_message(f"[ASSESS] {traceback.format_exc()}")
        lh_failed = True
    if g_rh_positions_db:
        log_message("[ASSESS] Starting RHS assessment...")
    try:
        rh_failed = process_variant_database(g_rh_positions_db, g_rh_results_db, g_master_positions_db, rh_overview_buttons, "RHS")
    except Exception as e:
        log_message(f"[ASSESS] RHS EXCEPTION: {e}")
        import traceback
        log_message(f"[ASSESS] {traceback.format_exc()}")
        rh_failed = True
    g_plc_tx_capture_complete = True
    auto_save_report()
    if lh_failed or rh_failed:
        overall_status_lbl.config(text="FAIL", bg="red", fg="white", font=("Arial", 14, "bold"))
    else:
        if len(g_lh_positions_db) == 0 and len(g_rh_positions_db) == 0:
            overall_status_lbl.config(text="SYSTEM IDLE", bg="lightgray", fg="black", font=("Arial", 10, "bold"))
            g_plc_tx_capture_complete = False
        else:
            overall_status_lbl.config(text="PASS", bg="green", fg="white", font=("Arial", 14, "bold"))
    refresh_displayed_position_metrics()

def check_run_conditions():
    """
    Old function that was used for manual testing of the GUI. This allowed for locking access to the run calculations button on the GUI,
    so that it couldn't be pressed unless all data was filled.
    """
    global g_run_btn
    if g_run_btn and g_run_btn.winfo_exists():
        if len(g_master_positions_db) > 0 and (len(g_lh_positions_db) > 0 or len(g_rh_positions_db) > 0):
            g_run_btn.config(state=tk.NORMAL, bg="#198754", fg="white")
        else:
            g_run_btn.config(state=tk.DISABLED, bg="#e0e0e0", fg="#a0a0a0")


# ------------------------------------------- Checking Calculation Results ---------------------------------------------------------

def process_variant_database(source_db, results_db, master_db, overview_buttons, variant):
    """
    Here is where we compare and declare a PASS/FAIL status for each positions list of
    metrics that are being assessed.

    For each listed metric we compare our test target calculation against a loaded master file calculation.
    These two values are then compared against each other to return their difference. That difference is
    then checked against the current tolerance value for that metric to evaluate a PASS/FAIL for that metric

    An overall PASS/FAIL for the position is determined by whether any of the metrics failed, if so an
    overall FAIL is given for the position, which prompts the user to investigate the position on the
    GUI to see which of the metrics caused a FAIL to occur.

    """
    any_fail = False
    for i in range(1, 6):
        if i not in source_db: overview_buttons[i].config(bg="lightgray", text="EMPTY", fg="black")
    for pos_idx, t_df in source_db.items():
        master_key = (variant, pos_idx)
        if master_key not in master_db:
            # No master file for this variant/position - mark it clearly and skip comparison.
            overview_buttons[pos_idx].config(bg="orange", text="NO MASTER", fg="white")
            log_message(f"[ASSESSMENT] No master file for {variant} position {pos_idx} - skipped")
            continue
        failed_criteria_count = 0
        m_res = run_all_calculations(master_db[master_key])
        t_res = run_all_calculations(t_df)
        metrics = {}
        m_w_mm, m_h_mm = m_res['image_size'][0] * g_MM_PER_PX, m_res['image_size'][1] * g_MM_PER_PX
        t_w_mm, t_h_mm = t_res['image_size'][0] * g_MM_PER_PX, t_res['image_size'][1] * g_MM_PER_PX
        w_pct = ((t_w_mm - m_w_mm) / m_w_mm * 100) if m_w_mm != 0 else 0.0
        h_pct = ((t_h_mm - m_h_mm) / m_h_mm * 100) if m_h_mm != 0 else 0.0
        max_size_pct = w_pct if abs(w_pct) >= abs(h_pct) else h_pct
        metrics['size'] = ("Image Size", f"{round(m_w_mm, 1)}x{round(m_h_mm, 1)} mm",
                           f"{round(t_w_mm, 1)}x{round(t_h_mm, 1)} mm", f"{round(max_size_pct, 2)} %",
                           "PASS" if abs(max_size_pct) <= float(tol_inputs['size'].get()) else "FAIL")
        rot_diff = t_res['rotation'] - m_res['rotation']
        metrics['rotation'] = ("Image Rotation", f"{round(m_res['rotation'], 2)}°", f"{round(t_res['rotation'], 2)}°",
                               f"{round(rot_diff, 3)} °",
                               "PASS" if abs(rot_diff) <= float(tol_inputs['rotation'].get()) else "FAIL")
        m_trap_h, m_trap_v = m_res['trap_dist']
        t_trap_h, t_trap_v = t_res['trap_dist']
        metrics['trap_h'] = ("Trapezoidal Dist. H", f"{round(m_trap_h, 1)}%", f"{round(t_trap_h, 1)}%",
                             f"{round(trap_h_diff := t_trap_h - m_trap_h, 3)} % delta",
                             "PASS" if abs(trap_h_diff) <= float(tol_inputs['trap_h'].get()) else "FAIL")
        metrics['trap_v'] = ("Trapezoidal Dist. V", f"{round(m_trap_v, 1)}%", f"{round(t_trap_v, 1)}%",
                             f"{round(trap_v_diff := t_trap_v - m_trap_v, 3)} % delta",
                             "PASS" if abs(trap_v_diff) <= float(tol_inputs['trap_v'].get()) else "FAIL")
        metrics['ar'] = ("Aspect Ratio", f"{round(m_res['aspect_ratio'], 3)}", f"{round(t_res['aspect_ratio'], 3)}",
                         f"{round(ar_diff := t_res['aspect_ratio'] - m_res['aspect_ratio'], 3)}",
                         "PASS" if abs(ar_diff) <= float(tol_inputs['ar'].get()) else "FAIL")
        m_trans_x, m_trans_y = m_res['translation']
        t_trans_x, t_trans_y = t_res['translation']
        trans_x_diff_mm = (t_trans_x - m_trans_x) * g_MM_PER_PX
        trans_y_diff_mm = (t_trans_y - m_trans_y) * g_MM_PER_PX
        metrics['trans_x'] = ("Translation X",
                              f"{round(m_trans_x * g_MM_PER_PX, 1)} mm",
                              f"{round(t_trans_x * g_MM_PER_PX, 1)} mm",
                              f"{round(trans_x_diff_mm, 3)} mm",
                              "PASS" if abs(trans_x_diff_mm) <= float(tol_inputs['trans_x'].get()) else "FAIL")
        metrics['trans_y'] = ("Translation Y",
                              f"{round(m_trans_y * g_MM_PER_PX, 1)} mm",
                              f"{round(t_trans_y * g_MM_PER_PX, 1)} mm",
                              f"{round(trans_y_diff_mm, 3)} mm",
                              "PASS" if abs(trans_y_diff_mm) <= float(tol_inputs['trans_y'].get()) else "FAIL")
        m_smile_h, m_smile_v = m_res['smile']
        t_smile_h, t_smile_v = t_res['smile']
        metrics['smile_h'] = ("Smile Distortion H",
                              f"{round(m_smile_h * g_MM_PER_PX, 2)} mm",
                              f"{round(t_smile_h * g_MM_PER_PX, 2)} mm",
                              f"{round(smile_h_diff_mm := (t_smile_h - m_smile_h) * g_MM_PER_PX, 3)} mm",
                              "PASS" if abs(smile_h_diff_mm) <= float(tol_inputs['smile_h'].get()) else "FAIL")
        metrics['smile_v'] = ("Smile Distortion V",
                              f"{round(m_smile_v * g_MM_PER_PX, 2)} mm",
                              f"{round(t_smile_v * g_MM_PER_PX, 2)} mm",
                              f"{round(smile_v_diff_mm := (t_smile_v - m_smile_v) * g_MM_PER_PX, 3)} mm",
                              "PASS" if abs(smile_v_diff_mm) <= float(tol_inputs['smile_v'].get()) else "FAIL")
        metrics['ghosting'] = ("Ghosting Distance", f"{round(m_res['avg_ghosting'] * g_MM_PER_PX, 2)} mm",
                               f"{round(t_res['avg_ghosting'] * g_MM_PER_PX, 2)} mm",
                               f"{round(ghost_diff_mm := (t_res['avg_ghosting'] - m_res['avg_ghosting']) * g_MM_PER_PX, 3)} mm",
                               "PASS" if abs(ghost_diff_mm) <= float(tol_inputs['ghosting'].get()) else "FAIL")
        for k in metrics:
            if metrics[k][4] == "FAIL": failed_criteria_count += 1; any_fail = True
        results_db[pos_idx] = metrics
        overview_buttons[pos_idx].config(bg="red" if failed_criteria_count > 0 else "green",
                                         text="FAIL" if failed_criteria_count > 0 else "PASS", fg="white")
    return any_fail



#=================================================== GUI Buttons ======================================================================

def show_drift_chart(variant, position_idx):
    """
    Newly added function to incorporate the old separate drift analysis script into the GUI so that for the user, they can work with
    one unified script and GUI.
    By adding it into the main script it also allows us to automate it and reduce user interaction and possible error.
    On completion of all the data calculations, we generate a matplot graph for each robot position that uses the
    loaded data to create a chart that showcases the magnitude and direction of ghosting occurring within the chosen position.
    """

    # ------------------------------------------- Calculating the Drift -----------------------------------------------------

    source_db = g_lh_positions_db if variant == "LHS" else g_rh_positions_db
    if position_idx not in source_db:
        messagebox.showwarning("Drift Analysis", f"No data loaded for {variant} Position {position_idx}.")
        return

    df = source_db[position_idx]
    x_prim  = df['x_prim'].values  # Pulls the values from each column in the vision builder csv file and writes them to variables.
    y_prim  = df['y_prim'].values
    x_ghost = df['x_ghost'].values
    y_ghost = df['y_ghost'].values

    # Compute the difference between the X and Y co-ordinates of each pair of primary and ghost image,
    # then compute the magnitude of the difference between the primary and ghost image pair.
    u = (x_ghost - x_prim) * g_MM_PER_PX
    v = (y_ghost - y_prim) * g_MM_PER_PX
    mag = np.sqrt(u ** 2 + v ** 2 + 1e-12)
    norm_mag = mag / mag.max()
    marker_size = 120 + 180 * norm_mag

    norm_vec = np.sqrt(u ** 2 + v ** 2 + 1e-12)
    u_dir = u / norm_vec
    v_dir = v / norm_vec
    arrow_inches = 0.22  # Note the reason for the use of inches here is for drawing of the vector arrows on the chart as using cm or mm caused the scaling to be way too small.

    # Create the popup window for where the chart will be drawn with matplot.
    popup = tk.Toplevel(root)
    popup.title(f"Drift Analysis — {variant} Position {position_idx}")  # Titles the window to show the WSDrive and Position number for the chart.
    popup.geometry("900x750")  # Can adjust the size of the popup window containing the chart here.

    # ------------------------------------------------------ Plotting the Drift --------------------------------------------------------------------

    # Create the scatterplot, create the magnitude and vector elements and constrain them to only the individual plot points.
    # Create the vector arrows to represent the direction of drift the ghosts are going away from the primary circles.

    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  #Using a special matplotlib import to allow us to use it inside a tkinter UI window.
    fig, ax = plt.subplots(figsize=(9, 7), dpi=100, facecolor='white')
    ax.set_facecolor('#f9f9f9')

    sc = ax.scatter(x_ghost * g_MM_PER_PX, y_ghost * g_MM_PER_PX, c=mag, cmap='viridis', s=marker_size,  # Creates the scatterplot chart.
                    edgecolor='black', linewidth=0.9, alpha=0.93, zorder=10,
                    label='Ghost points (with magnitude)')
    ax.quiver(x_prim * g_MM_PER_PX, y_prim * g_MM_PER_PX, u_dir, v_dir,  # This creates the vector arrows that are overlapped onto each point in the scatterplot.
              units='inches', scale_units='inches', scale=1 / arrow_inches,
              color='lime', width=0.010, headwidth=7, headlength=6.9,
              minlength=0.10, pivot='tail', alpha=0.92, zorder=15,
              edgecolor='darkgreen', linewidth=0.7)

    # Here is where we set up the layout of the chart in matplotlib, defining things such as the axes and legends.
    ax.set_aspect('equal')
    ax.invert_yaxis()  # VB uses image coordinates where Y=0 is at the top and increases downward.
    ax.grid(True, alpha=0.15, linestyle='--', color='0.75')
    cbar = fig.colorbar(sc, ax=ax, shrink=0.7, pad=0.04)
    cbar.set_label('Magnitude of Drift (mm)', rotation=270, labelpad=18)
    ax.legend(loc='upper right', fontsize=8)
    ax.set_title(f'HUD Drift Analysis — {variant} Position {position_idx}\n'
                 '(Color/Size = Magnitude  •  Arrows = Drift Direction)',
                 fontsize=13, pad=12)
    ax.set_xlabel('X Co-ordinates (mm)')
    ax.set_ylabel('Y Co-ordinates (mm)')
    fig.tight_layout()

    canvas = FigureCanvasTkAgg(fig, master=popup)  # Allows us to make it into a popup window in tkinter.
    canvas.draw()
    canvas.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

    popup.protocol("WM_DELETE_WINDOW", lambda: (plt.close(fig), popup.destroy()))  # Defines what happens to the chart when closed.



# ------------------------------------------------------ Viewing the Results ----------------------------------------------------------------

def select_and_view_position(variant, position_idx):
    """
    Changes what is currently displayed on the GUI by the User clicking on each of the listed positions.
    Loads in all the data and calculation results associated with that position.
    """
    current_view_label.config(text=f"Viewing: {variant} - Position {position_idx}")
    refresh_displayed_position_metrics(variant, position_idx)


def refresh_displayed_position_metrics(forced_variant=None, forced_pos=None):
    """
    Allows us to clear the screen of all currently displayed positions as new cycle is run to help with
    clarity for the User due to the rig being enclosed.
    """
    selected_variant = forced_variant if forced_variant else getattr(current_view_label, 'target_variant', 'LHS')
    selected_pos = forced_pos if forced_pos else getattr(current_view_label, 'target_pos', 1)
    if forced_variant and forced_pos: current_view_label.target_variant, current_view_label.target_pos = forced_variant, forced_pos
    target_db = g_lh_results_db if selected_variant == "LHS" else g_rh_results_db
    if selected_pos not in target_db:
        for key in ui_rows:
            ui_rows[key]['master'].config(text="-")
            ui_rows[key]['test'].config(text="-")
            ui_rows[key]['variance'].config(text="-")
            ui_rows[key]['status'].config(bg="lightgray", text=" NO DATA ", fg="black")
        return
    metrics = target_db[selected_pos]
    for key in ui_rows:
        _, master_txt, test_txt, variance_txt, status_txt = metrics[key]
        ui_rows[key]['master'].config(text=master_txt)
        ui_rows[key]['test'].config(text=test_txt)
        ui_rows[key]['variance'].config(text=variance_txt)
        ui_rows[key]['status'].config(bg="green" if status_txt == "PASS" else "red", text=f" {status_txt} ", fg="white")


# ==================================== Manual VBAI Test Panel (Debugging Use Only) ==================================================

# These helpers do NOT touch thread_vb() or the socket directly. They simply set the
# same g_plc_rx_* globals that thread_vb() already reads (the exact variables the PLC
# would normally populate) and clear g_vb_send_done.
# thread_vb()'s existing loop then sends the structure on its next pass using its own, unmodified send logic.

# --------------------------------------------- Logging -------------------------------------------------------------

def log_message(text):
    """
    Function to allow us to log messages in tkinter itself. Each message is appended with a timestamp.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    log.config(state="normal")
    log.insert(tk.END, f"[{timestamp}] {text}\n")
    log.see(tk.END)
    log.config(state="disabled")

# ------------------------------------------------ Manual Controls ------------------------------------------------------

def manual_vb_send(camera_trigger, lhs_active, rhs_active, capture_barcode, lh_bc_req, rh_bc_req):
    """
    Allows for us to bypass the PLC and send commands to vision builder from the GUI. Useful for debugging purposes
    if you want to force different values through to vision builder. Note that on the HMI there is manual buttons
    as well that allow for testing of vision builder manually, such as barcode capture.
    """
    global g_plc_rx_trigger_camera, g_plc_rx_capture_barcode
    global g_plc_rx_lhs_sequence_active, g_plc_rx_rhs_sequence_active
    global g_plc_rx_lh_barcode_req, g_plc_rx_rh_barcode_req
    global g_plc_rx_position, g_vb_send_done

    if g_connection_vb is None:
        messagebox.showwarning("Manual VBAI Test", "No active connection to Vision Builder. Cannot send.")
        return

    try:
        test_pos = int(g_manual_pos_entry.get())
    except (ValueError, AttributeError, TypeError):
        test_pos = 1

    g_plc_rx_trigger_camera = camera_trigger
    g_plc_rx_capture_barcode = capture_barcode
    g_plc_rx_lhs_sequence_active = lhs_active
    g_plc_rx_rhs_sequence_active = rhs_active
    g_plc_rx_lh_barcode_req = lh_bc_req
    g_plc_rx_rh_barcode_req = rh_bc_req
    g_plc_rx_position = test_pos
    g_vb_send_done = False  # Signals thread_vb() to build and send the packet on its next pass.

    log_message(f"[MANUAL TEST] Cam:{camera_trigger} BC:{capture_barcode} LHS:{lhs_active} "
                f"RHS:{rhs_active} LH_BC:{lh_bc_req} RH_BC:{rh_bc_req} Pos:{test_pos}")


def manual_vb_clear_flags():
    """
    Used in conjunction with manual_vb_send, as we need a way to clear all the manually set trigger flags that the GUI
    sends to vision builder. Especially important during debugging as any manual triggers are latching.
    """
    global g_plc_rx_trigger_camera, g_plc_rx_capture_barcode
    global g_plc_rx_lhs_sequence_active, g_plc_rx_rhs_sequence_active
    global g_plc_rx_lh_barcode_req, g_plc_rx_rh_barcode_req

    g_plc_rx_trigger_camera = False
    g_plc_rx_capture_barcode = False
    g_plc_rx_lhs_sequence_active = False
    g_plc_rx_rhs_sequence_active = False
    g_plc_rx_lh_barcode_req = False
    g_plc_rx_rh_barcode_req = False
    log_message("[MANUAL TEST] Cleared all manual RX flags.")

# ------------------------------------------------ IO list ----------------------------------------------------------------

def open_io_list_window():
    """
    The IO list is another sub window that can only be accessed on the GUI through clicking its dedicated button
    in the settings menu. Very helpful for debugging purposes as it allows us to see the current states of all the
    data tags being passed between the PLC and Python, along with the comms between Python and Vision Builder.
    This overview of the whole communication pipeline allows us to see how the comms look during a run of the rig
    and identify any problem such as triggers latching, faulty strings etc.
    """
    io_win = tk.Toplevel(root)
    io_win.title("Live IO List")
    io_win.geometry("1000x360")  # Can adjust size of window here.

    def add_column(parent, title):
        col = tk.LabelFrame(parent, text=title, font=("Arial", 9, "bold"), fg="#0c447c", padx=6, pady=4)
        col.pack(side="left", fill="both", expand=True, padx=4, pady=6)
        return col

    def add_row(parent, label_text):
        row = tk.Frame(parent)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label_text, font=("Arial", 9), anchor="w", width=18).pack(side="left")
        val_lbl = tk.Label(row, text="-", font=("Arial", 9, "bold"), anchor="w", width=10, bg="#e9ecef")
        val_lbl.pack(side="left")
        return val_lbl

    columns_frame = tk.Frame(io_win)
    columns_frame.pack(fill="both", expand=True)

# -------------------------------------- Received from PLC (IO List) ----------------------------------------------------------

    plc_rx_sec = add_column(columns_frame, "PLC -> Python (RX)")
    rows = {}
    pass
    rows['plc_rx_heartbeat'] = (add_row(plc_rx_sec, "Heartbeat"), lambda: g_plc_rx_heartbeat)
    rows['plc_rx_error'] = (add_row(plc_rx_sec, "Error"), lambda: g_plc_rx_error)
    rows['plc_rx_capture_barcode'] = (add_row(plc_rx_sec, "Capture Barcode"), lambda: g_plc_rx_capture_barcode)
    rows['plc_rx_trigger_camera'] = (add_row(plc_rx_sec, "Trigger Camera"), lambda: g_plc_rx_trigger_camera)
    rows['plc_rx_lhs_active'] = (add_row(plc_rx_sec, "LHS Active"), lambda: g_plc_rx_lhs_sequence_active)
    rows['plc_rx_rhs_active'] = (add_row(plc_rx_sec, "RHS Active"), lambda: g_plc_rx_rhs_sequence_active)
    rows['plc_rx_capture_results'] = (add_row(plc_rx_sec, "Capture Results"), lambda: g_plc_rx_capture_results)
    rows['plc_rx_lh_barcode_req'] = (add_row(plc_rx_sec, "BC Required LH"), lambda: g_plc_rx_lh_barcode_req)
    rows['plc_rx_rh_barcode_req'] = (add_row(plc_rx_sec, "BC Required RH"), lambda: g_plc_rx_rh_barcode_req)
    rows['plc_rx_position'] = (add_row(plc_rx_sec, "Position"), lambda: g_plc_rx_position)
    rows['plc_rx_recipe'] = (add_row(plc_rx_sec, "Recipe Selection"), lambda: g_plc_rx_recipe)


    # ------------------------------------------ Sent to PLC (IO List) -------------------------------------------------------------

    plc_tx_sec = add_column(columns_frame, "Python -> PLC (TX)")
    rows['plc_tx_heartbeat'] = (add_row(plc_tx_sec, "Heartbeat"), lambda: g_plc_tx_heartbeat)
    rows['plc_tx_error'] = (add_row(plc_tx_sec, "Error"), lambda: g_plc_tx_error)
    rows['plc_tx_barcode_pass'] = (add_row(plc_tx_sec, "Barcode Pass"), lambda: g_plc_tx_barcode_pass)
    rows['plc_tx_barcode_fail'] = (add_row(plc_tx_sec, "Barcode Fail"), lambda: g_plc_tx_barcode_fail)
    rows['plc_tx_camera_pass'] = (add_row(plc_tx_sec, "Camera Pass"), lambda: g_plc_tx_camera_pass)
    rows['plc_tx_camera_fail'] = (add_row(plc_tx_sec, "Camera Fail"), lambda: g_plc_tx_camera_fail)
    rows['plc_tx_capture_complete'] = (add_row(plc_tx_sec, "Capture Complete"), lambda: g_plc_tx_capture_complete)
    rows['plc_tx_ready'] = (add_row(plc_tx_sec, "Ready"), lambda: g_plc_tx_ready)
    rows['plc_tx_barcode_string'] = (add_row(plc_tx_sec, "Barcode String"), lambda: g_plc_tx_barcode_string)
    rows['plc_tx_position_echo'] = (add_row(plc_tx_sec, "Position Echo"), lambda: g_plc_tx_position_echo)

    # ------------------------------------------- Sent to Vision Builder (IO List) -------------------------------------------------

    vb_tx_sec = add_column(columns_frame, "Python -> VBAI (TX)")
    rows['vb_tx_trigger_camera'] = (add_row(vb_tx_sec, "Trigger Camera"), lambda: g_vb_tx_trigger_camera)
    rows['vb_tx_lhs'] = (add_row(vb_tx_sec, "LHS Active"), lambda: g_vb_tx_lhs)
    rows['vb_tx_rhs'] = (add_row(vb_tx_sec, "RHS Active"), lambda: g_vb_tx_rhs)
    rows['vb_tx_lh_barcode'] = (add_row(vb_tx_sec, "LH BC Trigger"), lambda: g_vb_tx_lh_barcode)
    rows['vb_tx_rh_barcode'] = (add_row(vb_tx_sec, "RH BC Trigger"), lambda: g_vb_tx_rh_barcode)
    rows['vb_tx_position'] = (add_row(vb_tx_sec, "Position"), lambda: g_vb_tx_position)


    # --------------------------------------- Received from Vision Builder (IO List) ----------------------------------------------
    vb_rx_sec = add_column(columns_frame, "VBAI -> Python (RX)")
    rows['vb_rx_camera_ready'] = (add_row(vb_rx_sec, "Camera Ready"), lambda: g_vb_rx_camera_ready)
    rows['vb_rx_trigger_complete'] = (add_row(vb_rx_sec, "Trigger Complete"), lambda: g_vb_rx_trigger_complete)
    rows['vb_rx_trigger_fail'] = (add_row(vb_rx_sec, "Trigger Fail"), lambda: g_vb_rx_trigger_fail)
    rows['vb_rx_barcode_complete'] = (add_row(vb_rx_sec, "Barcode Complete"), lambda: g_vb_rx_barcode_complete)
    rows['vb_rx_barcode_fail'] = (add_row(vb_rx_sec, "Barcode Fail"), lambda: g_vb_rx_barcode_fail)
    rows['vb_rx_position_echo'] = (add_row(vb_rx_sec, "Position Echo"), lambda: g_vb_rx_position_echo)
    rows['vb_rx_scanned_barcode'] = (add_row(vb_rx_sec, "Scanned Barcode"), lambda: g_vb_rx_scanned_barcode)

    def refresh():
        """
        Sub function within the IO list for controlling the refresh of the screen to make it dynamically update
        to any changes in the communication flags.
        """
        if not io_win.winfo_exists():
            return
        for val_lbl, getter in rows.values():
            try:
                val = getter()
            except Exception:
                val = "?"
            if isinstance(val, bool):
                val_lbl.config(text="TRUE" if val else "false", bg="#198754" if val else "#e9ecef",
                                fg="white" if val else "black")
            else:
                val_lbl.config(text=str(val), bg="#e9ecef", fg="black")
        io_win.after(150, refresh)

    refresh()

# ----------------------------------------------------- Settings Menu ----------------------------------------------------------

def open_settings_window():
    """
    Here we define our setting menu, which is a sub-menu accessed on the GUI through a button.
    Allows for the User to have access to manual controls of the GUI such as setting directory paths
    and opening the IO list for debugging purposes.
    """
    global g_run_btn, g_manual_pos_entry, g_master_dir_lbl, g_auto_save_dir_lbl
    settings_win = tk.Toplevel(root)
    settings_win.title("System Settings Panel")
    settings_win.geometry("580x480")
    settings_win.resizable(False, False)
    tk.Label(settings_win, text="System Configuration Controls", font=("Segoe UI", 12, "bold"), pady=10).pack()
    config_lf = tk.LabelFrame(settings_win, text=" Core Management ", padx=10, pady=8)
    config_lf.pack(fill="x", padx=15, pady=5)
    tk.Button(config_lf, text="Change Watch Directory", command=change_watch_directory, width=26, bg="#cbd5e1").grid(
        row=0, column=0, padx=5, pady=3)
    tk.Button(config_lf, text="Load Tolerance Template", command=load_tolerances_from_template, width=26,
              bg="#cbd5e1").grid(row=0, column=1, padx=5, pady=3)
    tk.Button(config_lf, text="Upload Master CSV Manually", command=select_master_file, width=26, bg="#cbd5e1").grid(
        row=1, column=0, padx=5, pady=3)
    g_run_btn = tk.Button(config_lf, text="Assess Data Manually", command=execute_assessment, state=tk.DISABLED,
                          bg="#198754", fg="white", width=26)
    g_run_btn.grid(row=1, column=1, padx=5, pady=3)
    tk.Button(config_lf, text="Change Master CSV Directory", command=change_master_csv_directory, width=26,
              bg="#cbd5e1").grid(row=2, column=0, padx=5, pady=3)
    master_dir_lbl = tk.Label(config_lf, text=f"Master CSV folder: {g_master_csv_directory}", font=("Arial", 7),
                              fg="#6c757d", wraplength=200, justify="left")
    master_dir_lbl.grid(row=2, column=1, padx=5, pady=3, sticky="w")
    g_master_dir_lbl = master_dir_lbl
    tk.Button(config_lf, text="Change Auto-Save Directory", command=change_auto_save_directory, width=26,
              bg="#cbd5e1").grid(row=3, column=0, padx=5, pady=3)
    auto_save_dir_lbl = tk.Label(config_lf, text=f"Auto-save folder: {g_auto_save_directory}", font=("Arial", 7),
                                 fg="#6c757d", wraplength=200, justify="left")
    auto_save_dir_lbl.grid(row=3, column=1, padx=5, pady=3, sticky="w")
    g_auto_save_dir_lbl = auto_save_dir_lbl

    # ----------------------------------------- Target Polling Overrides and Manual VBAI Test Panel ------------------------------------------------

    # Commented out for production use, can be uncommented to add functionality back to GUI.

    # These buttons allow for forcing which data is loaded into the GUI from the Vision builder watch directory
    # covers all three load states (LHS Only, RHS Only and BOTH)

    # sync_lf = tk.LabelFrame(settings_win, text=" Target Polling Overrides ", padx=10, pady=8);
    # sync_lf.pack(fill="x", padx=15, pady=5)
    # tk.Button(sync_lf, text="Sync LHS Only (5 Files)", command=lambda: auto_ingest_pipeline("LHS"), width=25,
    #           bg="#0dcaf0").grid(row=0, column=0, padx=5, pady=4)
    # tk.Button(sync_lf, text="Sync RHS Only (5 Files)", command=lambda: auto_ingest_pipeline("RHS"), width=25,
    #           bg="#ffc107").grid(row=0, column=1, padx=5, pady=4)
    # tk.Button(sync_lf, text="Synchronize Full Macro Dataset (10 Files)", command=lambda: auto_ingest_pipeline("BOTH"),
    #           width=54, bg="#212529", fg="white").grid(row=1, column=0, columnspan=2, padx=5, pady=4)

    maint_lf = tk.LabelFrame(settings_win, text=" Storage Maintenance ", padx=10, pady=8)
    maint_lf.pack(fill="x", padx=15, pady=5)
    tk.Button(maint_lf, text="Clear Dashboard Runtime Logs & Arrays", command=clear_all_data, width=54, bg="#f8d7da",
              fg="#842029").pack(pady=2)
    tk.Button(maint_lf, text="Save Assessment Manually 💾", command=save_assessment_report, width=54,
              bg="#198754", fg="white").pack(pady=2)

    tk.Button(settings_win, text="Open Live IO List", command=open_io_list_window, width=22, bg="#0c447c",
              fg="white").pack(pady=(8, 2))

    # -------------------------------------- Manual testing buttons for sending to Vision Builder -------------------------------------------------------

    # These are commented out for production use but if ever needed can be uncommented to bring back into the GUI.

    # These Buttons on the GUI allow for the user to manually trigger aspects of vision builder whilst it's
    # running in inspection mode.
    # These buttons bypass the PLC and write to the flags to send to vision builders TCP connection.
    # Note that pressing these buttons does latch the trigger so would need to be used in conjunction
    # with the clear flags buttons to return them to their default states.


    # vb_test_lf = tk.LabelFrame(settings_win, text=" Manual VBAI Test Panel (Engineering Use Only) ", padx=10, pady=8,
    #                            fg="#842029");
    # vb_test_lf.pack(fill="x", padx=15, pady=5)
    # tk.Label(vb_test_lf, text="Sends the same structure thread_vb() already sends, via the PLC RX flags it reads. "
    #                           "Use only with no PLC connected.", font=("Arial", 8), fg="#6c757d", wraplength=420,
    #          justify="left").pack(anchor="w", pady=(0, 6))
    #
    # pos_row = tk.Frame(vb_test_lf);
    # pos_row.pack(fill="x", pady=(0, 6))
    # tk.Label(pos_row, text="Test Position:", font=("Arial", 9, "bold")).pack(side="left")
    # g_manual_pos_entry = tk.Entry(pos_row, width=6, justify="center");
    # g_manual_pos_entry.pack(side="left", padx=8)
    # g_manual_pos_entry.insert(0, "1")
    #
    # cam_row = tk.Frame(vb_test_lf);
    # cam_row.pack(fill="x", pady=2)
    # tk.Button(cam_row, text="Trigger Camera - LHS",
    #           command=lambda: manual_vb_send(True, True, False, False, False, False), width=22,
    #           bg="#0dcaf0").pack(side="left", padx=3)
    # tk.Button(cam_row, text="Trigger Camera - RHS",
    #           command=lambda: manual_vb_send(True, False, True, False, False, False), width=22,
    #           bg="#ffc107").pack(side="left", padx=3)
    # tk.Button(cam_row, text="Trigger Camera - BOTH",
    #           command=lambda: manual_vb_send(True, True, True, False, False, False), width=22,
    #           bg="#212529", fg="white").pack(side="left", padx=3)
    #
    # bc_row = tk.Frame(vb_test_lf);
    # bc_row.pack(fill="x", pady=2)
    # tk.Button(bc_row, text="Request LH Barcode",
    #           command=lambda: manual_vb_send(False, False, False, True, True, False), width=22,
    #           bg="#0dcaf0").pack(side="left", padx=3)
    # tk.Button(bc_row, text="Request RH Barcode",
    #           command=lambda: manual_vb_send(False, False, False, True, False, True), width=22,
    #           bg="#ffc107").pack(side="left", padx=3)
    # tk.Button(bc_row, text="Request Both Barcodes",
    #           command=lambda: manual_vb_send(False, False, False, True, True, True), width=22,
    #           bg="#212529", fg="white").pack(side="left", padx=3)
    #
    # clear_row = tk.Frame(vb_test_lf);
    # clear_row.pack(fill="x", pady=(6, 0))
    # tk.Button(clear_row, text="Clear Manual RX Flags", command=manual_vb_clear_flags, width=70, bg="#f8d7da",
    #           fg="#842029").pack()

    tk.Button(settings_win, text="Exit Settings Menu", command=settings_win.destroy, width=18, bg="#6c757d",
              fg="white").pack(pady=12)
    check_run_conditions()


# ==================================== Vision Builder Thread ==================================================================

def thread_vb():
    """
    Establishes connection to Vision builder for sending commands and receiving status information on tcp
    builds a data structure to send and decodes the same structure when vision builder.  
    """
    # Python to PLC
    global g_connection_vb
    global g_plc_tx_barcode_string
    global g_plc_tx_barcode_pass
    global g_plc_tx_barcode_fail
    global g_plc_tx_camera_pass
    global g_plc_tx_camera_fail
    global g_plc_tx_error_code

    # PLC to Python
    global g_plc_rx_heartbeat
    global g_plc_rx_error
    global g_plc_rx_capture_barcode
    global g_plc_rx_trigger_camera
    global g_plc_rx_lhs_sequence_active
    global g_plc_rx_rhs_sequence_active
    global g_plc_rx_capture_results
    global g_plc_rx_lh_barcode_req
    global g_plc_rx_rh_barcode_req
    global g_plc_rx_error_code
    global g_plc_rx_position
    global g_plc_rx_recipe
    global g_plc_rx_barcode
    global g_plc_rx_master_csv

    # Python to VB
    global g_vb_tx_trigger_camera
    global g_vb_tx_lhs
    global g_vb_tx_rhs
    global g_vb_tx_lh_barcode
    global g_vb_tx_rh_barcode
    global g_vb_tx_position

    # VB to Python
    global g_vb_rx_camera_ready
    global g_vb_rx_trigger_complete
    global g_vb_rx_trigger_fail
    global g_vb_rx_barcode_complete
    global g_vb_rx_barcode_fail
    global g_vb_rx_position_echo

    # others
    global g_vb_send_done
    global g_vb_mode
    global g_vb_lhs
    global g_vb_rhs
    global g_vb_lh_bc_trigger
    global g_vb_rh_bc_trigger
    global g_vb_position
    global g_vb_rx_scanned_barcode

    # Initialize all per-loop-pass states before the while so values don't get stuck between packets.
    do_camera_send = False
    do_barcode_send = False
    just_sent = False
    l_vbsend_iteration_complete = False

    while g_system_running:

        # Trying to establish connection with Vision builder.
        if g_connection_vb is None:
            try:
                l_connection_vb = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                l_connection_vb.settimeout(20.0)
                l_connection_vb.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) #uses a KEEPALIVE socket method to maintain the connection during IDLE
                l_connection_vb.connect((VBAI_IP, VBAI_PORT)) # IP address and TCP Port set at top of script so that they can be easily adjusted

                with vbai_lock:
                    g_connection_vb = l_connection_vb
                g_gui_queue.put(("VBAI_CONNECTION", "CONNECTED"))
            except Exception: #If connection drops, then we update the status label, wait then jump back to the top to attempt a reconnect.
                with vbai_lock:
                    g_connection_vb = None
                g_gui_queue.put(("VBAI_CONNECTION", "DISCONNECTED"))
                time.sleep(2.0)
                continue

        # If connection successful, begin send/recieve.
        try:
            # Reset per-pass send flags at the top of every pass so stale True values from a
            # previous pass can never trigger send on the next pass.
            do_camera_send = False
            do_barcode_send = False
            just_sent = False

            # ------------------------------------------- Send to Vision Builder -----------------------------------------------------------

            # Gate on the PLC TX pass/fail fields rather than an internal latch: a trigger is sent only
            # while it's high AND we haven't already recorded a pass or fail for it. Those fields get set
            # from VB's reply below, and cleared once the PLC drops its trigger bit.
            #
            # Falling edge: if pass/fail is still set when the PLC bit drops, that's the moment it just
            # went low - send one more packet so VB sees the trigger go to 0 too, not just our own state.
            # The packet-build below already reads live g_plc_rx_* values, so it naturally sends a 0 for
            # whichever bit just dropped.

            # Kept separate from the rising-edge booleans below because only a genuine new request should
            # drive the receive-side pass/fail mapping further.

            camera_rising_send = (g_plc_rx_trigger_camera is True) and (g_plc_tx_camera_pass is not True) and (
                        g_plc_tx_camera_fail is not True)
            barcode_rising_send = (g_plc_rx_capture_barcode is True) and (g_plc_tx_barcode_pass is not True) and (
                        g_plc_tx_barcode_fail is not True)

            camera_falling_edge = (g_plc_rx_trigger_camera is False) and (
                        g_plc_tx_camera_pass is True or g_plc_tx_camera_fail is True)
            barcode_falling_edge = (g_plc_rx_capture_barcode is False) and (
                        g_plc_tx_barcode_pass is True or g_plc_tx_barcode_fail is True)

            # l_vbsend_iteration_complete blocks re-sending while the current trigger is still held high
            # after send has already been completed.
            # Brackets added to make explicit - without them 'or' was binding looser than 'and',
            # which would let camera_rising_send bypass the latch entirely, not allowing the one-shot send we were expecting.

            if (camera_rising_send is True) or (camera_falling_edge is True and l_vbsend_iteration_complete is False):
                do_camera_send = True

            if (barcode_rising_send is True) or (barcode_falling_edge is True and l_vbsend_iteration_complete is False):
                do_barcode_send = True

            if do_camera_send or do_barcode_send:

                if g_plc_rx_trigger_camera: # Checks the mode from the plc rx to see if camera trigger needed.
                    l_vb_camera_trigger = 1
                else:
                    l_vb_camera_trigger = 0

                if g_plc_rx_lhs_sequence_active: #Checks the plc rx to see if LHS is requested.
                    l_vb_lhs_active = 1
                else:
                    l_vb_lhs_active = 0

                if g_plc_rx_rhs_sequence_active: #Checks the plc rx to see if RHS is requested.
                    l_vb_rhs_active = 1
                else:
                    l_vb_rhs_active = 0

                # LH/RH barcode trigger bits only ever pass through while capture_barcode is actually true.
                # This guards against a stale/latched LH or RH "required" bit sneaking through to VB when
                # there's no barcode capture happening at all.
                if g_plc_rx_capture_barcode:
                    if g_plc_rx_lh_barcode_req: #Checks the plc rx to see if LHS barcode requested.
                        l_vb_lh_bc_trigger = 1
                    else:
                        l_vb_lh_bc_trigger = 0

                    if g_plc_rx_rh_barcode_req: #Checks the plc rx to see if RHS barcode requested.
                        l_vb_rh_bc_trigger = 1
                    else:
                        l_vb_rh_bc_trigger = 0
                else:
                    l_vb_lh_bc_trigger = 0
                    l_vb_rh_bc_trigger = 0

                l_vb_position = g_plc_rx_position #Passes the position integer from the plc rx to our local variable.

                # Mirror into globals purely for live display on the IO list - does not affect what gets sent.
                g_vb_tx_trigger_camera = bool(l_vb_camera_trigger)
                g_vb_tx_lhs = bool(l_vb_lhs_active)
                g_vb_tx_rhs = bool(l_vb_rhs_active)
                g_vb_tx_lh_barcode = bool(l_vb_lh_bc_trigger)
                g_vb_tx_rh_barcode = bool(l_vb_rh_bc_trigger)
                g_vb_tx_position = l_vb_position

                tx_b0 = 0
                tx_b0 |= l_vb_camera_trigger << 0
                tx_b0 |= l_vb_lhs_active     << 1
                tx_b0 |= l_vb_rhs_active     << 2
                tx_b0 |= l_vb_lh_bc_trigger  << 3
                tx_b0 |= l_vb_rh_bc_trigger  << 4

                outbound_packet = struct.pack("<BBH", tx_b0, 0, int(l_vb_position)) #Note: also using '<' here as well instead of '!' for packet build
                g_connection_vb.sendall(outbound_packet)
                #On sending of packet we update our variables that get used for gating logic.
                g_vb_send_done = True
                just_sent = True
                l_vbsend_iteration_complete = True
                do_camera_send = False
                do_barcode_send = False

            # Reset the one-shot latch only once ALL active trigger conditions have dropped -
            # i.e. neither a rising nor falling edge is pending for either trigger type.
            # Original logic was inverted (used 'or' so it was almost always True)
            if (not camera_rising_send and not camera_falling_edge and
                    not barcode_rising_send and not barcode_falling_edge):
                l_vbsend_iteration_complete = False

            # Clear pass/fail once the PLC drops the bit, and we're not busy sending 
            # (just_sent is False means no send happened this pass).
            if (g_plc_rx_trigger_camera is False) and (just_sent is False):
                g_plc_tx_camera_pass = False
                g_plc_tx_camera_fail = False
            if (g_plc_rx_capture_barcode is False) and (just_sent is False):
                g_plc_tx_barcode_pass = False
                g_plc_tx_barcode_fail = False



# --------------------------------------------------------- Receive from Vision Builder ------------------------------------------------------------------------------

            if just_sent:
                # Use select with a short timeout rather than blocking directly on recv - if VB is busy
                # (e.g. saving an image) it may take longer to reply than a single loop pass. With a plain
                # recv(54) the loop would stall here for up to the full socket timeout (20s), blocking all
                # PLC flag checks and falling-edge sends in the meantime. select() lets us check if data
                # is ready and move on if not, so the PLC never sees Python go silent mid-sequence.
                readable, _, _ = select.select([g_connection_vb], [], [], 0.5)
                if readable:
                    inbound_raw = g_connection_vb.recv(54) # Get 54 bytes from VB.
                    if not inbound_raw or len(inbound_raw) < 54: # Check length to ensure we got everything.
                        raise socket.error("Connection closed by Vision Builder remote endpoint.") # If not raise error.

                    rx_byte0, rx_byte1, rx_pos_echo, rx_scanned_barcode = struct.unpack("<BBH50s", inbound_raw[:54])  # Decode 54 bytes from VB.
                    g_vb_rx_camera_ready = bool(rx_byte0 & (1 << 0))
                    g_vb_rx_trigger_complete = bool(rx_byte0 & (1 << 1))
                    g_vb_rx_trigger_fail = bool(rx_byte0 & (1 << 2))
                    g_vb_rx_barcode_complete = bool(rx_byte0 & (1 << 3))
                    g_vb_rx_barcode_fail = bool(rx_byte0 & (1 << 4))

                    g_vb_rx_position_echo = int (rx_pos_echo)
                    decoded_barcode = rx_scanned_barcode.decode('utf-8', errors='ignore').strip('\x00\r\n')
                    if decoded_barcode and decoded_barcode != "0":
                        g_vb_rx_scanned_barcode = decoded_barcode
                        g_plc_tx_barcode_string = decoded_barcode  # We are the source of this value - forward VB's scan result on to the PLC TX packet.

                    # Map VB's result bits onto the PLC TX pass/fail fields - but only when this cycle's send was a
                    # genuine new request (rising edge), not the falling-edge "trigger now off" notification above.
                    # Otherwise, a stale/empty reply to that packet could re-set a flag we just cleared.
                    if camera_rising_send:
                        g_plc_tx_camera_pass = g_vb_rx_trigger_complete
                        g_plc_tx_camera_fail = g_vb_rx_trigger_fail
                    if barcode_rising_send:
                        g_plc_tx_barcode_pass = g_vb_rx_barcode_complete
                        g_plc_tx_barcode_fail = g_vb_rx_barcode_fail

                    just_sent = False
                # else: VB hasn't replied yet (still processing/saving) - leave just_sent True so we
                # come back to receive on the next pass, but don't block; let the loop continue so
                # PLC flags keep getting checked and falling-edge sends can still fire.
            else:
                # Nothing to send this pass, so nothing for VB to reply to - skip recv() entirely rather than
                # blocking on receive with nothing coming. Short sleep avoids hogging the CPU while idle,
                # while still re-checking the PLC flags every 20ms instead of being stuck inside recv().
                  time.sleep(0.01)

        except Exception:
            with vbai_lock:
                if g_connection_vb:
                    g_connection_vb.close()
                g_connection_vb = None
            g_gui_queue.put(("VBAI_CONNECTION", "DISCONNECTED"))
            g_vb_send_done = False
            time.sleep(1.0)

# ============================================ PLC Thread ========================================================================================

def thread_plc():
    """
    This thread handles all comms between Python and the PLC. We pass a common data structure back and forth to the PLC.
    The structure is 80 bytes in length, with different bytes representing integer, booleans, and strings so that all
    required information is shared between the two.
    The main commands that the PLC sends are held within the first byte, where we use booleans to indicate the state of different commands

    The PLC also makes use of a specified send and recieve rate of 200ms and 0ms respectively for the tcp comms.
    A Rising and Falling edge detection is also included here to add more logic to how python reacts when triggers are turned on and dropped.

    One last thing to note is when the structure is packed and unpacked we use the '<' operator and not the '!' operator.
    This was found during testing that across both thread_vb and thread_plc that we needed to a Big Endian and little Endian conversions
    as instructions and messages were showing up incorrectly in the IO list indcating a decoding problem.

    """
    # Python to PLC
    global g_plc_tx_position_echo
    global g_plc_tx_recipe_echo
    global g_plc_tx_barcode_string
    global g_plc_tx_barcode_pass
    global g_plc_tx_barcode_fail
    global g_plc_tx_error
    global g_plc_tx_error_code
    global g_plc_tx_camera_pass
    global g_plc_tx_camera_fail
    global g_plc_tx_master_csv_string
    global g_plc_tx_capture_complete

    # PLC to Python
    global g_plc_rx_heartbeat
    global g_plc_rx_error
    global g_plc_rx_capture_barcode
    global g_plc_rx_trigger_camera
    global g_plc_rx_lhs_sequence_active
    global g_plc_rx_rhs_sequence_active
    global g_plc_rx_capture_results
    global g_capture_results_armed
    global g_plc_rx_lh_barcode_req
    global g_plc_rx_rh_barcode_req
    global g_plc_rx_error_code
    global g_plc_rx_position
    global g_plc_rx_recipe
    global g_plc_rx_barcode
    global g_plc_rx_master_csv

# Timer setup for send rate for PLC.
    timer_prev = datetime.now()



    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(('0.0.0.0', PLC_PORT)) # Connect to the PLC TCP Port, and bind to special socket to listen on all network interfaces.
        server_socket.listen(1) # Marks the socket to be "passive" that will wait for and accept client requests (non-blocking).
    except Exception:
        return
# Trying to establish connection to PLC.
    while g_system_running:
        client_socket = None
        try:
            client_socket, _ = server_socket.accept()
            g_gui_queue.put(("PLC_CONNECTION", "CONNECTED"))
            session_active = True # On establishing connection we indicate that the session has started.
            timer_prev = datetime.now()  # Reset timer for this connection.

# ---------------------------------------------- Send to PLC -------------------------------------------------------------------

# We use a created function for the TCP send block to the PLC as we need to send and recieve at a specific rate.
# The PLC is expecting the Python script to work on a 200ms send and 0ms recieve rate.

            def plc_cyclic_sender(sock):
                nonlocal session_active, timer_prev
                global g_plc_tx_ready
                while g_system_running and session_active:
                    try:
                        timer_current = datetime.now()
                        if (timer_current - timer_prev).total_seconds() > plc_send_rate:  # Checks to see if the current time elapsed is greater than the send rate.
                            timer_prev = timer_current

                            # Ready (byte0.7): only true while the PLC isn't asking for anything, and we
                            # have no pending pass/fail result still waiting to be cleared. Prevents the PLC
                            # from re-triggering before this cycle has fully settled.
                            g_plc_tx_ready = (
                                    g_plc_rx_trigger_camera is False and
                                    g_plc_rx_capture_barcode is False and
                                    g_plc_tx_camera_pass is False and
                                    g_plc_tx_camera_fail is False and
                                    g_plc_tx_barcode_pass is False and
                                    g_plc_tx_barcode_fail is False
                            )

                            # Populate the structure of PLC send.
                            tx_byte0 = 0
                            tx_byte0 |= g_plc_tx_heartbeat <<0
                            tx_byte0 |= g_plc_tx_error << 1
                            tx_byte0 |= g_plc_tx_barcode_pass << 2
                            tx_byte0 |= g_plc_tx_barcode_fail << 3
                            tx_byte0 |= g_plc_tx_camera_pass << 4
                            tx_byte0 |= g_plc_tx_camera_fail << 5
                            tx_byte0 |= g_plc_tx_capture_complete << 6
                            tx_byte0 |= g_plc_tx_ready << 7

                            # Note: for building the tcp packet we use '<' instead of '!' as we need to do a Big Endian to Little Endian Conversion
                            encoded_bc = g_plc_tx_barcode_string.encode('utf-8')[:50].ljust(50, b'\x00')
                            encoded_mcsv = g_plc_tx_master_csv_string.encode('utf-8')[:20].ljust(20, b'\x00')
                            packet = struct.pack("<BBBBHH50s20sH", tx_byte0, 0, 0, 0, g_plc_tx_error_code, g_plc_tx_position_echo,
                                                 encoded_bc, encoded_mcsv, g_plc_tx_recipe_echo)
                            sock.sendall(packet)
                    except Exception:
                        session_active = False; break
                    time.sleep(0.200)

            threading.Thread(target=plc_cyclic_sender, args=(client_socket,), daemon=True).start() #Sub thread of thread_plc that manages our timed send

# -------------------------------------------------------- Receive from PLC -----------------------------------------------------------------------

            prev_capture_barcode = False
            prev_trigger_camera = False
            latch_lhs_seen = False  # latches True the moment LHS sequence goes active, held until next cycle
            latch_rhs_seen = False  # latches True the moment RHS sequence goes active, held until next cycle
            while g_system_running and session_active:
                try:
                    data = client_socket.recv(80)  # Receive 80 Bytes from PLC.
                    if not data or len(data) < 80: break  # Check that we are getting the correct amount of bytes.

                    byte0, _, byte2, _, _, robot_pos, _, master_csv_bytes, recipe_selection = struct.unpack(
                        "<BBBBHH50s20sH", data[:80]) # Unpacks and decodes the structure sent from the PLC, also uses '<' instead of '!'.
                    g_plc_rx_capture_barcode = bool(byte0 & (1 << 2))
                    g_plc_rx_trigger_camera = bool(byte0 & (1 << 3))
                    g_plc_rx_lhs_sequence_active = bool(byte0 & (1 << 4))
                    g_plc_rx_rhs_sequence_active = bool(byte0 & (1 << 5))
                    g_plc_rx_capture_results = bool(byte0 & (1 << 6))

                    # Capture Barcode signals the start of a new cycle. If it's high then any Capture
                    # Complete result from the previous cycle is now stale and must be cleared. This is
                    # mutually exclusive by definition (mid-cycle can't have a completed result from that
                    # same cycle) and guarantees Capture Complete is False well before the PLC ever checks
                    # it at the end of this cycle, regardless of how quickly it transitions from the previous.
                    if g_plc_rx_capture_barcode:
                        g_plc_tx_capture_complete = False

                    # Rising-edge detection: queue a CYCLE_START event the first time either trigger goes
                    # high so the GUI can clear its displayed data to signal a new cycle has begun.
                    # Only fires once per rising edge, not on every packet while the bit is held high.
                    if (g_plc_rx_capture_barcode and not prev_capture_barcode) or (
                            g_plc_rx_trigger_camera and not prev_trigger_camera):
                        g_gui_queue.put(("CYCLE_START", ""))

                    # Reset side-seen latches only at the genuine start of a new cycle (Capture Barcode
                    # rising edge) - NOT on every camera trigger rising edge, since camera triggers fire
                    # for every position including RHS ones in a BOTH run, which would wipe latch_lhs_seen
                    # before the RHS phase completes.
                    if g_plc_rx_capture_barcode and not prev_capture_barcode:
                        latch_lhs_seen = False
                        latch_rhs_seen = False

                    # Accumulate which sides have been active during this cycle - these stay True
                    # even after the sequence bit drops, so BOTH is correctly detected even though
                    # LHS and RHS are never simultaneously active (single camera moved between sides)
                    if g_plc_rx_lhs_sequence_active:
                        latch_lhs_seen = True
                    if g_plc_rx_rhs_sequence_active:
                        latch_rhs_seen = True

                    prev_capture_barcode = g_plc_rx_capture_barcode
                    prev_trigger_camera = g_plc_rx_trigger_camera

                    g_plc_rx_lh_barcode_req = bool(byte2 & (1 << 0))
                    g_plc_rx_rh_barcode_req = bool(byte2 & (1 << 1))

                    g_plc_rx_position = robot_pos
                    g_plc_rx_recipe = recipe_selection
                    g_plc_tx_recipe_echo = recipe_selection

                    plc_master_csv = master_csv_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n ')
                    # Only sends the data when the filename actually changes - avoids log spam and
                    # redundant reloads every packet while the PLC holds the same filename continuously.
                    if plc_master_csv and not bool(byte0 & (1 << 2)) and plc_master_csv != g_plc_rx_master_csv:
                        g_plc_rx_master_csv = plc_master_csv
                        g_plc_tx_master_csv_string = plc_master_csv
                        g_gui_queue.put(("PLC_MASTER_CSV", plc_master_csv))

                    # Rising edge only - sends exactly once, not on every packet while the PLC
                    # holds Capture Results high (which would re-run the full ingest + assessment repeatedly).
                    if g_plc_rx_capture_results is True and not g_capture_results_armed:
                        g_capture_results_armed = True
                        # Use latched side-seen values rather than live sequence bits - for a BOTH run
                        # the camera moves from LHS to RHS sequentially so both bits are never high at
                        # the same time. The latches accumulate which sides fired during this cycle.
                        if latch_lhs_seen and latch_rhs_seen:
                            ingest_mode = "BOTH"
                        elif latch_lhs_seen:
                            ingest_mode = "LHS"
                        else:
                            ingest_mode = "RHS"
                        g_gui_queue.put(("AUTO_INGEST_TRIGGER", ingest_mode))

                    # Falling edge - the PLC has dropped Capture Results, so clear capture_complete. Without
                    # this, capture_complete stays latched True from the previous run forever (nothing else
                    # ever clears it), so on every run after the first, the PLC sees it already True before
                    # the new ingest has even started and proceeds without ever seeing a fresh signal.
                    if g_plc_rx_capture_results is False and g_capture_results_armed:
                        g_capture_results_armed = False
                        g_plc_tx_capture_complete = False  # Must clear here - PLC drops Capture Results only after seeing Capture Complete True, so this is the only moment guaranteed to be before the next cycle's rising edge

                except Exception:
                    break
        except Exception:
            pass
        finally:
            session_active = False
            if client_socket: client_socket.close()
            g_gui_queue.put(("PLC_CONNECTION", "DISCONNECTED"))
            time.sleep(1.0)

# ====================================================== Heartbeat Thread ======================================================================

# This threads sole purpose is for running the heartbeat boolean bit within the data structure sent to the PLC.
# This heartbeat pulses at 1 second intervals and allows the PLC to know whether the tcp connection is being maintained.

# In the event of the script not being open or a fault with the IPC, the user can see on the HMI an alarm being displayed
# for the loss of connection to the IPC and prevents the User from going into Auto mode.

def thread_heartbeat():
    """
    Pulses the heartbeat bit at a 1s interval
    """
    global g_plc_tx_heartbeat
    while g_system_running:
        g_plc_tx_heartbeat = not g_plc_tx_heartbeat; time.sleep(1.0)


# ======================================================= GUI Layout Construction =============================================================

# This last section of the script is where we construct the GUI window and how its layout will look.
# This is also where we map functions that were built previously to their corresponding buttons on the UI.


# ------------------------------------------ Defining the Window ----------------------------------------------------------------------------------
root = tk.Tk()
root.title("GR1036 HUD Test Rig Dashboard")
root.geometry("1200x900")  #Adjust the size of the window on the screen, useful if elements are being cut off.


# ------------------------------------------------ COMPANY LOGO HEADER -----------------------------------------------------------------------------
# Here we load in the image files for the Granroth and Shatterprufe logos and place them into the header frame.


header_frame = tk.Frame(root, bg="white", padx=15, pady=8)
header_frame.pack(fill="x", side="top")

# Configure a 3-column layout grid to handle Left Logo, Centered Title, Right Logo
header_frame.columnconfigure(0, weight=1)
header_frame.columnconfigure(1, weight=2)
header_frame.columnconfigure(2, weight=1)

# Far-Left Logo Block (Granroth Logo)
left_logo_frame = tk.Frame(header_frame, bg="white")
left_logo_frame.grid(row=0, column=0, sticky="w", padx=15)

try: # Loads in the chosen image file from, pulls from Python script folder, and resizes it for use in the GUI. Has a fallback incase file cant be loaded to prevent script from failing.
    logo1_path = os.path.join(os.path.dirname(__file__), "granroth_logo.png") 
    if os.path.exists(logo1_path):
        logo1_pil = Image.open(logo1_path).resize((180, 70), Image.Resampling.LANCZOS)
        g_logo1_img = ImageTk.PhotoImage(logo1_pil)
        tk.Label(left_logo_frame, image=g_logo1_img, bg="white").pack()
    else:
        tk.Label(left_logo_frame, text="[ GRANROTH LOGO  ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10, #In the event of someone changing it, default to text to not break the GUI.
                 pady=5, borderwidth=1, relief="groove").pack()
except Exception:
    tk.Label(left_logo_frame, text="[ LOGO 1 ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10,
             pady=5, borderwidth=1, relief="groove").pack()


# Centered System Title Card.
tk.Label(header_frame, text="GR1036 HUD TEST RIG - Image Assessment", font=("Segoe UI", 14, "bold"), fg="#1e293b", bg="white").grid(row=0, column=1)

# Far-Right Logo Block (Shatterprufe Logo).
right_logo_frame = tk.Frame(header_frame, bg="white")
right_logo_frame.grid(row=0, column=2, sticky="e", padx=15)

try:
    logo2_path = os.path.join(os.path.dirname(__file__), "shatterprufe_logo.png")
    if os.path.exists(logo2_path):
        logo2_pil = Image.open(logo2_path).resize((180, 80), Image.Resampling.LANCZOS)
        g_logo2_img = ImageTk.PhotoImage(logo2_pil)
        tk.Label(right_logo_frame, image=g_logo2_img, bg="white").pack()
    else:
        tk.Label(right_logo_frame, text="[ SHATTERPRUFE LOGO  ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10,
                 pady=5, borderwidth=1, relief="groove").pack()
except Exception:
    tk.Label(right_logo_frame, text="[ LOGO 2 ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10,
             pady=5, borderwidth=1, relief="groove").pack()

tk.Frame(root, height=2, bg="#cbd5e1").pack(fill="x", side="top", pady=(0, 5))

# ------------------------------------------- Overview Panel ---------------------------------------------------
# Here we create a frame where we show the current status of data loads and which directory is being watched.
# Also the setting button is placed here.

summary_frame = tk.Frame(root, padx=15, pady=6, bg="#f8f9fa", borderwidth=1, relief="groove")
summary_frame.pack(fill="x", padx=15, pady=5)
master_label = tk.Label(summary_frame, text="Master File Empty", fg="red", font=("Arial", 9, "bold"), bg="#f8f9fa",
                        width=22, anchor="w")
master_label.pack(side="left", padx=5)
test_label = tk.Label(summary_frame, text="Test Files Empty", fg="red", font=("Arial", 9, "bold"), bg="#f8f9fa",
                      width=40, anchor="w")
test_label.pack(side="left", padx=5)
dir_lbl = tk.Label(summary_frame, text=f"Watching: {g_watch_directory}", fg="#0d6efd", font=("Segoe UI", 9), bg="#f8f9fa",
                   anchor="w")
dir_lbl.pack(side="left", fill="x", expand=True, padx=10)
settings_btn = tk.Button(summary_frame, text="Settings ⚙", command=open_settings_window, font=("Arial", 10, "bold"),
                         bg="#0d6efd", fg="white", padx=15, pady=2)
settings_btn.pack(side="right", padx=5)

# -------------------------------------------- Overview Slot Selection Grid  -------------------------------------------
# Here we create the Status overview frame where the labeled buttons for each robot position for LHS and RHS.
# These the labels on these buttons are dynamically updated through the process to show their states and statuses.
# The User can click on each of them to populate the metrics field to view the related data for that position.

global_frame = tk.LabelFrame(root, text=" Position Status Overview (Click a position to see its parameters) ", padx=10, pady=10)
global_frame.pack(fill="x", padx=15, pady=5)
lh_overview_buttons, rh_overview_buttons = {}, {}

# LHS: position button and drift chart button stacked in the same column frame
tk.Label(global_frame, text="LHS Positions:", font=("Arial", 9, "bold")).grid(row=0, column=0, padx=5, pady=(5,0), sticky="w")
tk.Label(global_frame, text="", font=("Arial", 7)).grid(row=1, column=0, pady=8)  # spacer for chart row
for i in range(1, 6):
    sf = tk.Frame(global_frame)
    sf.grid(row=0, column=i, rowspan=2, padx=20, pady=5, sticky="n")
    tk.Label(sf, text=f"Pos {i}:", font=("Arial", 9, "bold")).pack(anchor="center")
    btn = tk.Button(sf, text="IDLE", bg="lightgray", width=10,
                    command=lambda pos=i: select_and_view_position("LHS", pos))
    btn.pack(pady=(0, 3))
    lh_overview_buttons[i] = btn
    tk.Button(sf, text="📈 Chart", width=10, bg="#cbd5e1", fg="black", font=("Arial", 9),
              command=lambda pos=i: show_drift_chart("LHS", pos)).pack()

# RHS: position button and drift chart button stacked in the same column frame.
tk.Label(global_frame, text="RHS Positions:", font=("Arial", 9, "bold")).grid(row=2, column=0, padx=5, pady=(10,0), sticky="w")
tk.Label(global_frame, text="", font=("Arial", 7)).grid(row=3, column=0, pady=0)
for i in range(1, 6):
    sf = tk.Frame(global_frame)
    sf.grid(row=2, column=i, rowspan=2, padx=15, pady=(10, 5), sticky="n")
    tk.Label(sf, text=f"Pos {i}:", font=("Arial", 9, "bold")).pack(anchor="center")
    btn = tk.Button(sf, text="IDLE", bg="lightgray", width=10,
                    command=lambda pos=i: select_and_view_position("RHS", pos))
    btn.pack(pady=(0, 3))
    rh_overview_buttons[i] = btn
    tk.Button(sf, text="📈 Chart", width=10, bg="#cbd5e1", fg="black", font=("Arial", 9),
              command=lambda pos=i: show_drift_chart("RHS", pos)).pack()

# Overall pass/fail label spans all rows on the right.
overall_status_lbl = tk.Label(global_frame, text="SYSTEM IDLE", bg="lightgray", fg="black",
                              font=("Arial", 20, "bold"), width=20, borderwidth=2, relief="solid")
overall_status_lbl.grid(row=0, column=9, rowspan=5, padx=(20, 5), pady=5, sticky="nsew")
global_frame.grid_columnconfigure(6, weight=0)

# ----------------------------------------------- ASSESSMENT DISPLAY -----------------------------------------------------------
# Here we create our grid for laying out all the data we pulled in and processed.
# We have multiple columns to represent the diffrent data for each metric being assessed.
# The user can also enter their own values in the tolerance coloumn for each metric if they wish to change the default value,
# however they wont be saved in the script after closing it and it will default back to the currently loaded tolerance template


matrix_frame = tk.LabelFrame(root, text=" Position Parameters Overview ", padx=10, pady=10)
matrix_frame.pack(fill="x", padx=15, pady=5)
current_view_label = tk.Label(matrix_frame, text="Viewing: LHS - Position 1", font=("Arial", 10, "bold"), fg="#0d6efd")
current_view_label.grid(row=0, column=0, columnspan=6, sticky="w", pady=5)
headers = ["Evaluation Metric", "Master Baseline", "Test Target", "Tolerance Value", "Calculated Variance",
           "Status Indicator"]
for col_idx, text_header in enumerate(headers): tk.Label(matrix_frame, text=text_header, font=("Arial", 9, "bold"),
                                                         borderwidth=1, relief="solid", bg="#f8f9fa").grid(row=1,
                                                                                                           column=col_idx,
                                                                                                           sticky="nsew")

metrics_list = [('size', 'Image Size'), ('rotation', 'Image Rotation'), ('trap_h', 'Trapezoidal Dist. H'),
                ('trap_v', 'Trapezoidal Dist. V'), ('ar', 'Aspect Ratio'), ('trans_x', 'Translation X'),
                ('trans_y', 'Translation Y'), ('smile_h', 'Smile Distortion H'), ('smile_v', 'Smile Distortion V'),
                ('ghosting', 'Ghosting Distance')]
ui_rows, tol_inputs = {}, {}
for row_idx, (key, label_text) in enumerate(metrics_list, start=2):
    tk.Label(matrix_frame, text=label_text, anchor="w", borderwidth=1, relief="groove").grid(row=row_idx, column=0,
                                                                                             sticky="nsew")
    m_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14)
    m_val.grid(row=row_idx, column=1, sticky="nsew")
    t_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14)
    t_val.grid(row=row_idx, column=2, sticky="nsew")
    tol_ent = tk.Entry(matrix_frame, justify="center", width=12)
    tol_ent.grid(row=row_idx, column=3, padx=10, pady=5)
    tol_inputs[key] = tol_ent
    v_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=15)
    v_val.grid(row=row_idx, column=4, sticky="nsew")
    s_box = tk.Label(matrix_frame, text=" IDLE ", bg="lightgray", font=("Arial", 9, "bold"), borderwidth=1,
                     relief="sunken", width=10)
    s_box.grid(row=row_idx, column=5, padx=15, pady=5)
    ui_rows[key] = {'master': m_val, 'test': t_val, 'variance': v_val, 'status': s_box}
for c in range(6): matrix_frame.grid_columnconfigure(c, weight=1)

# ----------------------------------------- FOOTER RUNTIME STATUS -----------------------------------------
# This frame gives a live status view of the TCP connection between the Python script, PLC and vision builder.
# Also where the message log box sits so that the User can see all the current timestamped log messages.

status_bar_frame = tk.Frame(root, padx=15, pady=10)
status_bar_frame.pack(fill="x", side="bottom")
plc_status_lbl = tk.Label(status_bar_frame, text="PLC DISCONNECTED", bg="red", fg="white", font=("Arial", 13, "bold"),
                          width=22, borderwidth=1, relief="solid")
plc_status_lbl.pack(side="left", padx=5)
vbai_status_lbl = tk.Label(status_bar_frame, text="VBAI DISCONNECTED", bg="red", fg="white", font=("Arial", 13, "bold"),
                           width=22, borderwidth=1, relief="solid")
vbai_status_lbl.pack(side="left", padx=5)

log = ScrolledText(status_bar_frame, state="disabled", height=10)
log.pack(padx=30, pady=15, fill="both", expand=True)

# Load default base metrics.
defaults = {'size': '10.0', 'rotation': '3.0', 'trap_h': '5.0', 'trap_v': '5.0', 'ar': '0.5', 'trans_x': '20.0',
            'trans_y': '20.0', 'smile_h': '5.0', 'smile_v': '5.0', 'ghosting': '5.0'}
for k, e in tol_inputs.items():
    if k in defaults: e.insert(0, defaults[k])


# ---------------------------------------------------- Start threads -------------------------------------------------------
# Note: we set all our threads here to be daemons. This ensures they act as background threads. Unlike regular
#       threads, daemon threads do not block the Python program from exiting as they can self-terminate.

thread_vb.pending_trigger = None

threading.Thread(target=thread_plc, daemon=True).start()

threading.Thread(target=thread_vb, daemon=True).start()

threading.Thread(target=thread_heartbeat, daemon=True).start()

#--------------------------------------------------- Start GUI Event Loop ---------------------------------------------
root.after(100, status_network)
root.protocol("WM_DELETE_WINDOW", shutdown_application)
root.mainloop()  # Please note, anything after this line will not run, as tkinter uses this blocking method to start the applications event loop
                 # which keeps the GUI window active and responsive to user inputs.