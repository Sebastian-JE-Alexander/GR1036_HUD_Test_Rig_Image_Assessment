"""
GR1036 HUD Test Rig
Image Assessment GUI & PLC / NI Vision Builder Broker

Updates:
- Migrated NI Vision Builder AI TCP protocol from strings to fixed 4-byte structural packets.
- Implemented binary mapping layout for Byte 0 (Booleans), Byte 1 (Reserved), Bytes 2+3 (Robot Position Integer).
- Automatically updates PLC telemetry registers using the binary flags returned by VBAI.
"""

import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox
import numpy as np
from datetime import datetime
import csv
import os
from PIL import Image, ImageTk
import socket
import struct
import threading
import time
from queue import Queue, Empty

# Global variables to store our data states
master_df = None
watch_directory = "C:\\VBAI_Data_Exports"  # Default fallback path
MM_PER_PX = 25.4 / 96.0

# Dual-variant databases to hold dataframes for up to 5 robot positions each
lh_positions_db = {}
rh_positions_db = {}
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
tx_capture_complete = False
tx_error_code = 0
tx_position_echo = 0
tx_recipe_echo = 0
tx_barcode_string = ""
tx_master_csv_string = ""

# Shared cross-thread safe sockets
vbai_socket = None
vbai_lock = threading.Lock()
system_running = True
run_btn = None  # Reference for manual assessment button inside settings

# Network Configuration parameters
PLC_PORT = 9005
VBAI_IP = "127.0.0.1"
VBAI_PORT = 9006


# ============================ Data Management & Core Sorting ================================

def load_data(file_path):
    """Reads and cleans CSV data targeting grid coordinate sets."""
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
        raise ValueError("The CSV structure is invalid. Expected at least 9 data columns.")

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
    if not os.path.exists(watch_directory): return

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
        test_label.config(text=f"Matrix: {loaded_lh} LHS Only", fg="#0dcaf0", font=("Arial", 9, "bold"))
    elif mode == "RHS":
        test_label.config(text=f"Matrix: {loaded_rh} RHS Only", fg="#ffc107", font=("Arial", 9, "bold"))

    check_run_conditions()
    if master_df is not None: execute_assessment()


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


