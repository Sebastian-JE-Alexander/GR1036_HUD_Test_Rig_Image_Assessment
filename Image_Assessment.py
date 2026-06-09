"""
GR1036 HUD Test Rig
Image Assessment GUI & PLC / NI Vision Builder Broker

Customer calculations:
1) Image Size (units = mm)
2) Image Rotation (units = degrees)
3) Trapezoidal Distortion (Split into H and V)
4) Aspect Ratio (ratio expressed as decimal value)
5) Translation (Combined distance in mm)
6) Smile (Converted to mm)
7) Ghosting Distance (Converted to mm across all 77 points)
"""

import pandas as pd  # needed for data manipulation of the csv file
import tkinter as tk  # used for creating the GUI
from tkinter import filedialog, messagebox, ttk  # allows us to create dialogue boxes for the GUI
import numpy as np  # handles our math functions
from datetime import datetime  # used for timestamping our created files
import csv
import os
from PIL import Image, ImageTk  # used for soft image processing on the GUI, such as resizing imported images
import socket
import struct
import threading
import time
from queue import Queue, Empty

# Global variables to store our data states
master_df = None

# Active watch directory for automatic ingestion
watch_directory = "C:\\VBAI_Data_Exports"  # Default fallback path

# DPI Conversion Factor Constants (96 DPI -> 25.4 mm per inch)
MM_PER_PX = 25.4 / 96.0

# Dual-variant databases to hold dataframes for up to 5 robot positions each
lh_positions_db = {}  # { 1: df, 2: df, ... 5: df }
rh_positions_db = {}  # { 1: df, 2: df, ... 5: df }

# Compiled calculation records
lh_results_db = {}
rh_results_db = {}

# Thread-safe pipeline communication channel
gui_queue = Queue()

# Global communication outbox variables (Python -> PLC telemetry registers)
tx_heartbeat = False
tx_error = False
tx_barcode_pass = False
tx_barcode_fail = False
tx_camera_pass = False
tx_camera_fail = False
tx_error_code = 0
tx_position_echo = 0
tx_barcode_string = ""
tx_master_csv_string = ""  # Echo storage for the newly added Byte 56-75 field

# Engine run state tracker
system_running = True

# Network Configuration parameters
PLC_IP = "192.168.10.3"
PLC_PORT = 9005
VBAI_IP = "127.0.0.1"
VBAI_PORT = 9006


# ============================ Data Management & Core Sorting ================================

