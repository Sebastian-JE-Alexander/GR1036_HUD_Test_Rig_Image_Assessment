"""
GR1036 HUD Test Rig
Image Assessment GUI & PLC / NI Vision Builder Broker

1)Image Size
2) Image Rotation
3) Trapezoidal Distortion
4) Aspect Ratio
5) Translation
6) Smile Distortion
7) Ghosting Distance
"""
#====================================== Library Imports =============================
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
from tkinter.scrolledtext import ScrolledText
import sys

from scipy.stats import false_discovery_control

#===========================================================================================


#================== Global Variables =================================================

# Global variables to store our data states
g_master_df = None
g_watch_directory = "C:\\VBAI_Data_Exports"  # Default fallback path
g_MM_PER_PX = 25.4 / 96.0

# Dual-variant databases to hold dataframes for up to 5 robot positions each
g_lh_positions_db = {}
g_rh_positions_db = {}
g_lh_results_db = {}
g_rh_results_db = {}

# Thread-safe communication channel
g_gui_queue = Queue()



# Global Python to PLC communication variables
g_plc_tx_heartbeat = False
g_plc_tx_error = False
g_plc_tx_barcode_pass = False
g_plc_tx_barcode_fail = False
g_plc_tx_camera_pass = False
g_plc_tx_camera_fail = False
g_plc_tx_capture_complete = False
g_plc_tx_error_code = 0
g_plc_tx_position_echo = 0
g_plc_tx_recipe_echo = 0
g_plc_tx_barcode_string = ""
g_plc_tx_master_csv_string = ""

#global PLC to Python communication variables
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


#Global Python to VB communication variables
g_vb_tx_trigger_camera = False
g_vb_tx_lhs = False
g_vb_tx_rhs = False
g_vb_tx_lh_barcode = False
g_vb_tx_rh_barcode = False
g_vb_tx_position = ""

#Global VB to Python communication variables
g_vb_rx_camera_ready = False
g_vb_rx_trigger_complete = False
g_vb_rx_trigger_fail = False
g_vb_rx_position_echo = 0
g_vb_rx_scanned_barcode = 0


# Shared cross-thread safe sockets
g_connection_vb = None
vbai_lock = threading.Lock()
g_system_running = True
g_run_btn = None
g_manual_pos_entry = None  # Entry widget reference for the Manual VBAI Test Panel

# Network Configuration parameters
PLC_PORT = 9005
VBAI_IP = "127.0.0.1"
VBAI_PORT = 9006
plc_send_rate = 0.200
plc_receive_rate = 0.0


# Global references to keep logo image objects alive in memory
g_logo1_img = None
g_logo2_img = None

#other globals for thread_vb
g_vb_send_done = 0
g_vb_mode = 0
g_vb_lhs = 0
g_vb_rhs = 0
g_vb_lh_bc_trigger = 0
g_vb_rh_bc_trigger = 0
g_vb_position = int

# ================================================== File loading ======================================================