def save_assessment_report():
    """Exports the compiled assessment matrix data for all processed positions to a CSV file."""
    if not lh_results_db and not rh_results_db:
        messagebox.showwarning("Save Report", "No processed assessment data available to save.")
        return

    file_path = filedialog.asksaveasfilename(
        title="Save Assessment Report",
        defaultextension=".csv",
        filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
        initialfile=f"HUD_Assessment_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    if not file_path:
        return

    try:
        with open(file_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["GR1036 HUD Test Rig - Quality Assessment Report"])
            writer.writerow(["Timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            writer.writerow(["Scanned Barcode", tx_barcode_string if tx_barcode_string else "N/A"])
            writer.writerow([])

            writer.writerow(["Variant Side", "Position Number", "Evaluation Metric", "Master Baseline", "Test Target",
                             "Allowed Tolerance", "Calculated Variance", "Status Result"])

            # Record LHS datasets
            for pos, metrics in sorted(lh_results_db.items()):
                for key, data in metrics.items():
                    label, master_txt, test_txt, variance_txt, status_txt = data
                    tol_val = tol_inputs[key].get()
                    writer.writerow(["LHS", f"Position {pos}", label, master_txt, test_txt, tol_val, variance_txt,
                                     status_txt.strip()])

            # Record RHS datasets
            for pos, metrics in sorted(rh_results_db.items()):
                for key, data in metrics.items():
                    label, master_txt, test_txt, variance_txt, status_txt = data
                    tol_val = tol_inputs[key].get()
                    writer.writerow(["RHS", f"Position {pos}", label, master_txt, test_txt, tol_val, variance_txt,
                                     status_txt.strip()])

        messagebox.showinfo("Save Report", f"Assessment report successfully archived to:\n{file_path}")
    except Exception as e:
        messagebox.showerror("Export Failure", f"An error occurred while compiling the report file:\n{str(e)}")


def load_tolerances_from_template():
    target_file = filedialog.askopenfilename(title="Open Tolerance Settings Template File",
                                             filetypes=[("Text Documents", "*.txt"), ("All Files", "*.*")])
    if not target_file: return

    key_mapping = {
        "image size": ["size"], "image rotation": ["rotation"],
        "trapezoidal dist. h": ["trap_h"], "trapezoidal dist. v": ["trap_v"],
        "aspect ratio": ["ar"], "translation": ["translation"],
        "smile distortion": ["smile"], "ghosting distance": ["ghosting"]
    }

    try:
        loaded_count = 0
        with open(target_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                raw_key, raw_val = line.split("=", 1)
                clean_key = raw_key.strip().lower()
                if clean_key in key_mapping:
                    for ui_tag in key_mapping[clean_key]:
                        if ui_tag in tol_inputs:
                            tol_inputs[ui_tag].delete(0, tk.END)
                            tol_inputs[ui_tag].insert(0, raw_val.strip())
                            loaded_count += 1
        if loaded_count > 0 and master_df is not None: execute_assessment()
    except Exception as e:
        messagebox.showerror("Template Error", str(e))


def clear_all_data():
    global master_df, lh_positions_db, rh_positions_db, lh_results_db, rh_results_db
    if not messagebox.askyesno("Clear Dashboard", "Reset data arrays and clear loaded variant files?"): return
    master_df = None
    lh_positions_db.clear();
    rh_positions_db.clear()
    lh_results_db.clear();
    rh_results_db.clear()

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


def check_run_conditions():
    global run_btn
    if run_btn and run_btn.winfo_exists():
        if master_df is not None and (len(lh_positions_db) > 0 or len(rh_positions_db) > 0):
            run_btn.config(state=tk.NORMAL, bg="#198754", fg="white")
        else:
            run_btn.config(state=tk.DISABLED, bg="#e0e0e0", fg="#a0a0a0")


def run_all_calculations(df):
    return {
        'image_size': df_size_calc(df), 'aspect_ratio': df_ar_calc(df),
        'smile': df_smile_calc(df), 'rotation': df_rot_calc(df),
        'translation': df_transl_calc(df), 'trap_dist': df_trap_calc(df),
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
    if df.empty: return 0.0
    distances = np.sqrt((df['x_ghost'] - df['x_prim']) ** 2 + (df['y_ghost'] - df['y_prim']) ** 2)
    return np.mean(distances)


def process_variant_database(source_db, results_db, m_res, overview_buttons):
    any_fail = False
    for i in range(1, 6):
        if i not in source_db: overview_buttons[i].config(bg="lightgray", text="EMPTY", fg="black")

    for pos_idx, t_df in source_db.items():
        failed_criteria_count = 0
        t_res = run_all_calculations(t_df)
        metrics = {}

        m_w_mm, m_h_mm = m_res['image_size'][0] * MM_PER_PX, m_res['image_size'][1] * MM_PER_PX
        t_w_mm, t_h_mm = t_res['image_size'][0] * MM_PER_PX, t_res['image_size'][1] * MM_PER_PX
        w_diff, h_diff = t_w_mm - m_w_mm, t_h_mm - m_h_mm
        max_size_diff = w_diff if abs(w_diff) >= abs(h_diff) else h_diff
        metrics['size'] = ("Image Size", f"{round(m_w_mm, 1)}x{round(m_h_mm, 1)} mm",
                           f"{round(t_w_mm, 1)}x{round(t_h_mm, 1)} mm", f"{round(max_size_diff, 3)} mm",
                           "PASS" if abs(max_size_diff) <= float(tol_inputs['size'].get()) else "FAIL")

        rot_diff = t_res['rotation'] - m_res['rotation']
        metrics['rotation'] = ("Image Rotation", f"{round(m_res['rotation'], 2)}°", f"{round(t_res['rotation'], 2)}°",
                               f"{round(rot_diff, 3)} °",
                               "PASS" if abs(rot_diff) <= float(tol_inputs['rotation'].get()) else "FAIL")

        m_trap_h, m_trap_v = m_res['trap_dist']
        t_trap_h, t_trap_v = t_res['trap_dist']
        trap_h_diff, trap_v_diff = t_trap_h - m_trap_h, t_trap_v - m_trap_v
        metrics['trap_h'] = ("Trapezoidal Dist. H", f"{round(m_trap_h, 1)}%", f"{round(t_trap_h, 1)}%",
                             f"{round(trap_h_diff, 3)} % delta",
                             "PASS" if abs(trap_h_diff) <= float(tol_inputs['trap_h'].get()) else "FAIL")
        metrics['trap_v'] = ("Trapezoidal Dist. V", f"{round(m_trap_v, 1)}%", f"{round(t_trap_v, 1)}%",
                             f"{round(trap_v_diff, 3)} % delta",
                             "PASS" if abs(trap_v_diff) <= float(tol_inputs['trap_v'].get()) else "FAIL")

        ar_diff = t_res['aspect_ratio'] - m_res['aspect_ratio']
        metrics['ar'] = ("Aspect Ratio", f"{round(m_res['aspect_ratio'], 3)}", f"{round(t_res['aspect_ratio'], 3)}",
                         f"{round(ar_diff, 3)}", "PASS" if abs(ar_diff) <= float(tol_inputs['ar'].get()) else "FAIL")

        m_trans_x, m_trans_y = m_res['translation']
        t_trans_x, t_trans_y = t_res['translation']
        trans_x_diff_mm, trans_y_diff_mm = (t_trans_x - m_trans_x) * MM_PER_PX, (t_trans_y - m_trans_y) * MM_PER_PX
        trans_diff_mm = np.sqrt(trans_x_diff_mm ** 2 + trans_y_diff_mm ** 2)
        metrics['translation'] = ("Translation",
                                  f"X: {round(m_trans_x * MM_PER_PX, 1)} mm, Y: {round(m_trans_y * MM_PER_PX, 1)} mm",
                                  f"X: {round(t_trans_x * MM_PER_PX, 1)} mm, Y: {round(t_trans_y * MM_PER_PX, 1)} mm",
                                  f"{round(trans_diff_mm, 3)} mm",
                                  "PASS" if trans_diff_mm <= float(tol_inputs['translation'].get()) else "FAIL")

        smile_diff_mm = (t_res['smile'] - m_res['smile']) * MM_PER_PX
        metrics['smile'] = ("Smile Distortion", f"{round(m_res['smile'] * MM_PER_PX, 2)} mm",
                            f"{round(t_res['smile'] * MM_PER_PX, 2)} mm", f"{round(smile_diff_mm, 3)} mm",
                            "PASS" if abs(smile_diff_mm) <= float(tol_inputs['smile'].get()) else "FAIL")

        ghost_diff_mm = (t_res['avg_ghosting'] - m_res['avg_ghosting']) * MM_PER_PX
        metrics['ghosting'] = ("Ghosting Distance", f"{round(m_res['avg_ghosting'] * MM_PER_PX, 2)} mm",
                               f"{round(t_res['avg_ghosting'] * MM_PER_PX, 2)} mm", f"{round(ghost_diff_mm, 3)} mm",
                               "PASS" if abs(ghost_diff_mm) <= float(tol_inputs['ghosting'].get()) else "FAIL")

        for k in metrics:
            if metrics[k][4] == "FAIL":
                failed_criteria_count += 1;
                any_fail = True
        results_db[pos_idx] = metrics
        overview_buttons[pos_idx].config(bg="red" if failed_criteria_count > 0 else "green",
                                         text="FAIL" if failed_criteria_count > 0 else "PASS", fg="white")
    return any_fail


def execute_assessment():
    global lh_results_db, rh_results_db, tx_camera_pass, tx_camera_fail, tx_error_code, tx_capture_complete
    if master_df is None: return
    m_res = run_all_calculations(master_df)

    lh_failed = process_variant_database(lh_positions_db, lh_results_db, m_res, lh_overview_buttons)
    rh_failed = process_variant_database(rh_positions_db, rh_results_db, m_res, rh_overview_buttons)

    tx_capture_complete = True
    if lh_failed or rh_failed:
        tx_camera_pass, tx_camera_fail, tx_error_code = False, True, 101
        overall_status_lbl.config(text="FAIL", bg="red", fg="white", font=("Arial", 14, "bold"))
    else:
        if len(lh_positions_db) == 0 and len(rh_positions_db) == 0:
            overall_status_lbl.config(text="SYSTEM IDLE", bg="lightgray", fg="black", font=("Arial", 10, "bold"))
            tx_capture_complete = False
        else:
            tx_camera_pass, tx_camera_fail, tx_error_code = True, False, 0
            overall_status_lbl.config(text="PASS", bg="green", fg="white", font=("Arial", 14, "bold"))
    refresh_displayed_position_metrics()


def select_and_view_position(variant, position_idx):
    current_view_label.config(text=f"Viewing: {variant} - Position {position_idx}")
    refresh_displayed_position_metrics(variant, position_idx)


def refresh_displayed_position_metrics(forced_variant=None, forced_pos=None):
    if forced_variant and forced_pos:
        selected_variant, selected_pos = forced_variant, forced_pos
        current_view_label.target_variant, current_view_label.target_pos = forced_variant, forced_pos
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


# ==================== SUB-WINDOW COMPONENT INTERFACE MANAGEMENT ====================

def open_settings_window():
    """Constructs a clean popup container window for manual overrides."""
    global run_btn
    settings_win = tk.Toplevel(root)
    settings_win.title("System Settings & Operations Panel")
    settings_win.geometry("480x420")
    settings_win.resizable(False, False)
    settings_win.grab_set()  # Focus lock modal windowing

    tk.Label(settings_win, text="System Configuration Controls", font=("Segoe UI", 12, "bold"), pady=10).pack()

    # Block 1: File & Storage Remapping
    config_lf = tk.LabelFrame(settings_win, text=" Core Directory & Template Management ", padx=10, pady=8)
    config_lf.pack(fill="x", padx=15, pady=5)

    tk.Button(config_lf, text="Change Watch Directory", command=change_watch_directory, width=26, bg="#e2e8f0").grid(
        row=0, column=0, padx=5, pady=3)
    tk.Button(config_lf, text="Load Tolerance Template", command=load_tolerances_from_template, width=26,
              bg="#cbd5e1").grid(row=0, column=1, padx=5, pady=3)
    tk.Button(config_lf, text="Upload Master CSV Manually", command=select_master_file, width=26, bg="#d1e7dd").grid(
        row=1, column=0, padx=5, pady=3)

    run_btn = tk.Button(config_lf, text="Assess Data Manually", command=execute_assessment, state=tk.DISABLED,
                        bg="#198754", fg="white", width=26)
    run_btn.grid(row=1, column=1, padx=5, pady=3)

    # Block 2: Manual Pipeline Data Sync Override
    sync_lf = tk.LabelFrame(settings_win, text=" Manual Target Polling Overrides ", padx=10, pady=8)
    sync_lf.pack(fill="x", padx=15, pady=5)

    tk.Button(sync_lf, text="Sync LHS Only (5 Files)", command=lambda: auto_ingest_pipeline("LHS"), width=25,
              bg="#0dcaf0").grid(row=0, column=0, padx=5, pady=4)
    tk.Button(sync_lf, text="Sync RHS Only (5 Files)", command=lambda: auto_ingest_pipeline("RHS"), width=25,
              bg="#ffc107").grid(row=0, column=1, padx=5, pady=4)
    tk.Button(sync_lf, text="Synchronize Full Macro Dataset (10 Files)", command=lambda: auto_ingest_pipeline("BOTH"),
              width=54, bg="#212529", fg="white").grid(row=1, column=0, columnspan=2, padx=5, pady=4)

    # Block 3: Log Clear Flush Maintenance
    maint_lf = tk.LabelFrame(settings_win, text=" Storage Maintenance ", padx=10, pady=8)
    maint_lf.pack(fill="x", padx=15, pady=5)
    tk.Button(maint_lf, text="Clear Dashboard Runtime Logs & Arrays", command=clear_all_data, width=54, bg="#f8d7da",
              fg="#842029").pack(pady=2)

    # Close button out-of-frame
    tk.Button(settings_win, text="Exit Settings Menu", command=settings_win.destroy, width=18, bg="#6c757d",
              fg="white").pack(pady=12)

    check_run_conditions()


# ============================ VISION BUILDER ENGINE STRUCT BROKER ============================

def execute_vbai_binary_transaction(trigger_camera=False, lhs_variant=False, rhs_variant=False, lh_barcode=False,
                                    rh_barcode=False, robot_pos=0):
    """
    Executes a structured 4-byte network call with NI Vision Builder AI.

    Packs outputs into a "!BBH" sequence:
    - Byte 0: Packed Boolean bits (Trigger, Variants, Barcodes)
    - Byte 1: Reserved Null Padding
    - Bytes 2+3: Big-Endian 16-bit Robot Position Integer

    Returns: (camera_ready, trigger_complete, trigger_fail, position_echo)
    """
    global vbai_socket
    with vbai_lock:
        if not vbai_socket:
            return False, False, False, 0

        try:
            # --- PACK THE OUTBOUND BINARY PACKET ---
            byte0 = 0
            if trigger_camera: byte0 |= (1 << 0)
            if lhs_variant:    byte0 |= (1 << 1)
            if rhs_variant:    byte0 |= (1 << 2)
            if lh_barcode:     byte0 |= (1 << 3)
            if rh_barcode:     byte0 |= (1 << 4)

            byte1 = 0x00  # Reserved alignment byte

            # Pack using big-endian layout ('!') to align directly with LabVIEW network byte orders
            outbound_packet = struct.pack("!BBH", byte0, byte1, int(robot_pos))
            vbai_socket.sendall(outbound_packet)

            # --- UNPACK THE INBOUND RETURN BINARY PACKET (4 Bytes) ---
            inbound_raw = vbai_socket.recv(4)
            if not inbound_raw or len(inbound_raw) < 4:
                return False, False, False, 0

            rx_byte0, rx_byte1, rx_pos_echo = struct.unpack("!BBH", inbound_raw[:4])

            # Extract returned booleans mapped directly to spreadsheet assignments
            camera_ready = bool(rx_byte0 & (1 << 0))
            trigger_complete = bool(rx_byte0 & (1 << 1))
            trigger_fail = bool(rx_byte0 & (1 << 2))

            return camera_ready, trigger_complete, trigger_fail, rx_pos_echo

        except Exception:
            return False, False, False, 0


def vbai_dedicated_client_worker():
    """
    Manages active connection pooling with the Vision Builder system.
    Uses OS-level TCP Keepalives instead of dummy byte injections to prevent socket contention.
    """
    global vbai_socket
    while system_running:
        if vbai_socket is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(4.0)

                # --- Turn on OS-Level Keepalives instead of sending dummy bytes ---
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

                sock.connect((VBAI_IP, VBAI_PORT))
                with vbai_lock:
                    vbai_socket = sock
                gui_queue.put(("VBAI_CONNECTION", "CONNECTED"))
            except Exception:
                with vbai_lock:
                    vbai_socket = None
                gui_queue.put(("VBAI_CONNECTION", "DISCONNECTED"))
                time.sleep(2.0)
                continue
        else:
            # Passive health validation check:
            # We check if the socket has thrown an internal error flags without sending junk data
            try:
                # A zero-byte non-blocking peek will instantly detect an OS-level connection drop
                error_code = vbai_socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error_code != 0:
                    raise socket.error("OS Level Socket reported unrecoverable error.")
            except Exception:
                with vbai_lock:
                    if vbai_socket:
                        vbai_socket.close()
                    vbai_socket = None
                gui_queue.put(("VBAI_CONNECTION", "DISCONNECTED"))

        # Polling rate for socket verification
        time.sleep(1.0)


# =================== ASYNCHRONOUS DECOUPLED PLC BROKER ENGINE ===================

def plc_network_broker_worker():
    global tx_position_echo, tx_recipe_echo, tx_barcode_string, tx_barcode_pass, tx_barcode_fail, tx_error, tx_error_code, tx_camera_pass, tx_camera_fail, tx_master_csv_string, tx_capture_complete
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(('0.0.0.0', PLC_PORT))
        server_socket.listen(1)
    except Exception:
        return

    while system_running:
        client_socket = None
        try:
            client_socket, addr = server_socket.accept()
            gui_queue.put(("PLC_CONNECTION", "CONNECTED"))

            session_active = True

            # Cyclic background transmitter loop to PLC (Send Rate: 200ms)
            def plc_cyclic_sender(sock):
                nonlocal session_active
                while system_running and session_active:
                    try:
                        tx_byte0 = 0
                        if tx_heartbeat:        tx_byte0 |= (1 << 0)
                        if tx_error:            tx_byte0 |= (1 << 1)
                        if tx_barcode_pass:     tx_byte0 |= (1 << 2)
                        if tx_barcode_fail:     tx_byte0 |= (1 << 3)
                        if tx_camera_pass:      tx_byte0 |= (1 << 4)
                        if tx_camera_fail:      tx_byte0 |= (1 << 5)
                        if tx_capture_complete: tx_byte0 |= (1 << 6)

                        encoded_barcode = tx_barcode_string.encode('utf-8')[:50].ljust(50, b'\x00')
                        encoded_master_csv = tx_master_csv_string.encode('utf-8')[:20].ljust(20, b'\x00')

                        packet = struct.pack("!BBBBHH50s20sH", tx_byte0, 0, 0, 0, tx_error_code, tx_position_echo,
                                             encoded_barcode, encoded_master_csv, tx_recipe_echo)
                        sock.sendall(packet)
                    except Exception:
                        session_active = False
                        break
                    time.sleep(0.200)

            sender_thread = threading.Thread(target=plc_cyclic_sender, args=(client_socket,), daemon=True)
            sender_thread.start()

            # Fast Pipeline Ingestion Loop from PLC (Read Rate: Immediate continuous)
            while system_running and session_active:
                try:
                    data = client_socket.recv(80)
                    if not data or len(data) < 80:
                        break

                    byte0, byte1, byte2, byte3, error_code, robot_pos, barcode_bytes, master_csv_bytes, recipe_selection = struct.unpack(
                        "!BBBBHH50s20sH", data[:80])
                    tx_position_echo = robot_pos
                    tx_recipe_echo = recipe_selection

                    # Read PLC dynamic control values
                    plc_req_lh_barcode = bool(byte2 & (1 << 0))
                    plc_req_rh_barcode = bool(byte2 & (1 << 1))
                    plc_is_lhs_variant = bool(byte0 & (1 << 4))
                    plc_is_rhs_variant = bool(byte0 & (1 << 5))

                    plc_master_csv = master_csv_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n ')
                    if plc_master_csv and not bool(byte0 & (1 << 2)):
                        tx_master_csv_string = plc_master_csv
                        gui_queue.put(("PLC_MASTER_CSV", plc_master_csv))

                    if bool(byte0 & (1 << 6)):
                        gui_queue.put(("PLC_CAPTURE_RESULTS", ""))

                    # --- EXECUTE 4-BYTE HANDSHAKE DIRECTLY WITH VISION BUILDER ENGINE ---
                    is_camera_trigger = bool(byte0 & (1 << 3))
                    is_barcode_trigger = bool(byte0 & (1 << 2))

                    if is_camera_trigger or is_barcode_trigger:
                        # Unpack structural elements to pipe straight to Vision Builder
                        v_ready, v_complete, v_fail, v_pos_echo = execute_vbai_binary_transaction(
                            trigger_camera=is_camera_trigger,
                            lhs_variant=plc_is_lhs_variant,
                            rhs_variant=plc_is_rhs_variant,
                            lh_barcode=(is_barcode_trigger and plc_req_lh_barcode),
                            rh_barcode=(is_barcode_trigger and plc_req_rh_barcode),
                            robot_pos=robot_pos
                        )

                        # Process status registers based on structural response keys
                        if is_barcode_trigger:
                            if v_complete and not v_fail:
                                tx_barcode_pass, tx_barcode_fail, tx_error_code = True, False, 0
                                # (Optional) If barcode data logging is active, assign mock text strings
                                tx_barcode_string = f"SCANNED_POS_{v_pos_echo}"
                            else:
                                tx_barcode_pass, tx_barcode_fail, tx_error_code = False, True, 104

                        elif is_camera_trigger:
                            if v_complete and not v_fail:
                                tx_camera_pass, tx_camera_fail, tx_error_code = True, False, 0
                                active_side = "LHS" if plc_is_lhs_variant else "RHS"
                                gui_queue.put(("AUTO_INGEST_TRIGGER", active_side))
                            else:
                                tx_camera_pass, tx_camera_fail, tx_error_code = False, True, 102

                except Exception:
                    break

        except Exception:
            pass
        finally:
            session_active = False
            if client_socket:
                client_socket.close()
            gui_queue.put(("PLC_CONNECTION", "DISCONNECTED"))
            time.sleep(1.0)


def listen_for_network_queue():
    global master_df
    try:
        while True:
            event_type, payload = gui_queue.get_nowait()
            if event_type == "PLC_CONNECTION":
                plc_status_lbl.config(text="PLC LINK ACTIVE" if payload == "CONNECTED" else "PLC DISCONNECTED",
                                      bg="green" if payload == "CONNECTED" else "red")
            elif event_type == "VBAI_CONNECTION":
                vbai_status_lbl.config(text="VBAI LINK ACTIVE" if payload == "CONNECTED" else "VBAI DISCONNECTED",
                                       bg="green" if payload == "CONNECTED" else "red")
            elif event_type == "AUTO_INGEST_TRIGGER":
                auto_ingest_pipeline(mode=payload)
            elif event_type == "PLC_CAPTURE_RESULTS":
                if master_df is not None: execute_assessment()
            elif event_type == "PLC_MASTER_CSV":
                filename = payload if payload.lower().endswith('.csv') else payload + '.csv'
                if os.path.exists(os.path.join(watch_directory, filename)):
                    try:
                        master_df = load_data(os.path.join(watch_directory, filename))
                        master_label.config(text=f"Master: {payload}", fg="green")
                        execute_assessment()
                    except Exception:
                        pass
            gui_queue.task_done()
    except Empty:
        pass
    if system_running: root.after(50, listen_for_network_queue)


def plc_heartbeat_worker():
    global tx_heartbeat
    while system_running: tx_heartbeat = not tx_heartbeat; time.sleep(1.0)


def shutdown_application():
    global system_running;
    system_running = False
    with vbai_lock:
        if vbai_socket: vbai_socket.close()
    root.destroy()


# ============================ GUI Layout Tree Construction =======================================

root = tk.Tk()
root.title("GR1036 HUD Test Rig Panel Dashboard")
root.geometry("1180x700")

# Top Branding Header Frame
header_frame = tk.Frame(root, bg="white", padx=15, pady=8)
header_frame.pack(fill="x", side="top")

try:
    pil_left = Image.open("granroth_logo.png").resize((160, 55), Image.Resampling.LANCZOS)
    logo_left_image = ImageTk.PhotoImage(pil_left)
    logo_left_lbl = tk.Label(header_frame, image=logo_left_image, bg="white")
    logo_left_lbl.image = logo_left_image
    logo_left_lbl.pack(side="left", padx=5)
except Exception:
    tk.Label(header_frame, text="[ GRANROTH LOGO ]", font=("Arial", 10, "bold"), fg="#6c757d", bg="#e9ecef", padx=8,
             pady=15).pack(side="left", padx=5)

try:
    pil_right = Image.open("shatterprufe_logo.png").resize((160, 55), Image.Resampling.LANCZOS)
    logo_right_image = ImageTk.PhotoImage(pil_right)
    logo_right_lbl = tk.Label(header_frame, image=logo_right_image, bg="white")
    logo_right_lbl.image = logo_right_image
    logo_right_lbl.pack(side="right", padx=5)
except Exception:
    tk.Label(header_frame, text="[ CUSTOMER LOGO ]", font=("Arial", 10, "bold"), fg="#6c757d", bg="#e9ecef", padx=8,
             pady=15).pack(side="right", padx=5)

tk.Label(header_frame, text="GR1036 HUD TEST RIG - IMAGE ASSESSMENT", font=("Segoe UI", 12, "bold"), fg="#1e293b",
         bg="white").pack(expand=True, pady=12)
tk.Frame(root, height=2, bg="#cbd5e1").pack(fill="x", side="top", pady=(0, 5))

# --- Persistent Status Summary & Operation Deployment Bar ---
summary_frame = tk.Frame(root, padx=15, pady=6, bg="#f8f9fa", borderwidth=1, relief="groove")
summary_frame.pack(fill="x", padx=15, pady=5)

master_label = tk.Label(summary_frame, text="Master File Empty", fg="red", font=("Arial", 9, "bold"), bg="#f8f9fa",
                        width=22, anchor="w")
master_label.pack(side="left", padx=5)

test_label = tk.Label(summary_frame, text="Test Files Empty", fg="red", font=("Arial", 9, "bold"), bg="#f8f9fa",
                      width=40, anchor="w")
test_label.pack(side="left", padx=5)

dir_lbl = tk.Label(summary_frame, text=f"Watching: {watch_directory}", fg="#0d6efd", font=("Segoe UI", 9), bg="#f8f9fa",
                   anchor="w")
dir_lbl.pack(side="left", fill="x", expand=True, padx=10)

settings_btn = tk.Button(summary_frame, text="Settings ⚙", command=open_settings_window, font=("Arial", 10, "bold"),
                         bg="#0d6efd", fg="white", padx=15, pady=2)
settings_btn.pack(side="right", padx=5)

# Primary "Save Assessment" Button
save_btn = tk.Button(summary_frame, text="Save Assessment 💾", command=save_assessment_report,
                     font=("Arial", 10, "bold"), bg="#198754", fg="white", padx=15, pady=2)
save_btn.pack(side="right", padx=5)

# Array Macro Monitoring grid
global_frame = tk.LabelFrame(root, text=" Global Position Status Overview (Click to position to see parameters) ", padx=10, pady=10)
global_frame.pack(fill="x", padx=15, pady=5)

lh_overview_buttons, rh_overview_buttons = {}, {}

# Row 0: LHS Array with side-by-side position labels
tk.Label(global_frame, text="LHS Position Slots: ", font=("Arial", 9, "bold")).grid(row=0, column=0, padx=5, pady=5,
                                                                                    sticky="w")
for i in range(1, 6):
    slot_frame = tk.Frame(global_frame)
    slot_frame.grid(row=0, column=i, padx=8, pady=5)

    lbl = tk.Label(slot_frame, text=f"Pos {i}: ", font=("Arial", 9, "bold"))
    lbl.pack(side="left", padx=2)

    btn = tk.Button(slot_frame, text="IDLE", bg="lightgray", width=8,
                    command=lambda pos=i: select_and_view_position("LHS", pos))
    btn.pack(side="left")
    lh_overview_buttons[i] = btn

# Row 1: RHS Array with side-by-side position labels
tk.Label(global_frame, text="RHS Position Slots: ", font=("Arial", 9, "bold")).grid(row=1, column=0, padx=5, pady=5,
                                                                                    sticky="w")
for i in range(1, 6):
    slot_frame = tk.Frame(global_frame)
    slot_frame.grid(row=1, column=i, padx=8, pady=5)

    lbl = tk.Label(slot_frame, text=f"Pos {i}: ", font=("Arial", 9, "bold"))
    lbl.pack(side="left", padx=2)

    btn = tk.Button(slot_frame, text="IDLE", bg="lightgray", width=8,
                    command=lambda pos=i: select_and_view_position("RHS", pos))
    btn.pack(side="left")
    rh_overview_buttons[i] = btn

# Calibration Parameter Verification Grid
matrix_frame = tk.LabelFrame(root, text=" Position Parameters ", padx=10, pady=10)
matrix_frame.pack(fill="x", padx=15, pady=5)

# Persistent text view tracking context
current_view_label = tk.Label(matrix_frame, text="Viewing: LHS - Position 1", font=("Arial", 10, "bold"), fg="#0d6efd")
current_view_label.grid(row=0, column=0, columnspan=6, sticky="w", pady=5)

headers = ["Evaluation Metric", "Master Baseline", "Test Target", "Tolerance Value", "Calculated Variance",
           "Status Indicator"]
for col_idx, text_header in enumerate(headers):
    tk.Label(matrix_frame, text=text_header, font=("Arial", 9, "bold"), borderwidth=1, relief="solid",
             bg="#f8f9fa").grid(row=1, column=col_idx, sticky="nsew")

metrics_list = [('size', 'Image Size'), ('rotation', 'Image Rotation'), ('trap_h', 'Trapezoidal Dist. H'),
                ('trap_v', 'Trapezoidal Dist. V'), ('ar', 'Aspect Ratio'), ('translation', 'Translation'),
                ('smile', 'Smile Distortion'), ('ghosting', 'Ghosting Distance')]
ui_rows, tol_inputs = {}, {}

for row_idx, (key, label_text) in enumerate(metrics_list, start=2):
    tk.Label(matrix_frame, text=label_text, anchor="w", borderwidth=1, relief="groove").grid(row=row_idx, column=0,
                                                                                             sticky="nsew")
    m_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14)
    m_val.grid(row=row_idx, column=1, sticky="nsew")
    t_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14)
    t_val.grid(row=row_idx, column=2, sticky="nsew")
    tol_ent = tk.Entry(matrix_frame, justify="center", width=12)
    tol_ent.grid(row=row_idx, column=3, padx=10, pady=5);
    tol_inputs[key] = tol_ent
    v_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=15)
    v_val.grid(row=row_idx, column=4, sticky="nsew")
    s_box = tk.Label(matrix_frame, text=" IDLE ", bg="lightgray", font=("Arial", 9, "bold"), borderwidth=1,
                     relief="sunken", width=10)
    s_box.grid(row=row_idx, column=5, padx=15, pady=5)
    ui_rows[key] = {'master': m_val, 'test': t_val, 'variance': v_val, 'status': s_box}

for c in range(6): matrix_frame.grid_columnconfigure(c, weight=1)

# Persistent Tracking Indicators Footer
status_bar_frame = tk.Frame(root, padx=15, pady=10)
status_bar_frame.pack(fill="x", side="bottom")

plc_status_lbl = tk.Label(status_bar_frame, text="PLC DISCONNECTED", bg="red", fg="white", font=("Arial", 9, "bold"),
                          width=22, borderwidth=1, relief="solid")
plc_status_lbl.pack(side="left", padx=5)

vbai_status_lbl = tk.Label(status_bar_frame, text="VBAI DISCONNECTED", bg="red", fg="white", font=("Arial", 9, "bold"),
                           width=22, borderwidth=1, relief="solid")
vbai_status_lbl.pack(side="left", padx=5)

# High-visibility Global PASS / FAIL status display block
overall_status_lbl = tk.Label(status_bar_frame, text="SYSTEM IDLE", bg="lightgray", fg="black",
                              font=("Arial", 20, "bold"), width=24, borderwidth=1, relief="solid")
overall_status_lbl.pack(side="right", padx=5)

# Initialize input parameters
defaults = {'size': '2.0', 'rotation': '2.0', 'trap_h': '1.0', 'trap_v': '1.0', 'ar': '0.05', 'translation': '5.0',
            'smile': '1.0', 'ghosting': '1.0'}
for k, e in tol_inputs.items():
    if k in defaults: e.insert(0, defaults[k])

# Active background thread components
threading.Thread(target=plc_network_broker_worker, daemon=True).start()
threading.Thread(target=vbai_dedicated_client_worker, daemon=True).start()
threading.Thread(target=plc_heartbeat_worker, daemon=True).start()

root.after(100, listen_for_network_queue)
root.protocol("WM_DELETE_WINDOW", shutdown_application)
root.mainloop()