def load_data(file_path):
    """
    Reads and cleans CSV data. Accounts for a multi-row header, targets
    Columns F and G for Primary, and Columns H and I for Ghost coordinates.
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
        raise ValueError(f"The CSV structure is invalid. Expected at least 9 data columns.")

    target_cols = ['x_prim', 'y_prim', 'x_ghost', 'y_ghost']
    for col in target_cols:
        df[col] = df[col].astype(str).str.replace(',', '.')
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=target_cols)

    if len(df) != 77:
        raise ValueError(f"Grid integrity check failed. Expected exactly 77 points, found {len(df)}.")

    return df


def get_grid_points(df):
    y_min, y_max = df['y_prim'].min(), df['y_prim'].max()
    total_height = y_max - y_min
    approx_row_spacing = total_height / 6

    df['row_group'] = ((df['y_prim'] - y_min) / approx_row_spacing).round()
    grid = df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True)

    points = {
        "top_left": grid.iloc[0],
        "top_mid": grid.iloc[5],
        "top_right": grid.iloc[10],
        "center": grid.iloc[38],
        "bottom_left": grid.iloc[66],
        "bottom_mid": grid.iloc[71],
        "bottom_right": grid.iloc[76]
    }
    return points


def change_watch_directory():
    global watch_directory
    selected_dir = filedialog.askdirectory(title="Select Vision Builder Output Directory")
    if selected_dir:
        watch_directory = os.path.normpath(selected_dir)
        dir_lbl.config(text=f"Watching: {watch_directory}", fg="blue")


def auto_ingest_pipeline(mode="BOTH"):
    global lh_positions_db, rh_positions_db

    if not os.path.exists(watch_directory):
        messagebox.showerror("Directory Error", f"The watch directory does not exist:\n{watch_directory}")
        return

    if mode == "LHS" or mode == "BOTH": lh_positions_db.clear()
    if mode == "RHS" or mode == "BOTH": rh_positions_db.clear()

    all_raw_files = []
    for entry in os.listdir(watch_directory):
        full_path = os.path.join(watch_directory, entry)
        if os.path.isfile(full_path) and entry.lower().endswith('.csv'):
            all_raw_files.append((full_path, entry.lower(), os.path.getmtime(full_path)))

    # Harvest LHS
    loaded_lh = 0
    if mode == "LHS" or mode == "BOTH":
        lhs_candidates = [f for f in all_raw_files if "lh" in f[1]]
        lhs_candidates.sort(key=lambda x: x[2], reverse=True)
        for path, filename, _ in lhs_candidates[:5]:
            detected_pos = None
            for i in range(1, 6):
                if f"pos{i}" in filename: detected_pos = i; break
            if detected_pos:
                try:
                    lh_positions_db[detected_pos] = load_data(path)
                    loaded_lh += 1
                except Exception:
                    pass

    # Harvest RHS
    loaded_rh = 0
    if mode == "RHS" or mode == "BOTH":
        rhs_candidates = [f for f in all_raw_files if "rh" in f[1]]
        rhs_candidates.sort(key=lambda x: x[2], reverse=True)
        for path, filename, _ in rhs_candidates[:5]:
            detected_pos = None
            for i in range(1, 6):
                if f"pos{i}" in filename: detected_pos = i; break
            if detected_pos:
                try:
                    rh_positions_db[detected_pos] = load_data(path)
                    loaded_rh += 1
                except Exception:
                    pass

    if mode == "BOTH":
        test_label.config(text=f"Matrix: {loaded_lh} LHS / {loaded_rh} RHS (10 Files)", fg="green",
                          font=("Arial", 9, "bold"))
    elif mode == "LHS":
        test_label.config(text=f"Matrix: {loaded_lh} LHS Only (RHS Idle)", fg="#0dcaf0", font=("Arial", 9, "bold"))
    elif mode == "RHS":
        test_label.config(text=f"Matrix: {loaded_rh} RHS Only (LHS Idle)", fg="#ffc107", font=("Arial", 9, "bold"))

    check_run_conditions()
    if master_df is not None:
        execute_assessment()


def select_master_file():
    global master_df
    file_path = filedialog.askopenfilename(title="Select Master CSV File", filetypes=[("CSV files", "*.csv")])
    if file_path:
        try:
            master_df = load_data(file_path)
            master_label.config(text="Master: Loaded", fg="green", font=("Arial", 9, "bold"))
        except Exception as e:
            master_df = None
            master_label.config(text="Master: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Master CSV:\n\n{str(e)}")
        check_run_conditions()


def load_tolerances_from_template():
    """
    Reads a text template configuration file and updates GUI evaluation entries.
    """
    target_file = filedialog.askopenfilename(title="Open Tolerance Settings Template File",
                                             filetypes=[("Text Documents", "*.txt"), ("All Files", "*.*")])
    if not target_file:
        return

    key_mapping = {
        "image size": ["size"],
        "image rotation": ["rotation"],
        "trapezoidal dist.": ["trap_h", "trap_v"],
        "trapezoidal dist. h": ["trap_h"],
        "trapezoidal dist. v": ["trap_v"],
        "aspect ratio": ["ar"],
        "translation": ["translation"],
        "smile distortion": ["smile"],
        "ghosting distance": ["ghosting"]
    }

    try:
        loaded_count = 0
        with open(target_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    raw_key, raw_val = line.split("=", 1)
                    clean_key = raw_key.strip().lower()
                    if clean_key in key_mapping:
                        ui_tags = key_mapping[clean_key]
                        val_str = raw_val.strip()
                        for ui_tag in ui_tags:
                            if ui_tag in tol_inputs:
                                tol_inputs[ui_tag].delete(0, tk.END)
                                tol_inputs[ui_tag].insert(0, val_str)
                                loaded_count += 1

        if loaded_count > 0:
            messagebox.showinfo("Tolerances Configured",
                                f"Successfully loaded {loaded_count} evaluation thresholds from file.")
            if master_df is not None and (lh_positions_db or rh_positions_db):
                execute_assessment()
        else:
            messagebox.showwarning("Empty Template",
                                   "No matching parameters were processed. Check format of tolerance file.")
    except Exception as e:
        messagebox.showerror("Template Parse Block",
                             f"Error encountered reading tolerance parameters:\n\n{str(e)}")


def clear_all_data():
    global master_df, lh_positions_db, rh_positions_db, lh_results_db, rh_results_db
    if not messagebox.askyesno("Clear Dashboard", "Reset data arrays and clear loaded variant files?"):
        return

    master_df = None
    lh_positions_db.clear()
    rh_positions_db.clear()
    lh_results_db.clear()
    rh_results_db.clear()

    master_label.config(text="Master File Empty", fg="red", font=("Arial", 9, "normal"))
    test_label.config(text="Test Files Empty", fg="red", font=("Arial", 9, "normal"))
    check_run_conditions()

    for key in ui_rows:
        ui_rows[key]['master'].config(text="-")
        ui_rows[key]['test'].config(text="-")
        ui_rows[key]['variance'].config(text="-")
        ui_rows[key]['status'].config(bg="lightgray", text=" IDLE ", fg="black")

    for i in range(1, 6):
        lh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")
        rh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")

    overall_status_lbl.config(text="SYSTEM IDLE", bg="lightgray", fg="black")


def check_run_conditions():
    if master_df is not None and (len(lh_positions_db) > 0 or len(rh_positions_db) > 0):
        run_btn.config(state=tk.NORMAL, bg="#198754", fg="white")
    else:
        run_btn.config(state=tk.DISABLED, bg="#e0e0e0", fg="#a0a0a0")


def export_all_assessments_report():
    """
    Generates a complete multi-position record report tracking all current calculations.
    """
    if not lh_results_db and not rh_results_db:
        messagebox.showwarning("Export Blocked", "There are no evaluated assessment matrix records available to save.")
        return

    timestamp_string = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_report_name = f"Full_System_Assessment_Report_{timestamp_string}.csv"

    target_file_path = filedialog.asksaveasfilename(
        title="Export All Evaluation Logs",
        initialfile=default_report_name,
        filetypes=[("CSV Text Document", "*.csv")]
    )

    if not target_file_path:
        return

    try:
        with open(target_file_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(["GR1036 HUD Test Rig - Data Logging Calibration Report"])
            writer.writerow([f"Export Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
            writer.writerow([])

            for variant_name, target_db in [("Left-Hand Side (LHS)", lh_results_db),
                                            ("Right-Hand Side (RHS)", rh_results_db)]:
                writer.writerow([f"==================== {variant_name} Assessment Logs ===================="])
                writer.writerow([])

                for pos_idx in range(1, 6):
                    writer.writerow([f"--- Position {pos_idx} Matrix Status ---"])
                    if pos_idx not in target_db:
                        writer.writerow(["[NO INSPECTION DATA FOR THIS POSITION SLOT]"])
                        writer.writerow([])
                        continue

                    writer.writerow(["Parameter", "Master Reference", "Test Data",
                                     "Calculated Variance", "Pass/Fail Status"])
                    metrics = target_db[pos_idx]
                    for key in ['size', 'rotation', 'trap_h', 'trap_v', 'ar', 'translation', 'smile', 'ghosting']:
                        label, master_txt, test_txt, variance_txt, status_txt = metrics[key]
                        writer.writerow([label, master_txt, test_txt, variance_txt, status_txt])
                    writer.writerow([])

        messagebox.showinfo("Export Confirmed",
                            f"Complete inspection record successfully exported to:\n{os.path.basename(target_file_path)}")
    except Exception as e:
        messagebox.showerror("Export Error",
                             f"System encountered a block writing out the full database:\n\n{str(e)}")


def run_all_calculations(df):
    return {
        'image_size': df_size_calc(df),
        'aspect_ratio': df_ar_calc(df),
        'smile': df_smile_calc(df),
        'rotation': df_rot_calc(df),
        'translation': df_transl_calc(df),
        'trap_dist': df_trap_calc(df),
        'avg_ghosting': df_ghosting_calc(df)
    }


def df_size_calc(df): return (df['x_prim'].max() - df['x_prim'].min(),
                              df['y_prim'].max() - df['y_prim'].min()) if not df.empty else (0, 0)


def df_ar_calc(df):
    p = get_grid_points(df)
    w, h = p['top_right']['x_prim'] - p['top_left']['x_prim'], p['bottom_left']['y_prim'] - p['top_left']['y_prim']
    return w / h if h != 0 else 0


def df_smile_calc(df):
    p = get_grid_points(df)
    return p['top_mid']['y_prim'] - ((p['top_left']['y_prim'] + p['top_right']['y_prim']) / 2)


def df_rot_calc(df):
    try:
        tl, tr = df.loc[(df['x_prim'] + df['y_prim']).idxmin()], df.loc[(df['x_prim'] - df['y_prim']).idxmax()]
        return np.degrees(np.arctan2(tr['y_prim'] - tl['y_prim'], tr['x_prim'] - tl['x_prim']))
    except Exception:
        return 0.0


def df_transl_calc(df): p = get_grid_points(df); return p['center']['x_prim'], p['center']['y_prim']


def df_trap_calc(df):
    try:
        tl, br = df.loc[(df['x_prim'] + df['y_prim']).idxmin()], df.loc[(df['x_prim'] + df['y_prim']).idxmax()]
        tr, bl = df.loc[(df['x_prim'] - df['y_prim']).idxmax()], df.loc[(df['x_prim'] - df['y_prim']).idxmin()]
        tw, bw, lh, rh = tr['x_prim'] - tl['x_prim'], br['x_prim'] - bl['x_prim'], bl['y_prim'] - tl['y_prim'], br[
            'y_prim'] - tr['y_prim']
        return (abs(tw - bw) / min(tw, bw)) * 100, (abs(lh - rh) / min(lh, rh)) * 100
    except Exception:
        return 0.0, 0.0


def df_ghosting_calc(df):
    """
    Calculates the Euclidean distance between primary and ghost coordinates for all 77 points and averages them.
    """
    if df.empty: return 0.0
    distances = np.sqrt((df['x_ghost'] - df['x_prim']) ** 2 + (df['y_ghost'] - df['y_prim']) ** 2)
    return np.mean(distances)


def process_variant_database(source_db, results_db, m_res, overview_buttons):
    any_fail = False

    for i in range(1, 6):
        if i not in source_db:
            overview_buttons[i].config(bg="lightgray", text="EMPTY", fg="black")

    for pos_idx, t_df in source_db.items():
        failed_criteria_count = 0
        t_res = run_all_calculations(t_df)
        metrics = {}

        # 1. Image Size Calculations (Converted from Px to MM)
        m_w_mm = m_res['image_size'][0] * MM_PER_PX
        m_h_mm = m_res['image_size'][1] * MM_PER_PX
        t_w_mm = t_res['image_size'][0] * MM_PER_PX
        t_h_mm = t_res['image_size'][1] * MM_PER_PX

        w_diff = t_w_mm - m_w_mm
        h_diff = t_h_mm - m_h_mm
        max_size_diff = w_diff if abs(w_diff) >= abs(h_diff) else h_diff

        metrics['size'] = ("Image Size", f"{round(m_w_mm, 1)}x{round(m_h_mm, 1)} mm",
                           f"{round(t_w_mm, 1)}x{round(t_h_mm, 1)} mm", f"{round(max_size_diff, 3)} mm",
                           "PASS" if abs(max_size_diff) <= float(tol_inputs['size'].get()) else "FAIL")

        # 2. Image Rotation Calculations (Stays Degrees)
        rot_diff = t_res['rotation'] - m_res['rotation']
        metrics['rotation'] = ("Image Rotation", f"{round(m_res['rotation'], 2)}°", f"{round(t_res['rotation'], 2)}°",
                               f"{round(rot_diff, 3)} °",
                               "PASS" if abs(rot_diff) <= float(tol_inputs['rotation'].get()) else "FAIL")

        # 3. Trapezoidal Distortion Calculations (Split into Horizontal and Vertical)
        m_trap_h, m_trap_v = m_res['trap_dist']
        t_trap_h, t_trap_v = t_res['trap_dist']

        trap_h_diff = t_trap_h - m_trap_h
        trap_v_diff = t_trap_v - m_trap_v

        metrics['trap_h'] = ("Trapezoidal Dist. H", f"{round(m_trap_h, 1)}%", f"{round(t_trap_h, 1)}%",
                             f"{round(trap_h_diff, 3)} % delta",
                             "PASS" if abs(trap_h_diff) <= float(tol_inputs['trap_h'].get()) else "FAIL")

        metrics['trap_v'] = ("Trapezoidal Dist. V", f"{round(m_trap_v, 1)}%", f"{round(t_trap_v, 1)}%",
                             f"{round(trap_v_diff, 3)} % delta",
                             "PASS" if abs(trap_v_diff) <= float(tol_inputs['trap_v'].get()) else "FAIL")

        # 4. Aspect Ratio Calculations (Stays Dimensionless)
        ar_diff = t_res['aspect_ratio'] - m_res['aspect_ratio']
        metrics['ar'] = ("Aspect Ratio", f"{round(m_res['aspect_ratio'], 3)}", f"{round(t_res['aspect_ratio'], 3)}",
                         f"{round(ar_diff, 3)}",
                         "PASS" if abs(ar_diff) <= float(tol_inputs['ar'].get()) else "FAIL")

        # 5. Reverted Combined Translation Calculations (Euclidean Center Point Distance in mm)
        m_trans_x, m_trans_y = m_res['translation']
        t_trans_x, t_trans_y = t_res['translation']

        trans_x_diff_mm = (t_trans_x - m_trans_x) * MM_PER_PX
        trans_y_diff_mm = (t_trans_y - m_trans_y) * MM_PER_PX
        trans_diff_mm = np.sqrt(trans_x_diff_mm ** 2 + trans_y_diff_mm ** 2)

        metrics['translation'] = ("Translation",
                                  f"X: {round(m_trans_x * MM_PER_PX, 1)} mm, Y: {round(m_trans_y * MM_PER_PX, 1)} mm",
                                  f"X: {round(t_trans_x * MM_PER_PX, 1)} mm, Y: {round(t_trans_y * MM_PER_PX, 1)} mm",
                                  f"{round(trans_diff_mm, 3)} mm",
                                  "PASS" if trans_diff_mm <= float(tol_inputs['translation'].get()) else "FAIL")

        # 6. Smile Distortion Calculations (Converted from Px to MM)
        smile_diff_px = t_res['smile'] - m_res['smile']
        smile_diff_mm = smile_diff_px * MM_PER_PX
        metrics['smile'] = ("Smile Distortion", f"{round(m_res['smile'] * MM_PER_PX, 2)} mm",
                            f"{round(t_res['smile'] * MM_PER_PX, 2)} mm", f"{round(smile_diff_mm, 3)} mm",
                            "PASS" if abs(smile_diff_mm) <= float(tol_inputs['smile'].get()) else "FAIL")

        # 7. Ghosting Distance Variance Calculations (Converted from Px to MM)
        ghost_diff_px = t_res['avg_ghosting'] - m_res['avg_ghosting']
        ghost_diff_mm = ghost_diff_px * MM_PER_PX
        metrics['ghosting'] = ("Ghosting Distance", f"{round(m_res['avg_ghosting'] * MM_PER_PX, 2)} mm",
                               f"{round(t_res['avg_ghosting'] * MM_PER_PX, 2)} mm", f"{round(ghost_diff_mm, 3)} mm",
                               "PASS" if abs(ghost_diff_mm) <= float(tol_inputs['ghosting'].get()) else "FAIL")

        for k in metrics:
            if metrics[k][4] == "FAIL":
                failed_criteria_count += 1
                any_fail = True

        results_db[pos_idx] = metrics

        if failed_criteria_count > 0:
            overview_buttons[pos_idx].config(bg="red", text="FAIL", fg="white")
        else:
            overview_buttons[pos_idx].config(bg="green", text="PASS", fg="white")

    return any_fail


def execute_assessment():
    global lh_results_db, rh_results_db, tx_camera_pass, tx_camera_fail, tx_error_code
    if master_df is None: return
    m_res = run_all_calculations(master_df)

    lh_failed = process_variant_database(lh_positions_db, lh_results_db, m_res, lh_overview_buttons)
    rh_failed = process_variant_database(rh_positions_db, rh_results_db, m_res, rh_overview_buttons)

    # Global assessment calculation mapping
    if lh_failed or rh_failed:
        tx_camera_pass, tx_camera_fail, tx_error_code = False, True, 101
        overall_status_lbl.config(text="SYSTEM FAIL", bg="red", fg="white")
    else:
        if len(lh_positions_db) == 0 and len(rh_positions_db) == 0:
            overall_status_lbl.config(text="SYSTEM IDLE", bg="lightgray", fg="black")
        else:
            tx_camera_pass, tx_camera_fail, tx_error_code = True, False, 0
            overall_status_lbl.config(text="SYSTEM PASS", bg="green", fg="white")

    refresh_displayed_position_metrics()


def select_and_view_position(variant, position_idx):
    """
    Callback function triggered when clicking any global status button.
    """
    current_view_label.config(text=f"Viewing: {variant} - Position {position_idx}")
    refresh_displayed_position_metrics(variant, position_idx)


def refresh_displayed_position_metrics(forced_variant=None, forced_pos=None):
    if forced_variant and forced_pos:
        selected_variant = forced_variant
        selected_pos = forced_pos
        current_view_label.target_variant = forced_variant
        current_view_label.target_pos = forced_pos
    else:
        selected_variant = getattr(current_view_label, 'target_variant', 'LHS')
        selected_pos = getattr(current_view_label, 'target_pos', 1)

    target_db = lh_results_db if selected_variant == "LHS" else rh_results_db

    if selected_pos not in target_db:
        for key in ui_rows:
            ui_rows[key]['master'].config(text="-")
            ui_rows[key]['test'].config(text="-")
            ui_rows[key]['variance'].config(text="-")
            ui_rows[key]['status'].config(bg="lightgray", text=" NO DATA ", fg="black")
        return

    metrics = target_db[selected_pos]
    for key in ui_rows:
        label, master_txt, test_txt, variance_txt, status_txt = metrics[key]
        ui_rows[key]['master'].config(text=master_txt)
        ui_rows[key]['test'].config(text=test_txt)
        ui_rows[key]['variance'].config(text=variance_txt)
        ui_rows[key]['status'].config(bg="green" if status_txt == "PASS" else "red", text=f" {status_txt} ", fg="white")


def open_settings_window():
    """
    Generates a transient modal settings popup for engineering controls.
    """
    settings_win = tk.Toplevel(root)
    settings_win.title("Rig Configuration Controls")
    settings_win.geometry("340x220")
    settings_win.resizable(False, False)
    settings_win.transient(root)  # Lock focus onto the subwindow
    settings_win.grab_set()

    tk.Label(settings_win, text="Settings Menu", font=("Arial", 11, "bold"), pady=12).pack()

    # Pack configuration items cleanly inside popover
    tk.Button(settings_win, text="📁 Set Watch Folder", command=change_watch_directory, width=24, bg="#e2e3e5",
              pady=4).pack(pady=6)
    tk.Button(settings_win, text="⚙️ Load Tolerances", command=load_tolerances_from_template, width=24,
              bg="#f8f9fa", pady=4).pack(pady=6)
    tk.Button(settings_win, text="🗑️ Clear Run Logs & Arrays", command=clear_all_data, width=24, bg="#dc3545",
              fg="white", font=("Arial", 9, "bold"), pady=4).pack(pady=6)


# ============================ NETWORK ENGINE HOOKS ============================

def handle_vbai_block_comms(vbai_socket, variant_command):
    if not vbai_socket: return "NOT_CONNECTED"
    try:
        vbai_socket.sendall(f"{variant_command}\r\n".encode('utf-8'))
        return vbai_socket.recv(1024).decode('utf-8').strip()
    except Exception:
        return "ERROR"


def plc_heartbeat_worker():
    global tx_heartbeat
    while system_running: tx_heartbeat = not tx_heartbeat; time.sleep(1.0)


def plc_network_broker_worker():
    global tx_position_echo, tx_barcode_string, tx_barcode_pass, tx_barcode_fail, tx_error, tx_error_code, tx_camera_pass, tx_camera_fail, tx_master_csv_string
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind((PLC_IP, PLC_PORT)); server_socket.listen(1)
    except Exception:
        return

    while system_running:
        client_socket = None;
        vbai_socket = None
        try:
            client_socket, addr = server_socket.accept()
            gui_queue.put(("PLC_CONNECTION", "CONNECTED"))
            try:
                vbai_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM); vbai_socket.connect(
                    (VBAI_IP, VBAI_PORT)); gui_queue.put(("VBAI_CONNECTION", "CONNECTED"))
            except Exception:
                pass

            while system_running:
                # Expanded buffer read window changed from 56 to 76 bytes total
                data = client_socket.recv(76)
                if not data or len(data) < 76: break

                # Expanded struct unpack incorporating the new 20-byte string field trailing at the end
                byte0, byte1, error_code, robot_pos, barcode_bytes, master_csv_bytes = struct.unpack("!BBHH50s20s",
                                                                                                     data[:76])
                tx_position_echo = robot_pos
                tx_barcode_string = barcode_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n ')

                # Extract and store the PLC Master CSV requested file string
                plc_master_csv = master_csv_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n ')
                tx_master_csv_string = plc_master_csv

                if plc_master_csv:
                    gui_queue.put(("PLC_MASTER_CSV", plc_master_csv))

                # Check control Bit 6 (Capture Results) from PLC Send mapping
                if bool(byte0 & (1 << 6)):
                    gui_queue.put(("PLC_CAPTURE_RESULTS", ""))

                # Trigger Camera verification sequences (Bit 3)
                if bool(byte0 & (1 << 3)) and vbai_socket:
                    variant_cmd = "LHS" if bool(byte0 & (1 << 4)) else "RHS" if bool(byte0 & (1 << 5)) else "RUN"
                    vbai_reply = handle_vbai_block_comms(vbai_socket, f"{variant_cmd}_POS{robot_pos}")

                    if "PASS" in vbai_reply or vbai_reply.startswith("1"):
                        tx_camera_pass, tx_camera_fail, tx_error_code = True, False, 0
                        gui_queue.put(("AUTO_INGEST_TRIGGER", variant_cmd))
                    else:
                        tx_camera_pass, tx_camera_fail, tx_error_code = False, True, 102

                # Comprehensive output status flag packing logic mapping to Byte 0 Python Send structure
                tx_byte0 = 0
                if tx_heartbeat:    tx_byte0 |= (1 << 0)
                if tx_error:        tx_byte0 |= (1 << 1)
                if tx_barcode_pass: tx_byte0 |= (1 << 2)
                if tx_barcode_fail: tx_byte0 |= (1 << 3)
                if tx_camera_pass:  tx_byte0 |= (1 << 4)
                if tx_camera_fail:  tx_byte0 |= (1 << 5)

                encoded_barcode = tx_barcode_string.encode('utf-8')[:50].ljust(50, b'\x00')
                encoded_master_csv = tx_master_csv_string.encode('utf-8')[:20].ljust(20, b'\x00')

                # Send the compiled 76-byte data frame packet back out to the PLC connection stream
                client_socket.sendall(
                    struct.pack("!BBHH50s20s", tx_byte0, 0, tx_error_code, tx_position_echo, encoded_barcode,
                                encoded_master_csv))
        except Exception:
            pass
        finally:
            if client_socket: client_socket.close()
            if vbai_socket: vbai_socket.close()
            time.sleep(1.0)


def listen_for_network_queue():
    global master_df
    try:
        while True:
            event_type, payload = gui_queue.get_nowait()
            if event_type == "PLC_CONNECTION":
                plc_status_lbl.config(text="PLC LINK ACTIVE" if payload == "CONNECTED" else "PLC DISCONNECTED",
                                      bg="green" if payload == "CONNECTED" else "red", fg="white")
            elif event_type == "VBAI_CONNECTION":
                vbai_status_lbl.config(text="VBAI LINK ACTIVE" if payload == "CONNECTED" else "VBAI DISCONNECTED",
                                       bg="green" if payload == "CONNECTED" else "red", fg="white")
            elif event_type == "AUTO_INGEST_TRIGGER":
                target_mode = "LHS" if payload == "LHS" else "RHS" if payload == "RHS" else "BOTH"
                auto_ingest_pipeline(mode=target_mode)
            elif event_type == "PLC_CAPTURE_RESULTS":
                if master_df is not None:
                    execute_assessment()
            elif event_type == "PLC_MASTER_CSV":
                # Safe auto-ingestion hook checking workspace paths for a match to the PLC's string request
                filename = payload if payload.lower().endswith('.csv') else payload + '.csv'
                possible_paths = [filename, os.path.join(watch_directory, filename)]
                loaded_auto = False
                for p in possible_paths:
                    if os.path.exists(p):
                        try:
                            master_df = load_data(p)
                            master_label.config(text=f"Master: {payload}", fg="green", font=("Arial", 9, "bold"))
                            check_run_conditions()
                            execute_assessment()
                            loaded_auto = True
                            break
                        except Exception:
                            pass
                if not loaded_auto and master_df is None:
                    master_label.config(text=f"PLC Req: {payload} (NF)", fg="orange", font=("Arial", 9, "bold"))
            gui_queue.task_done()
    except Empty:
        pass
    if system_running: root.after(50, listen_for_network_queue)


def shutdown_application():
    global system_running;
    system_running = False;
    root.destroy()


# ============================ GUI Construction =======================================

root = tk.Tk()
root.title("GR1036 HUD Test Rig Image Assessment Panel Dashboard")
root.geometry("1200x850")

# Top Branding & Title Header Block (Configured for 3-Column Split Distribution)
header_frame = tk.Frame(root, bg="white", padx=15, pady=8)
header_frame.pack(fill="x", side="top")

# --- COLUMN 1: Left-Hand Side Company Logo ---
logo_left_path = "granroth_logo.png"
try:
    pil_left = Image.open(logo_left_path)
    pil_left = pil_left.resize((160, 55), Image.Resampling.LANCZOS)
    logo_left_image = ImageTk.PhotoImage(pil_left)

    logo_left_lbl = tk.Label(header_frame, image=logo_left_image, bg="white")
    logo_left_lbl.image = logo_left_image  # Retain reference
    logo_left_lbl.pack(side="left", padx=5)
except Exception:
    logo_left_lbl = tk.Label(header_frame, text="[ PRIMARY LOGO ]", font=("Arial", 10, "bold"), fg="#6c757d",
                             bg="#e9ecef", padx=8, pady=15)
    logo_left_lbl.pack(side="left", padx=5)

# --- COLUMN 3: Right-Hand Side Partner Logo ---
logo_right_path = "shatterprufe_logo.png"
try:
    pil_right = Image.open(logo_right_path)
    pil_right = pil_right.resize((190, 90), Image.Resampling.LANCZOS)
    logo_right_image = ImageTk.PhotoImage(pil_right)

    logo_right_lbl = tk.Label(header_frame, image=logo_right_image, bg="white")
    logo_right_lbl.image = logo_right_image  # Retain reference
    logo_right_lbl.pack(side="right", padx=5)
except Exception:
    logo_right_lbl = tk.Label(header_frame, text="[ PARTNER LOGO ]", font=("Arial", 10, "bold"), fg="#6c757d",
                              bg="#e9ecef", padx=8, pady=15)
    logo_right_lbl.pack(side="right", padx=5)

# --- COLUMN 2: Centered Rig Title Banner Text ---
title_lbl = tk.Label(header_frame, text="GR1036 HUD Test Rig Image Assessment", font=("Segoe UI", 14, "bold"),
                     fg="#1e293b", bg="white")
title_lbl.pack(expand=True, pady=12)

# Decorative divider line beneath the header block
divider = tk.Frame(root, height=2, bg="#cbd5e1")
divider.pack(fill="x", side="top", pady=(0, 5))

# 1. Main Ingestion Control Options Frame
upload_frame = tk.LabelFrame(root, text=" Target Ingestion Control Options Profile ", padx=10, pady=10)
upload_frame.pack(fill="x", padx=15, pady=5)

master_btn = tk.Button(upload_frame, text="Upload Master CSV", command=select_master_file, width=18, bg="#d1e7dd")
master_btn.grid(row=0, column=0, padx=5, pady=5)
master_label = tk.Label(upload_frame, text="Master File Empty", fg="red", anchor="w", width=18)
master_label.grid(row=0, column=1, padx=5, pady=5)

lhs_sync_btn = tk.Button(upload_frame, text="Sync Only LHS (5)", command=lambda: auto_ingest_pipeline("LHS"),
                         bg="#cff4fc", width=16)
lhs_sync_btn.grid(row=0, column=2, padx=3, pady=5)
rhs_sync_btn = tk.Button(upload_frame, text="Sync Only RHS (5)", command=lambda: auto_ingest_pipeline("RHS"),
                         bg="#fff3cd", width=16)
rhs_sync_btn.grid(row=0, column=3, padx=3, pady=5)
both_sync_btn = tk.Button(upload_frame, text="Sync Both (10)", command=lambda: auto_ingest_pipeline("BOTH"),
                          bg="#d2f4ea", font=("Arial", 9, "bold"), width=15)
both_sync_btn.grid(row=0, column=4, padx=3, pady=5)

test_label = tk.Label(upload_frame, text="Test Files Empty", fg="red", anchor="w", width=30)
test_label.grid(row=0, column=5, padx=5, pady=5)

run_btn = tk.Button(upload_frame, text="Assess Data", command=execute_assessment, state=tk.DISABLED, bg="#198754",
                    fg="white", width=18)
run_btn.grid(row=1, column=0, padx=5, pady=5, sticky="w")

save_btn = tk.Button(upload_frame, text="💾 Save Assessment", command=export_all_assessments_report, bg="#f8f9fa",
                     fg="black", width=18, font=("Arial", 9, "bold"))
save_btn.grid(row=1, column=1, padx=5, pady=5, sticky="w")

# Engineering Controls Menu hook (Opens Settings Window Modal Pop-up)
settings_btn = tk.Button(upload_frame, text="⚙️ Settings Menu", command=open_settings_window, bg="#e2e3e5", fg="black",
                         width=16, font=("Arial", 9, "bold"))
settings_btn.grid(row=1, column=2, padx=5, pady=5, sticky="w")

dir_lbl = tk.Label(upload_frame, text=f"Watching: {watch_directory}", fg="blue", anchor="w")
dir_lbl.grid(row=1, column=3, columnspan=3, padx=5, pady=5, sticky="w")

# 2. Global Multi-Position Macro Variant Status Matrix
global_frame = tk.LabelFrame(root,
                             text=" Variant Master Global Status Overview Matrix (Click an active status position to view detailed parameters) ",
                             padx=10, pady=10)
global_frame.pack(fill="x", padx=15, pady=5)

# LHS Row Elements
tk.Label(global_frame, text="LHS Variant Matrix Status: ", font=("Arial", 9, "bold"), anchor="e", width=22).grid(row=0,
                                                                                                                 column=0,
                                                                                                                 padx=5,
                                                                                                                 pady=5,
                                                                                                                 sticky="w")
lh_overview_buttons = {}
for i in range(1, 6):
    tk.Label(global_frame, text=f"Pos {i}", font=("Arial", 9, "normal"), bg="#f8f9fa", width=8, borderwidth=1,
             relief="solid").grid(row=0, column=(i * 2) - 1, padx=2, pady=5)
    btn = tk.Button(global_frame, text="IDLE", font=("Arial", 9, "bold"), bg="lightgray", fg="black", width=12,
                    borderwidth=1, relief="raised",
                    command=lambda pos=i: select_and_view_position("LHS", pos))
    btn.grid(row=0, column=i * 2, padx=5, pady=5)
    lh_overview_buttons[i] = btn

# RHS Row Elements
tk.Label(global_frame, text="RHS Variant Matrix Status: ", font=("Arial", 9, "bold"), anchor="e", width=22).grid(row=1,
                                                                                                                 column=0,
                                                                                                                 padx=5,
                                                                                                                 pady=5,
                                                                                                                 sticky="w")
rh_overview_buttons = {}
for i in range(1, 6):
    tk.Label(global_frame, text=f"Pos {i}", font=("Arial", 9, "normal"), bg="#f8f9fa", width=8, borderwidth=1,
             relief="solid").grid(row=1, column=(i * 2) - 1, padx=2, pady=5)
    btn = tk.Button(global_frame, text="IDLE", font=("Arial", 9, "bold"), bg="lightgray", fg="black", width=12,
                    borderwidth=1, relief="raised",
                    command=lambda pos=i: select_and_view_position("RHS", pos))
    btn.grid(row=1, column=i * 2, padx=5, pady=5)
    rh_overview_buttons[i] = btn

# 3. Calculation Parameter Micro Evaluation Matrix Block
matrix_frame = tk.LabelFrame(root, text=" Position Micro-Evaluation Parameters Grid ", padx=10, pady=10)
matrix_frame.pack(fill="x", padx=15, pady=5)

selector_subframe = tk.Frame(matrix_frame, pady=5)
selector_subframe.grid(row=0, column=0, columnspan=6, sticky="w")

current_view_label = tk.Label(selector_subframe, text="Viewing: LHS - Position 1", font=("Arial", 10, "bold"),
                              fg="#0d6efd")
current_view_label.pack(side="left", padx=5)
current_view_label.target_variant = 'LHS'
current_view_label.target_pos = 1

headers = ["Evaluation Metric", "Master Baseline", "Test Target", "Tolerance Value", "Calculated Variance",
           "Status Indicator"]
for col_idx, text_header in enumerate(headers):
    tk.Label(matrix_frame, text=text_header, font=("Arial", 9, "bold"), borderwidth=1, relief="solid", padx=5, pady=5,
             bg="#f8f9fa").grid(row=1, column=col_idx, sticky="nsew")

metrics_list = [
    ('size', 'Image Size'),
    ('rotation', 'Image Rotation'),
    ('trap_h', 'Trapezoidal Dist. H'),
    ('trap_v', 'Trapezoidal Dist. V'),
    ('ar', 'Aspect Ratio'),
    ('translation', 'Translation'),
    ('smile', 'Smile Distortion'),
    ('ghosting', 'Ghosting Distance')
]
ui_rows = {};
tol_inputs = {}

for row_idx, (key, label_text) in enumerate(metrics_list, start=2):
    tk.Label(matrix_frame, text=label_text, anchor="w", font=("Arial", 9), borderwidth=1, relief="groove", padx=5,
             pady=5).grid(row=row_idx, column=0, sticky="nsew")
    m_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14)
    m_val.grid(row=row_idx, column=1, sticky="nsew")
    t_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14)
    t_val.grid(row=row_idx, column=2, sticky="nsew")
    tol_ent = tk.Entry(matrix_frame, font=("Arial", 9), justify="center", width=12)
    tol_ent.grid(row=row_idx, column=3, padx=10, pady=5);
    tol_inputs[key] = tol_ent
    v_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=15)
    v_val.grid(row=row_idx, column=4, sticky="nsew")
    s_box = tk.Label(matrix_frame, text=" IDLE ", bg="lightgray", font=("Arial", 9, "bold"), borderwidth=1,
                     relief="sunken", width=10)
    s_box.grid(row=row_idx, column=5, padx=15, pady=5)
    ui_rows[key] = {'master': m_val, 'test': t_val, 'variance': v_val, 'status': s_box}

for c in range(6): matrix_frame.grid_columnconfigure(c, weight=1)

# 4. Streamlined Network Connection Indicator Bars & Overall Evaluation Banner
status_bar_frame = tk.Frame(root, padx=15, pady=10)
status_bar_frame.pack(fill="x")

plc_status_lbl = tk.Label(status_bar_frame, text="PLC DISCONNECTED", bg="red", fg="white", font=("Arial", 9, "bold"),
                          width=22, pady=4, borderwidth=1, relief="solid")
plc_status_lbl.pack(side="left", padx=5)

vbai_status_lbl = tk.Label(status_bar_frame, text="VBAI DISCONNECTED", bg="red", fg="white", font=("Arial", 9, "bold"),
                           width=22, pady=4, borderwidth=1, relief="solid")
vbai_status_lbl.pack(side="left", padx=5)

# Overall Pass / Fail Dynamic Tracker
overall_status_lbl = tk.Label(status_bar_frame, text="SYSTEM IDLE", bg="lightgray", fg="black",
                              font=("Arial", 10, "bold"), width=24, pady=4, borderwidth=1, relief="solid")
overall_status_lbl.pack(side="right", padx=5)

# Initialize Clean Configuration Metrics Defaults (Spatials initialized directly in mm)
defaults = {
    'size': '2.0',  # max dimensional delta in mm
    'rotation': '2.0',  # degrees
    'trap_h': '1.0',  # % delta ratio H
    'trap_v': '1.0',  # % delta ratio V
    'ar': '0.05',  # aspect ratio threshold
    'translation': '5.0',  # combined Euclidean distance in mm
    'smile': '1.0',  # profile line arch peak variance in mm
    'ghosting': '1.0'  # average separation distance drift in mm
}
for k, e in tol_inputs.items():
    if k in defaults:
        e.insert(0, defaults[k])

# Fire background communication loop engine operations
threading.Thread(target=plc_network_broker_worker, daemon=True).start()
threading.Thread(target=plc_heartbeat_worker, daemon=True).start()
root.after(100, listen_for_network_queue)
root.protocol("WM_DELETE_WINDOW", shutdown_application)
root.mainloop()