def load_data(file_path):
    """
    Reads and cleans CSV data targeting grid coordinate sets.
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

def change_watch_directory():
    global g_watch_directory
    selected_dir = filedialog.askdirectory(title="Select Vision Builder Output Directory")
    if selected_dir:
        g_watch_directory = os.path.normpath(selected_dir)
        dir_lbl.config(text=f"Watching: Dataset", fg="blue")

def select_master_file():
    global g_master_df
    file_path = filedialog.askopenfilename(title="Select Master CSV File", filetypes=[("CSV files", "*.csv")])
    if file_path:
        try:
            g_master_df = load_data(file_path)
            master_label.config(text="Master: Loaded", fg="green", font=("Arial", 9, "bold"))
        except Exception as e:
            g_master_df = None
            master_label.config(text="Master: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Master CSV:\n\n{str(e)}")
        check_run_conditions()

#============================================== Network Queues ======================================================

def auto_ingest_pipeline(mode="BOTH"):
    global g_lh_positions_db, g_rh_positions_db
    if not os.path.exists(g_watch_directory): return

    if mode == "LHS" or mode == "BOTH": g_lh_positions_db.clear()
    if mode == "RHS" or mode == "BOTH": g_rh_positions_db.clear()

    all_raw_files = []
    for entry in os.listdir(g_watch_directory):
        full_path = os.path.join(g_watch_directory, entry)
        if os.path.isfile(full_path) and entry.lower().endswith('.csv'):
            all_raw_files.append((full_path, entry.lower(), os.path.getmtime(full_path)))

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
                    g_lh_positions_db[detected_pos] = load_data(path)
                    loaded_lh += 1
                except Exception:
                    pass

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
                    g_rh_positions_db[detected_pos] = load_data(path)
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
    if g_master_df is not None: execute_assessment()


def status_network():
    global g_master_df
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
            elif event_type == "PLC_CAPTURE_RESULTS":
                if g_master_df is not None: execute_assessment()
            elif event_type == "PLC_MASTER_CSV":
                filename = payload if payload.lower().endswith('.csv') else payload + '.csv'
                if os.path.exists(os.path.join(g_watch_directory, filename)):
                    try:
                        g_master_df = load_data(os.path.join(g_watch_directory, filename))
                        master_label.config(text=f"Master: {payload}", fg="green");
                        execute_assessment()
                    except Exception:
                        pass
            g_gui_queue.task_done()
    except Empty:
        pass
    if g_system_running: root.after(50, status_network)


#=========================================== GUI data handling =================================================

def save_assessment_report():
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
        with open(file_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["GR1036 HUD Test Rig - Quality Assessment Report"])
            writer.writerow(["Timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            writer.writerow(["Scanned Barcode", g_plc_tx_barcode_string if g_plc_tx_barcode_string else "N/A"])
            writer.writerow([])
            writer.writerow(["Variant Side", "Position Number", "Evaluation Metric", "Master Baseline", "Test Target",
                             "Allowed Tolerance", "Calculated Variance", "Status Result"])
            for pos, metrics in sorted(g_lh_results_db.items()):
                for key, data in metrics.items():
                    label, master_txt, test_txt, variance_txt, status_txt = data
                    writer.writerow(
                        ["LHS", f"Position {pos}", label, master_txt, test_txt, tol_inputs[key].get(), variance_txt,
                         status_txt.strip()])
            for pos, metrics in sorted(g_rh_results_db.items()):
                for key, data in metrics.items():
                    label, master_txt, test_txt, variance_txt, status_txt = data
                    writer.writerow(
                        ["RHS", f"Position {pos}", label, master_txt, test_txt, tol_inputs[key].get(), variance_txt,
                         status_txt.strip()])
        messagebox.showinfo("Save Report", "Assessment report successfully archived.")
    except Exception as e:
        messagebox.showerror("Export Failure", str(e))

def shutdown_application():
    global g_system_running;
    g_system_running = False
    with vbai_lock:
        if g_connection_vb: g_connection_vb.close()
    root.destroy()



def load_tolerances_from_template():
    target_file = filedialog.askopenfilename(title="Open Tolerance Settings Template File",
                                             filetypes=[("Text Documents", "*.txt"), ("All Files", "*.*")])
    if not target_file: return
    key_mapping = {"image size": ["size"], "image rotation": ["rotation"], "trapezoidal dist. h": ["trap_h"],
                   "trapezoidal dist. v": ["trap_v"], "aspect ratio": ["ar"], "translation": ["translation"],
                   "smile distortion": ["smile"], "ghosting distance": ["ghosting"]}
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip() or line.startswith("#") or "=" not in line: continue
                raw_key, raw_val = line.split("=", 1)
                clean_key = raw_key.strip().lower()
                if clean_key in key_mapping:
                    for ui_tag in key_mapping[clean_key]:
                        if ui_tag in tol_inputs:
                            tol_inputs[ui_tag].delete(0, tk.END);
                            tol_inputs[ui_tag].insert(0, raw_val.strip())
        if g_master_df is not None: execute_assessment()
    except Exception as e:
        messagebox.showerror("Template Error", str(e))


def clear_all_data():
    global g_master_df, g_lh_positions_db, g_rh_positions_db, g_lh_results_db, g_rh_results_db
    if not messagebox.askyesno("Clear Dashboard", "Reset data arrays?"): return
    g_master_df = None
    g_lh_positions_db.clear();
    g_rh_positions_db.clear()
    g_lh_results_db.clear();
    g_rh_results_db.clear()
    master_label.config(text="Master File Empty", fg="red")
    test_label.config(text="Test Files Empty", fg="red")
    check_run_conditions()
    for key in ui_rows:
        ui_rows[key]['master'].config(text="-");
        ui_rows[key]['test'].config(text="-");
        ui_rows[key]['variance'].config(text="-")
        ui_rows[key]['status'].config(bg="lightgray", text=" IDLE ", fg="black")
    for i in range(1, 6):
        lh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")
        rh_overview_buttons[i].config(bg="lightgray", text="IDLE", fg="black")
    overall_status_lbl.config(text="SYSTEM IDLE", bg="lightgray", fg="black", font=("Arial", 10, "bold"))

#====================================== Calculations ================================================================
def check_run_conditions():
    global g_run_btn
    if g_run_btn and g_run_btn.winfo_exists():
        if g_master_df is not None and (len(g_lh_positions_db) > 0 or len(g_rh_positions_db) > 0):
            g_run_btn.config(state=tk.NORMAL, bg="#198754", fg="white")
        else:
            g_run_btn.config(state=tk.DISABLED, bg="#e0e0e0", fg="#a0a0a0")


def run_all_calculations(df):
    p = get_grid_points(df)
    w_size, h_size = df['x_prim'].max() - df['x_prim'].min(), df['y_prim'].max() - df['y_prim'].min()
    w_ar, h_ar = p['top_right']['x_prim'] - p['top_left']['x_prim'], p['bottom_left']['y_prim'] - p['top_left'][
        'y_prim']
    ar = w_ar / h_ar if h_ar != 0 else 0
    smile = p['top_mid']['y_prim'] - ((p['top_left']['y_prim'] + p['top_right']['y_prim']) / 2)
    try:
        tl, tr = df.loc[(df['x_prim'] + df['y_prim']).idxmin()], df.loc[(df['x_prim'] - df['y_prim']).idxmax()]
        rot = np.degrees(np.arctan2(tr['y_prim'] - tl['y_prim'], tr['x_prim'] - tl['x_prim']))
    except Exception:
        rot = 0.0
    try:
        tl, br = df.loc[(df['x_prim'] + df['y_prim']).idxmin()], df.loc[(df['x_prim'] + df['y_prim']).idxmax()]
        tr, bl = df.loc[(df['x_prim'] - df['y_prim']).idxmax()], df.loc[(df['x_prim'] - df['y_prim']).idxmin()]
        tw, bw, lh, rh = tr['x_prim'] - tl['x_prim'], br['x_prim'] - bl['x_prim'], bl['y_prim'] - tl['y_prim'], br[
            'y_prim'] - tr['y_prim']
        trap = ((abs(tw - bw) / min(tw, bw)) * 100, (abs(lh - rh) / min(lh, rh)) * 100)
    except Exception:
        trap = (0.0, 0.0)
    ghost = np.mean(
        np.sqrt((df['x_ghost'] - df['x_prim']) ** 2 + (df['y_ghost'] - df['y_prim']) ** 2)) if not df.empty else 0.0
    return {'image_size': (w_size, h_size), 'aspect_ratio': ar, 'smile': smile, 'rotation': rot,
            'translation': (p['center']['x_prim'], p['center']['y_prim']), 'trap_dist': trap, 'avg_ghosting': ghost}


def process_variant_database(source_db, results_db, m_res, overview_buttons):
    any_fail = False
    for i in range(1, 6):
        if i not in source_db: overview_buttons[i].config(bg="lightgray", text="EMPTY", fg="black")
    for pos_idx, t_df in source_db.items():
        failed_criteria_count = 0
        t_res = run_all_calculations(t_df)
        metrics = {}
        m_w_mm, m_h_mm = m_res['image_size'][0] * g_MM_PER_PX, m_res['image_size'][1] * g_MM_PER_PX
        t_w_mm, t_h_mm = t_res['image_size'][0] * g_MM_PER_PX, t_res['image_size'][1] * g_MM_PER_PX
        w_diff, h_diff = t_w_mm - m_w_mm, t_h_mm - m_h_mm
        max_size_diff = w_diff if abs(w_diff) >= abs(h_diff) else h_diff
        metrics['size'] = ("Image Size", f"{round(m_w_mm, 1)}x{round(m_h_mm, 1)} mm",
                           f"{round(t_w_mm, 1)}x{round(t_h_mm, 1)} mm", f"{round(max_size_diff, 3)} mm",
                           "PASS" if abs(max_size_diff) <= float(tol_inputs['size'].get()) else "FAIL")
        rot_diff = t_res['rotation'] - m_res['rotation']
        metrics['rotation'] = ("Image Rotation", f"{round(m_res['rotation'], 2)}°", f"{round(t_res['rotation'], 2)}°",
                               f"{round(rot_diff, 3)} °",
                               "PASS" if abs(rot_diff) <= float(tol_inputs['rotation'].get()) else "FAIL")
        m_trap_h, m_trap_v = m_res['trap_dist'];
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
        m_trans_x, m_trans_y = m_res['translation'];
        t_trans_x, t_trans_y = t_res['translation']
        trans_diff_mm = np.sqrt(((t_trans_x - m_trans_x) * g_MM_PER_PX) ** 2 + ((t_trans_y - m_trans_y) * g_MM_PER_PX) ** 2)
        metrics['translation'] = ("Translation",
                                  f"X: {round(m_trans_x * g_MM_PER_PX, 1)} mm, Y: {round(m_trans_y * g_MM_PER_PX, 1)} mm",
                                  f"X: {round(t_trans_x * g_MM_PER_PX, 1)} mm, Y: {round(t_trans_y * g_MM_PER_PX, 1)} mm",
                                  f"{round(trans_diff_mm, 3)} mm",
                                  "PASS" if trans_diff_mm <= float(tol_inputs['translation'].get()) else "FAIL")
        metrics['smile'] = ("Smile Distortion", f"{round(m_res['smile'] * g_MM_PER_PX, 2)} mm",
                            f"{round(t_res['smile'] * g_MM_PER_PX, 2)} mm",
                            f"{round(smile_diff_mm := (t_res['smile'] - m_res['smile']) * g_MM_PER_PX, 3)} mm",
                            "PASS" if abs(smile_diff_mm) <= float(tol_inputs['smile'].get()) else "FAIL")
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


def execute_assessment():
    global g_lh_results_db, g_rh_results_db, g_plc_tx_camera_pass, g_plc_tx_camera_fail, g_plc_tx_error_code, g_plc_tx_capture_complete
    if g_master_df is None: return
    m_res = run_all_calculations(g_master_df)
    lh_failed = process_variant_database(g_lh_positions_db, g_lh_results_db, m_res, lh_overview_buttons)
    rh_failed = process_variant_database(g_rh_positions_db, g_rh_results_db, m_res, rh_overview_buttons)
    g_plc_tx_capture_complete = True
    if lh_failed or rh_failed:
        g_plc_tx_camera_pass, g_plc_tx_camera_fail, g_plc_tx_error_code = False, True, 101
        overall_status_lbl.config(text="FAIL", bg="red", fg="white", font=("Arial", 14, "bold"))
    else:
        if len(g_lh_positions_db) == 0 and len(g_rh_positions_db) == 0:
            overall_status_lbl.config(text="SYSTEM IDLE", bg="lightgray", fg="black", font=("Arial", 10, "bold"))
            g_plc_tx_capture_complete = False
        else:
            g_plc_tx_camera_pass, g_plc_tx_camera_fail, g_plc_tx_error_code = True, False, 0
            overall_status_lbl.config(text="PASS", bg="green", fg="white", font=("Arial", 14, "bold"))
    refresh_displayed_position_metrics()

def get_grid_points(df):
    y_min, y_max = df['y_prim'].min(), df['y_prim'].max()
    total_height = y_max - y_min
    approx_row_spacing = total_height / 6
    df['row_group'] = ((df['y_prim'] - y_min) / approx_row_spacing).round()
    return {
        "top_left": df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True).iloc[0],
        "top_mid": df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True).iloc[5],
        "top_right": df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True).iloc[10],
        "center": df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True).iloc[38],
        "bottom_left": df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True).iloc[66],
        "bottom_mid": df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True).iloc[71],
        "bottom_right": df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True).iloc[76]
    }

#================================ GUI Buttons ======================================================================

def select_and_view_position(variant, position_idx):
    current_view_label.config(text=f"Viewing: {variant} - Position {position_idx}")
    refresh_displayed_position_metrics(variant, position_idx)


def refresh_displayed_position_metrics(forced_variant=None, forced_pos=None):
    selected_variant = forced_variant if forced_variant else getattr(current_view_label, 'target_variant', 'LHS')
    selected_pos = forced_pos if forced_pos else getattr(current_view_label, 'target_pos', 1)
    if forced_variant and forced_pos: current_view_label.target_variant, current_view_label.target_pos = forced_variant, forced_pos
    target_db = g_lh_results_db if selected_variant == "LHS" else g_rh_results_db
    if selected_pos not in target_db:
        for key in ui_rows:
            ui_rows[key]['master'].config(text="-");
            ui_rows[key]['test'].config(text="-");
            ui_rows[key]['variance'].config(text="-")
            ui_rows[key]['status'].config(bg="lightgray", text=" NO DATA ", fg="black")
        return
    metrics = target_db[selected_pos]
    for key in ui_rows:
        _, master_txt, test_txt, variance_txt, status_txt = metrics[key]
        ui_rows[key]['master'].config(text=master_txt);
        ui_rows[key]['test'].config(text=test_txt);
        ui_rows[key]['variance'].config(text=variance_txt)
        ui_rows[key]['status'].config(bg="green" if status_txt == "PASS" else "red", text=f" {status_txt} ", fg="white")


#================== Manual VBAI Test Panel (Engineering Use Only) ==================================================
# These helpers do NOT touch thread_vb() or the socket directly. They simply set the
# same g_plc_rx_* globals that thread_vb() already reads (the exact variables the PLC
# would normally populate) and clear g_vb_send_done. thread_vb()'s existing loop then
# sends the structure on its next pass using its own, unmodified send logic. This keeps
# the wire format exactly as specified by the PLC programmer.

def log_message(text):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log.config(state="normal")
    log.insert(tk.END, f"[{timestamp}] {text}\n")
    log.see(tk.END)
    log.config(state="disabled")


def manual_vb_send(camera_trigger, lhs_active, rhs_active, capture_barcode, lh_bc_req, rh_bc_req):
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
    g_vb_send_done = False  # signals thread_vb() to build and send the packet on its next pass

    log_message(f"[MANUAL TEST] Cam:{camera_trigger} BC:{capture_barcode} LHS:{lhs_active} "
                f"RHS:{rhs_active} LH_BC:{lh_bc_req} RH_BC:{rh_bc_req} Pos:{test_pos}")


def manual_vb_clear_flags():
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


def open_io_list_window():
    """
    Create a extra window in the settings menu for debugging purposes.
    allows us to see the states of the RX and TX tags so that we can see what data is actually being sent.
    """
    io_win = tk.Toplevel(root)
    io_win.title("Live IO List")
    io_win.geometry("500x700")

    def add_section(parent, title):
        tk.Label(parent, text=title, font=("Arial", 10, "bold"), fg="#0c447c").pack(anchor="w", pady=(10, 2), padx=8)
        sec = tk.Frame(parent)
        sec.pack(fill="x", padx=8)
        return sec

    def add_row(parent, label_text):
        row = tk.Frame(parent)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label_text, font=("Arial", 9), anchor="w", width=26).pack(side="left")
        val_lbl = tk.Label(row, text="-", font=("Arial", 9, "bold"), anchor="w", width=14, bg="#e9ecef")
        val_lbl.pack(side="left")
        return val_lbl

    canvas_frame = tk.Frame(io_win)
    canvas_frame.pack(fill="both", expand=True)
#============================ PLC to Python List (RX) =====================================================
    plc_rx_sec = add_section(canvas_frame, "PLC -> Python (RX)")
    rows = {}
    rows['plc_rx_heartbeat'] = (add_row(plc_rx_sec, "Heartbeat"), lambda: g_plc_rx_heartbeat)
    rows['plc_rx_error'] = (add_row(plc_rx_sec, "Error"), lambda: g_plc_rx_error)
    rows['plc_rx_capture_barcode'] = (add_row(plc_rx_sec, "Capture Barcode"), lambda: g_plc_rx_capture_barcode)
    rows['plc_rx_trigger_camera'] = (add_row(plc_rx_sec, "Trigger Camera"), lambda: g_plc_rx_trigger_camera)
    rows['plc_rx_lhs_active'] = (add_row(plc_rx_sec, "Sequence LHS Active"), lambda: g_plc_rx_lhs_sequence_active)
    rows['plc_rx_rhs_active'] = (add_row(plc_rx_sec, "Sequence RHS Active"), lambda: g_plc_rx_rhs_sequence_active)
    rows['plc_rx_capture_results'] = (add_row(plc_rx_sec, "Capture Results"), lambda: g_plc_rx_capture_results)
    rows['plc_rx_lh_barcode_req'] = (add_row(plc_rx_sec, "Barcode Required LH"), lambda: g_plc_rx_lh_barcode_req)
    rows['plc_rx_rh_barcode_req'] = (add_row(plc_rx_sec, "Barcode Required RH"), lambda: g_plc_rx_rh_barcode_req)
    rows['plc_rx_position'] = (add_row(plc_rx_sec, "Position"), lambda: g_plc_rx_position)

#============================== Python to Vision Builder List (TX) =================================

    vb_tx_sec = add_section(canvas_frame, "Python -> Vision Builder (TX)")
    rows['vb_tx_trigger_camera'] = (add_row(vb_tx_sec, "Trigger Camera"), lambda: g_vb_tx_trigger_camera)
    rows['vb_tx_lhs'] = (add_row(vb_tx_sec, "LHS Active"), lambda: g_vb_tx_lhs)
    rows['vb_tx_rhs'] = (add_row(vb_tx_sec, "RHS Active"), lambda: g_vb_tx_rhs)
    rows['vb_tx_lh_barcode'] = (add_row(vb_tx_sec, "LH Barcode Trigger"), lambda: g_vb_tx_lh_barcode)
    rows['vb_tx_rh_barcode'] = (add_row(vb_tx_sec, "RH Barcode Trigger"), lambda: g_vb_tx_rh_barcode)
    rows['vb_tx_position'] = (add_row(vb_tx_sec, "Position"), lambda: g_vb_tx_position)

#============================== VB Send to Python List (RX) ===============================================

    vb_rx_sec = add_section(canvas_frame, "Vision Builder -> Python (RX)")
    rows['vb_rx_camera_ready'] = (add_row(vb_rx_sec, "Camera Ready"), lambda: g_vb_rx_camera_ready)
    rows['vb_rx_trigger_complete'] = (add_row(vb_rx_sec, "Trigger Complete"), lambda: g_vb_rx_trigger_complete)
    rows['vb_rx_trigger_fail'] = (add_row(vb_rx_sec, "Trigger Fail"), lambda: g_vb_rx_trigger_fail)
    rows['vb_rx_position_echo'] = (add_row(vb_rx_sec, "Position Echo"), lambda: g_vb_rx_position_echo)
    rows['vb_rx_scanned_barcode'] = (add_row(vb_rx_sec, "Scanned Barcode"), lambda: g_vb_rx_scanned_barcode)

#=============================================== Python PLC send list =============================================

    plc_tx_sec = add_section(canvas_frame, "Python -> PLC (TX)")
    rows['plc_tx_heartbeat'] = (add_row(plc_tx_sec, "Heartbeat"), lambda: g_plc_tx_heartbeat)
    rows['plc_tx_error'] = (add_row(plc_tx_sec, "Error"), lambda: g_plc_tx_error)
    rows['plc_tx_barcode_pass'] = (add_row(plc_tx_sec, "Barcode Pass"), lambda: g_plc_tx_barcode_pass)
    rows['plc_tx_barcode_fail'] = (add_row(plc_tx_sec, "Barcode Fail"), lambda: g_plc_tx_barcode_fail)
    rows['plc_tx_camera_pass'] = (add_row(plc_tx_sec, "Camera Pass"), lambda: g_plc_tx_camera_pass)
    rows['plc_tx_camera_fail'] = (add_row(plc_tx_sec, "Camera Fail"), lambda: g_plc_tx_camera_fail)
    rows['plc_tx_capture_complete'] = (add_row(plc_tx_sec, "Capture Complete"), lambda: g_plc_tx_capture_complete)
    rows['plc_tx_barcode_string'] = (add_row(plc_tx_sec, "Barcode String"), lambda: g_plc_tx_barcode_string)
    rows['plc_tx_position_echo'] = (add_row(plc_tx_sec, "Position Echo"), lambda: g_plc_tx_position_echo)

    def refresh():
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


def open_settings_window():
    global g_run_btn, g_manual_pos_entry
    settings_win = tk.Toplevel(root);
    settings_win.title("System Settings Panel");
    settings_win.geometry("480x680");
    settings_win.resizable(False, False);
    settings_win.grab_set()
    tk.Label(settings_win, text="System Configuration Controls", font=("Segoe UI", 12, "bold"), pady=10).pack()
    config_lf = tk.LabelFrame(settings_win, text=" Core Management ", padx=10, pady=8);
    config_lf.pack(fill="x", padx=15, pady=5)
    tk.Button(config_lf, text="Change Watch Directory", command=change_watch_directory, width=26, bg="#e2e8f0").grid(
        row=0, column=0, padx=5, pady=3)
    tk.Button(config_lf, text="Load Tolerance Template", command=load_tolerances_from_template, width=26,
              bg="#cbd5e1").grid(row=0, column=1, padx=5, pady=3)
    tk.Button(config_lf, text="Upload Master CSV Manually", command=select_master_file, width=26, bg="#d1e7dd").grid(
        row=1, column=0, padx=5, pady=3)
    g_run_btn = tk.Button(config_lf, text="Assess Data Manually", command=execute_assessment, state=tk.DISABLED,
                          bg="#198754", fg="white", width=26);
    g_run_btn.grid(row=1, column=1, padx=5, pady=3)
    sync_lf = tk.LabelFrame(settings_win, text=" Target Polling Overrides ", padx=10, pady=8);
    sync_lf.pack(fill="x", padx=15, pady=5)
    tk.Button(sync_lf, text="Sync LHS Only (5 Files)", command=lambda: auto_ingest_pipeline("LHS"), width=25,
              bg="#0dcaf0").grid(row=0, column=0, padx=5, pady=4)
    tk.Button(sync_lf, text="Sync RHS Only (5 Files)", command=lambda: auto_ingest_pipeline("RHS"), width=25,
              bg="#ffc107").grid(row=0, column=1, padx=5, pady=4)
    tk.Button(sync_lf, text="Synchronize Full Macro Dataset (10 Files)", command=lambda: auto_ingest_pipeline("BOTH"),
              width=54, bg="#212529", fg="white").grid(row=1, column=0, columnspan=2, padx=5, pady=4)
    maint_lf = tk.LabelFrame(settings_win, text=" Storage Maintenance ", padx=10, pady=8);
    maint_lf.pack(fill="x", padx=15, pady=5)
    tk.Button(maint_lf, text="Clear Dashboard Runtime Logs & Arrays", command=clear_all_data, width=54, bg="#f8d7da",
              fg="#842029").pack(pady=2)

    tk.Button(settings_win, text="Open Live IO List", command=open_io_list_window, width=22, bg="#0c447c",
              fg="white").pack(pady=(8, 2)) #newly added button in settings for debugging purposes

    vb_test_lf = tk.LabelFrame(settings_win, text=" Manual VBAI Test Panel (Engineering Use Only) ", padx=10, pady=8,
                               fg="#842029");
    vb_test_lf.pack(fill="x", padx=15, pady=5)
    tk.Label(vb_test_lf, text="Sends the same structure thread_vb() already sends, via the PLC RX flags it reads. "
                              "Use only with no PLC connected.", font=("Arial", 8), fg="#6c757d", wraplength=420,
             justify="left").pack(anchor="w", pady=(0, 6))

    pos_row = tk.Frame(vb_test_lf);
    pos_row.pack(fill="x", pady=(0, 6))
    tk.Label(pos_row, text="Test Position:", font=("Arial", 9, "bold")).pack(side="left")
    g_manual_pos_entry = tk.Entry(pos_row, width=6, justify="center");
    g_manual_pos_entry.pack(side="left", padx=8)
    g_manual_pos_entry.insert(0, "1")

    cam_row = tk.Frame(vb_test_lf);
    cam_row.pack(fill="x", pady=2)
    tk.Button(cam_row, text="Trigger Camera - LHS",
              command=lambda: manual_vb_send(True, True, False, False, False, False), width=22,
              bg="#0dcaf0").pack(side="left", padx=3)
    tk.Button(cam_row, text="Trigger Camera - RHS",
              command=lambda: manual_vb_send(True, False, True, False, False, False), width=22,
              bg="#ffc107").pack(side="left", padx=3)
    tk.Button(cam_row, text="Trigger Camera - BOTH",
              command=lambda: manual_vb_send(True, True, True, False, False, False), width=22,
              bg="#212529", fg="white").pack(side="left", padx=3)

    bc_row = tk.Frame(vb_test_lf);
    bc_row.pack(fill="x", pady=2)
    tk.Button(bc_row, text="Request LH Barcode",
              command=lambda: manual_vb_send(False, False, False, True, True, False), width=22,
              bg="#0dcaf0").pack(side="left", padx=3)
    tk.Button(bc_row, text="Request RH Barcode",
              command=lambda: manual_vb_send(False, False, False, True, False, True), width=22,
              bg="#ffc107").pack(side="left", padx=3)
    tk.Button(bc_row, text="Request Both Barcodes",
              command=lambda: manual_vb_send(False, False, False, True, True, True), width=22,
              bg="#212529", fg="white").pack(side="left", padx=3)

    clear_row = tk.Frame(vb_test_lf);
    clear_row.pack(fill="x", pady=(6, 0))
    tk.Button(clear_row, text="Clear Manual RX Flags", command=manual_vb_clear_flags, width=70, bg="#f8d7da",
              fg="#842029").pack()

    tk.Button(settings_win, text="Exit Settings Menu", command=settings_win.destroy, width=18, bg="#6c757d",
              fg="white").pack(pady=12)
    check_run_conditions()


# ==================================== Vision Builder Thread ==================================================================

def thread_vb():
    """
    Establishes connection to Vision builder for sending commands and receiving status information on tcp
    builds a data structure to send and decodes the same structure when vision builder.  
    """
    #Python to PLC
    global g_connection_vb
    global g_plc_tx_barcode_string
    global g_plc_tx_barcode_pass
    global g_plc_tx_barcode_fail
    global g_plc_tx_camera_pass
    global g_plc_tx_camera_fail
    global g_plc_tx_error_code

    #PLC to Python
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

    #Python to VB
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
    global g_vb_rx_position_echo

    #others
    global g_vb_send_done
    global g_vb_mode
    global g_vb_lhs
    global g_vb_rhs
    global g_vb_lh_bc_trigger
    global g_vb_rh_bc_trigger
    global g_vb_position
    global g_vb_rx_scanned_barcode




    while g_system_running:

        #Trying to establish connection with Vision builder
        if g_connection_vb is None:
            try:
                l_connection_vb = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                l_connection_vb.settimeout(3.0)
                l_connection_vb.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                l_connection_vb.connect((VBAI_IP, VBAI_PORT))

                with vbai_lock:
                    g_connection_vb = l_connection_vb
                g_gui_queue.put(("VBAI_CONNECTION", "CONNECTED"))
            except Exception:
                with vbai_lock:
                    g_connection_vb = None
                g_gui_queue.put(("VBAI_CONNECTION", "DISCONNECTED"))
                time.sleep(2.0)
                continue

        #If connection successful, begin send/recieve
        try:
            #============================= Send ============================================================
            if (g_plc_rx_trigger_camera is True) or (g_plc_rx_capture_barcode is True) and (not g_vb_send_done is True):

                if g_plc_rx_trigger_camera is True: #checks the mode from the plc rx to see if camera trigger needed
                    l_vb_camera_trigger = 1
                else:
                    l_vb_camera_trigger = 0

                if g_plc_rx_lhs_sequence_active is True: #checks the plc rx to see if LHS is requested
                    l_vb_lhs_active = 1
                else:
                    l_vb_lhs_active = 0

                if g_plc_rx_rhs_sequence_active is True: #checks the plc rx to see if RHS is requested
                    l_vb_rhs_active = 1
                else:
                    l_vb_rhs_active = 0

                if g_plc_rx_lh_barcode_req is True: #checks the plc rx to see if LHS barcode requested
                    l_vb_lh_bc_trigger = 1
                else:
                    l_vb_lh_bc_trigger = 0

                if g_plc_rx_rh_barcode_req is True: #checks the plc rx to see if RHS barcode requested
                    l_vb_rh_bc_trigger = 1
                else:
                    l_vb_rh_bc_trigger = 0

                l_vb_position = g_plc_rx_position #passes the position integer from the plc rx to our local variable

                # Mirror into globals purely for live display on the IO list - does not affect what gets sent
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

                outbound_packet = struct.pack("<BBH", tx_b0, 0, int(l_vb_position)) # swapped ! to < for BE to LE
                g_connection_vb.sendall(outbound_packet)
                g_vb_send_done = True



#======================================= Receive ==============================================

            inbound_raw = g_connection_vb.recv(54) #get 54 bytes from VB
            if not inbound_raw or len(inbound_raw) < 54: #check length to ensure we got everything
                raise socket.error("Connection closed by Vision Builder remote endpoint.") #if not raise error

            rx_byte0, rx_byte1, rx_pos_echo, rx_scanned_barcode = struct.unpack("!BBH50s", inbound_raw[:54])  #decode 54 bytes from VB
            g_vb_rx_camera_ready = bool(rx_byte0 & (1 << 0))
            g_vb_rx_trigger_complete = bool(rx_byte0 & (1 << 1))
            g_vb_rx_trigger_fail = bool(rx_byte0 & (1 << 2))

            g_vb_rx_position_echo = int (rx_pos_echo)
            g_vb_rx_scanned_barcode = rx_scanned_barcode.decode('utf-8', errors='ignore').strip('\x00\r\n')
            g_plc_tx_barcode_string = g_vb_rx_scanned_barcode  # we are the source of this value - forward VB's scan result on to the PLC TX packet

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
    #Python to PLC
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

    #PLC to Python
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

#timer setup for send rate for PLC
    timer_prev = datetime.now()



    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(('0.0.0.0', PLC_PORT));  #connect to the PLC TCP Port, and bind to special socket
        server_socket.listen(1) #
    except Exception:
        return
#trying to establish connection to PLC
    while g_system_running:
        client_socket = None
        try:
            client_socket, _ = server_socket.accept()
            g_gui_queue.put(("PLC_CONNECTION", "CONNECTED"))
            session_active = True
            timer_prev = datetime.now()  # reset cyclic-send timer for this connection

#================================== Send ===========================================
            def plc_cyclic_sender(sock):
                nonlocal session_active, timer_prev
                while g_system_running and session_active:
                    try:
                        timer_current = datetime.now()
                        if (timer_current - timer_prev).total_seconds() > plc_send_rate:
                            timer_prev = timer_current

                            #Populate the structure of send
                            tx_byte0 = 0
                            tx_byte0 |= g_plc_tx_heartbeat <<0
                            tx_byte0 |= g_plc_tx_error << 1
                            tx_byte0 |= g_plc_tx_barcode_pass << 2
                            tx_byte0 |= g_plc_tx_barcode_fail << 3
                            tx_byte0 |= g_plc_tx_camera_pass << 4
                            tx_byte0 |= g_plc_tx_camera_fail << 5
                            tx_byte0 |= g_plc_tx_capture_complete << 6


                            encoded_bc = g_plc_tx_barcode_string.encode('utf-8')[:50].ljust(50, b'\x00')
                            encoded_mcsv = g_plc_tx_master_csv_string.encode('utf-8')[:20].ljust(20, b'\x00')
                            packet = struct.pack("!BBBBHH50s20sH", tx_byte0, 0, 0, 0, g_plc_tx_error_code, g_plc_tx_position_echo,
                                                 encoded_bc, encoded_mcsv, g_plc_tx_recipe_echo)
                            sock.sendall(packet)
                    except Exception:
                        session_active = False; break
                    time.sleep(0.200)

            threading.Thread(target=plc_cyclic_sender, args=(client_socket,), daemon=True).start()

#======================================== Receive =====================================================
            while g_system_running and session_active:
                try:
                    data = client_socket.recv(80)  #receive 80 Bytes from PLC
                    if not data or len(data) < 80: break  #check that we are getting the correct amount of bytes

                    byte0, _, byte2, _, _, robot_pos, _, master_csv_bytes, recipe_selection = struct.unpack(
                        "!BBBBHH50s20sH", data[:80]) #unpacks and decodes the sent structure from PLC
                    g_plc_rx_capture_barcode = bool(byte0 & (1 << 2))
                    g_plc_rx_trigger_camera = bool(byte0 & (1 << 3))
                    g_plc_rx_lhs_sequence_active = bool(byte0 & (1 << 4))
                    g_plc_rx_rhs_sequence_active = bool(byte0 & (1 << 5))
                    g_plc_rx_capture_results = bool(byte0 & (1 << 6))

                    g_plc_rx_lh_barcode_req = bool(byte2 & (1 << 0))
                    g_plc_rx_rh_barcode_req = bool(byte2 & (1 << 1))

                    g_plc_rx_position = robot_pos
                    g_plc_rx_master_csv = master_csv_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n')
                    g_plc_tx_recipe_echo = recipe_selection

                    plc_master_csv = master_csv_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n ')
                    if plc_master_csv and not bool(byte0 & (1 << 2)):
                        g_plc_tx_master_csv_string = plc_master_csv
                        g_gui_queue.put(("PLC_MASTER_CSV", plc_master_csv))

                    if bool(byte0 & (1 << 6)): g_gui_queue.put(("PLC_CAPTURE_RESULTS", ""))

                    # --- FORWARD INSTRUCTION STATE TO VISION BUILDER LOOP ENGINE ---
                    # NOTE: commented out - plc_is_lhs_variant, plc_is_rhs_variant, plc_req_lh_barcode,
                    # and plc_req_rh_barcode are not defined anywhere in this file, and thread_vb() does
                    # not read .pending_trigger anywhere (it drives off the g_plc_rx_* globals directly).
                    # Left here, commented, in case this was intended to be wired up to something -
                    # raises NameError if uncommented as-is.
                    # is_camera_trigger = bool(byte0 & (1 << 3))
                    # is_barcode_trigger = bool(byte0 & (1 << 2))
                    #
                    # if is_barcode_trigger:
                    #     thread_vb.pending_trigger = ("BARCODE", plc_is_lhs_variant,
                    #                                  plc_is_rhs_variant, plc_req_lh_barcode,
                    #                                  plc_req_rh_barcode, robot_pos)
                    # elif is_camera_trigger:
                    #     thread_vb.pending_trigger = ("CAMERA", plc_is_lhs_variant,
                    #                                  plc_is_rhs_variant, plc_req_lh_barcode,
                    #                                  plc_req_rh_barcode, robot_pos)

                except Exception:
                    break
        except Exception:
            pass
        finally:
            session_active = False
            if client_socket: client_socket.close()
            g_gui_queue.put(("PLC_CONNECTION", "DISCONNECTED"))
            time.sleep(1.0)

#========================== Heartbeat Thread (No change needed here) =================================
def thread_heartbeat():
    global g_plc_tx_heartbeat
    while g_system_running:
        g_plc_tx_heartbeat = not g_plc_tx_heartbeat; time.sleep(1.0)


# ============================ GUI Layout Construction =======================================

root = tk.Tk();
root.title("GR1036 HUD Test Rig Dashboard");
root.geometry("1180x700")

# --- BRANDING HEADER PANEL ---
header_frame = tk.Frame(root, bg="white", padx=15, pady=8);
header_frame.pack(fill="x", side="top")

# Configure a 3-column layout grid to handle Left Logo, Centered Title, Right Logo neatly
header_frame.columnconfigure(0, weight=1)
header_frame.columnconfigure(1, weight=2)
header_frame.columnconfigure(2, weight=1)

# Far-Left Logo Block (Granroth Logo)
left_logo_frame = tk.Frame(header_frame, bg="white")
left_logo_frame.grid(row=0, column=0, sticky="w", padx=15)

try:
    logo1_path = os.path.join(os.path.dirname(__file__), "granroth_logo.png")
    if os.path.exists(logo1_path):
        logo1_pil = Image.open(logo1_path).resize((150, 60), Image.Resampling.LANCZOS)
        g_logo1_img = ImageTk.PhotoImage(logo1_pil)
        tk.Label(left_logo_frame, image=g_logo1_img, bg="white").pack()
    else:
        tk.Label(left_logo_frame, text="[ GRANROTH Logo  ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10,
                 pady=5, borderwidth=1, relief="groove").pack()
except Exception:
    tk.Label(left_logo_frame, text="[ Logo 1 ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10,
             pady=5, borderwidth=1, relief="groove").pack()


# Centered System Title Card
tk.Label(header_frame, text="GR1036 HUD TEST RIG - Image Assessment", font=("Segoe UI", 14, "bold"), fg="#1e293b", bg="white").grid(row=0, column=1)

# Far-Right Logo Block (Customer Logo)
right_logo_frame = tk.Frame(header_frame, bg="white")
right_logo_frame.grid(row=0, column=2, sticky="e", padx=15)

try:
    logo2_path = os.path.join(os.path.dirname(__file__), "shatterprufe_logo.png")
    if os.path.exists(logo2_path):
        logo2_pil = Image.open(logo2_path).resize((180, 80), Image.Resampling.LANCZOS)
        g_logo2_img = ImageTk.PhotoImage(logo2_pil)
        tk.Label(right_logo_frame, image=g_logo2_img, bg="white").pack()
    else:
        tk.Label(right_logo_frame, text="[ CUSTOMER Logo  ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10,
                 pady=5, borderwidth=1, relief="groove").pack()
except Exception:
    tk.Label(right_logo_frame, text="[ Logo 2 ]", font=("Arial", 11, "bold"), fg="#475569", bg="#f1f5f9", padx=10,
             pady=5, borderwidth=1, relief="groove").pack()

tk.Frame(root, height=2, bg="#cbd5e1").pack(fill="x", side="top", pady=(0, 5))

# --- SUMMARY DATA METRICS BAR PANEL ---
summary_frame = tk.Frame(root, padx=15, pady=6, bg="#f8f9fa", borderwidth=1, relief="groove");
summary_frame.pack(fill="x", padx=15, pady=5)
master_label = tk.Label(summary_frame, text="Master File Empty", fg="red", font=("Arial", 9, "bold"), bg="#f8f9fa",
                        width=22, anchor="w");
master_label.pack(side="left", padx=5)
test_label = tk.Label(summary_frame, text="Test Files Empty", fg="red", font=("Arial", 9, "bold"), bg="#f8f9fa",
                      width=40, anchor="w");
test_label.pack(side="left", padx=5)
dir_lbl = tk.Label(summary_frame, text=f"Watching: {g_watch_directory}", fg="#0d6efd", font=("Segoe UI", 9), bg="#f8f9fa",
                   anchor="w");
dir_lbl.pack(side="left", fill="x", expand=True, padx=10)
settings_btn = tk.Button(summary_frame, text="Settings ⚙", command=open_settings_window, font=("Arial", 10, "bold"),
                         bg="#0d6efd", fg="white", padx=15, pady=2);
settings_btn.pack(side="right", padx=5)
save_btn = tk.Button(summary_frame, text="Save Assessment 💾", command=save_assessment_report,
                     font=("Arial", 10, "bold"), bg="#198754", fg="white", padx=15, pady=2);
save_btn.pack(side="right", padx=5)

# --- TRACKING SLOT SELECTION OVERVIEW GRID ---
global_frame = tk.LabelFrame(root, text=" Global Position Status Overview (Click a position to see its parameters) ", padx=10, pady=10);
global_frame.pack(fill="x", padx=15, pady=5)
lh_overview_buttons, rh_overview_buttons = {}, {}
tk.Label(global_frame, text="LHS Position Slots: ", font=("Arial", 9, "bold")).grid(row=0, column=0, padx=5, pady=5,
                                                                                    sticky="w")
for i in range(1, 6):
    sf = tk.Frame(global_frame);
    sf.grid(row=0, column=i, padx=8, pady=5)
    tk.Label(sf, text=f"Pos {i}: ", font=("Arial", 9, "bold")).pack(side="left")
    btn = tk.Button(sf, text="IDLE", bg="lightgray", width=8,
                    command=lambda pos=i: select_and_view_position("LHS", pos));
    btn.pack(side="left")
    lh_overview_buttons[i] = btn
tk.Label(global_frame, text="RHS Position Slots: ", font=("Arial", 9, "bold")).grid(row=1, column=0, padx=5, pady=5,
                                                                                    sticky="w")
for i in range(1, 6):
    sf = tk.Frame(global_frame);
    sf.grid(row=1, column=i, padx=8, pady=5)
    tk.Label(sf, text=f"Pos {i}: ", font=("Arial", 9, "bold")).pack(side="left")
    btn = tk.Button(sf, text="IDLE", bg="lightgray", width=8,
                    command=lambda pos=i: select_and_view_position("RHS", pos));
    btn.pack(side="left")
    rh_overview_buttons[i] = btn

# --- ASSESSMENT VARIANCE MATRIX DISPLAY ---
matrix_frame = tk.LabelFrame(root, text=" Position Parameters Overview ", padx=10, pady=10);
matrix_frame.pack(fill="x", padx=15, pady=5)
current_view_label = tk.Label(matrix_frame, text="Viewing: LHS - Position 1", font=("Arial", 10, "bold"), fg="#0d6efd");
current_view_label.grid(row=0, column=0, columnspan=6, sticky="w", pady=5)
headers = ["Evaluation Metric", "Master Baseline", "Test Target", "Tolerance Value", "Calculated Variance",
           "Status Indicator"]
for col_idx, text_header in enumerate(headers): tk.Label(matrix_frame, text=text_header, font=("Arial", 9, "bold"),
                                                         borderwidth=1, relief="solid", bg="#f8f9fa").grid(row=1,
                                                                                                           column=col_idx,
                                                                                                           sticky="nsew")

metrics_list = [('size', 'Image Size'), ('rotation', 'Image Rotation'), ('trap_h', 'Trapezoidal Dist. H'),
                ('trap_v', 'Trapezoidal Dist. V'), ('ar', 'Aspect Ratio'), ('translation', 'Translation'),
                ('smile', 'Smile Distortion'), ('ghosting', 'Ghosting Distance')]
ui_rows, tol_inputs = {}, {}
for row_idx, (key, label_text) in enumerate(metrics_list, start=2):
    tk.Label(matrix_frame, text=label_text, anchor="w", borderwidth=1, relief="groove").grid(row=row_idx, column=0,
                                                                                             sticky="nsew")
    m_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14);
    m_val.grid(row=row_idx, column=1, sticky="nsew")
    t_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=14);
    t_val.grid(row=row_idx, column=2, sticky="nsew")
    tol_ent = tk.Entry(matrix_frame, justify="center", width=12);
    tol_ent.grid(row=row_idx, column=3, padx=10, pady=5);
    tol_inputs[key] = tol_ent
    v_val = tk.Label(matrix_frame, text="-", borderwidth=1, relief="groove", width=15);
    v_val.grid(row=row_idx, column=4, sticky="nsew")
    s_box = tk.Label(matrix_frame, text=" IDLE ", bg="lightgray", font=("Arial", 9, "bold"), borderwidth=1,
                     relief="sunken", width=10);
    s_box.grid(row=row_idx, column=5, padx=15, pady=5)
    ui_rows[key] = {'master': m_val, 'test': t_val, 'variance': v_val, 'status': s_box}
for c in range(6): matrix_frame.grid_columnconfigure(c, weight=1)

# --- FOOTER RUNTIME STATUS TELEMETRY CONTROL PANEL ---
status_bar_frame = tk.Frame(root, padx=15, pady=10);
status_bar_frame.pack(fill="x", side="bottom")
plc_status_lbl = tk.Label(status_bar_frame, text="PLC DISCONNECTED", bg="red", fg="white", font=("Arial", 13, "bold"),
                          width=22, borderwidth=1, relief="solid");
plc_status_lbl.pack(side="left", padx=5)
vbai_status_lbl = tk.Label(status_bar_frame, text="VBAI DISCONNECTED", bg="red", fg="white", font=("Arial", 13, "bold"),
                           width=22, borderwidth=1, relief="solid");
vbai_status_lbl.pack(side="left", padx=5)
overall_status_lbl = tk.Label(status_bar_frame, text="SYSTEM IDLE", bg="lightgray", fg="black",
                              font=("Arial", 20, "bold"), width=24, borderwidth=1, relief="solid");
overall_status_lbl.pack(side="right", padx=5)

log = ScrolledText(status_bar_frame, state="disabled", height=10)
log.pack(padx=10, pady=10, fill="both", expand=True)

# Load default base metrics
defaults = {'size': '2.0', 'rotation': '2.0', 'trap_h': '1.0', 'trap_v': '1.0', 'ar': '0.05', 'translation': '5.0',
            'smile': '1.0', 'ghosting': '1.0'}
for k, e in tol_inputs.items():
    if k in defaults: e.insert(0, defaults[k])


# Start threads
thread_vb.pending_trigger = None


threading.Thread(target=thread_plc, daemon=True).start()

threading.Thread(target=thread_vb, daemon=True).start()

threading.Thread(target=thread_heartbeat, daemon=True).start()


root.after(100, status_network)
root.protocol("WM_DELETE_WINDOW", shutdown_application)
root.mainloop()