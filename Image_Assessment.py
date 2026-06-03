"""
GR1036 HUD Test Rig
Image Assessment GUI & PLC / NI Vision Builder Broker

Customer calculations:
1) Image Size
2) Image Rotation
3) Trapezoidal Distortion
4) Aspect Ratio
5) Translation
6) Smile
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

# Engine run state tracker
system_running = True

# Network Configuration parameters
PLC_PORT = 5002
VBAI_IP = "127.0.0.1"
VBAI_PORT = 6000


# ============================ Data Management & Core Sorting ================================

def load_data(file_path):
    """
    Reads and cleans CSV data. Accounts for a multi-row header, targets
    Columns F and G, and forcefully strips out any lingering text header rows.
    """
    skip_rows = 0
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for i, line in enumerate(f):
            if "Center.X" in line or "Primary" in line:
                skip_rows = i
                break

    df = pd.read_csv(file_path, sep=';', skiprows=skip_rows)
    df.columns = df.columns.str.strip()

    target_x = None
    target_y = None

    for col in df.columns:
        if "Primary" in col and "X Position" in col:
            target_x = col
        elif "Primary" in col and "Y Position" in col:
            target_y = col

    if not target_x or not target_y:
        if len(df.columns) >= 7:
            target_x = df.columns[5]  # Column F
            target_y = df.columns[6]  # Column G
        else:
            raise ValueError(f"The CSV structure is invalid. Expected at least 7 columns.")

    df = df.rename(columns={target_x: 'x_prim', target_y: 'y_prim'})

    for col in ['x_prim', 'y_prim']:
        df[col] = df[col].astype(str).str.replace(',', '.')
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=['x_prim', 'y_prim'])

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
    """
    Automated core pipeline.
    Modes:
      "LHS"  -> Clear memory, grab last 5 LHS files only.
      "RHS"  -> Clear memory, grab last 5 RHS files only.
      "BOTH" -> Clear memory, grab last 5 LHS AND last 5 RHS (10 files total).
    """
    global lh_positions_db, rh_positions_db

    if not os.path.exists(watch_directory):
        messagebox.showerror("Directory Error", f"The watch directory does not exist:\n{watch_directory}")
        return

    # Clear target selection bases depending on chosen operator option
    if mode == "LHS" or mode == "BOTH":
        lh_positions_db.clear()
    if mode == "RHS" or mode == "BOTH":
        rh_positions_db.clear()

    all_raw_files = []
    for entry in os.listdir(watch_directory):
        full_path = os.path.join(watch_directory, entry)
        if os.path.isfile(full_path) and entry.lower().endswith('.csv'):
            all_raw_files.append((full_path, entry.lower(), os.path.getmtime(full_path)))

    # 1. Harvest LHS if requested
    loaded_lh = 0
    if mode == "LHS" or mode == "BOTH":
        lhs_candidates = [f for f in all_raw_files if "lh" in f[1]]
        lhs_candidates.sort(key=lambda x: x[2], reverse=True)  # Newest first
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

    # 2. Harvest RHS if requested
    loaded_rh = 0
    if mode == "RHS" or mode == "BOTH":
        rhs_candidates = [f for f in all_raw_files if "rh" in f[1]]
        rhs_candidates.sort(key=lambda x: x[2], reverse=True)  # Newest first
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

    # Dynamic status logging feedback updates
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


def check_run_conditions():
    if master_df is not None and (len(lh_positions_db) > 0 or len(rh_positions_db) > 0):
        run_btn.config(state=tk.NORMAL, bg="#198754", fg="white")
    else:
        run_btn.config(state=tk.DISABLED, bg="#e0e0e0", fg="#a0a0a0")


def run_all_calculations(df):
    return {
        'image_size': df_size_calc(df),
        'aspect_ratio': df_ar_calc(df),
        'smile': df_smile_calc(df),
        'rotation': df_rot_calc(df),
        'translation': df_transl_calc(df),
        'trap_dist': df_trap_calc(df)
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


def process_variant_database(source_db, results_db, m_res):
    any_fail = False
    for pos_idx, t_df in source_db.items():
        t_res = run_all_calculations(t_df)
        metrics = {}

        # 1. Image Size Calculations
        m_p = 2 * (m_res['image_size'][0] + m_res['image_size'][1])
        t_p = 2 * (t_res['image_size'][0] + t_res['image_size'][1])
        size_diff = ((t_p - m_p) / m_p) * 100
        metrics['size'] = ("Image Size", f"{round(m_res['image_size'][0], 1)}x{round(m_res['image_size'][1], 1)}",
                           f"{round(t_res['image_size'][0], 1)}x{round(t_res['image_size'][1], 1)}",
                           f"{round(size_diff, 3)} %",
                           "PASS" if abs(size_diff) <= float(tol_inputs['size'].get()) else "FAIL")

        # 2. Image Rotation Calculations
        rot_diff = t_res['rotation'] - m_res['rotation']
        metrics['rotation'] = ("Image Rotation", f"{round(m_res['rotation'], 2)}°", f"{round(t_res['rotation'], 2)}°",
                               f"{round(rot_diff, 3)} °",
                               "PASS" if abs(rot_diff) <= float(tol_inputs['rotation'].get()) else "FAIL")

        # 3. Trapezoidal Distortion Calculations
        h_diff = t_res['trap_dist'][0] - m_res['trap_dist'][0]
        v_diff = t_res['trap_dist'][1] - m_res['trap_dist'][1]
        max_trap_diff = h_diff if abs(h_diff) > abs(v_diff) else v_diff
        metrics['trap'] = ("Trapezoidal Dist.",
                           f"H:{round(m_res['trap_dist'][0], 1)}% V:{round(m_res['trap_dist'][1], 1)}%",
                           f"H:{round(t_res['trap_dist'][0], 1)}% V:{round(t_res['trap_dist'][1], 1)}%",
                           f"{round(max_trap_diff, 3)} % delta",
                           "PASS" if abs(max_trap_diff) <= float(tol_inputs['trap'].get()) else "FAIL")

        # 4. Aspect Ratio Calculations
        ar_diff = t_res['aspect_ratio'] - m_res['aspect_ratio']
        metrics['ar'] = ("Aspect Ratio", f"{round(m_res['aspect_ratio'], 3)}", f"{round(t_res['aspect_ratio'], 3)}",
                         f"{round(ar_diff, 3)}",
                         "PASS" if abs(ar_diff) <= float(tol_inputs['ar'].get()) else "FAIL")

        # 5. Translation Calculations
        dist = np.sqrt((t_res['translation'][0] - m_res['translation'][0]) ** 2 + (
                    t_res['translation'][1] - m_res['translation'][1]) ** 2)
        metrics['translation'] = ("Translation",
                                  f"X:{round(m_res['translation'][0], 1)} Y:{round(m_res['translation'][1], 1)}",
                                  f"X:{round(t_res['translation'][0], 1)} Y:{round(t_res['translation'][1], 1)}",
                                  f"{round(dist, 3)} px",
                                  "PASS" if abs(dist) <= float(tol_inputs['translation'].get()) else "FAIL")

        # 6. Smile Distortion Calculations
        smile_diff = t_res['smile'] - m_res['smile']
        metrics['smile'] = ("Smile Distortion", f"{round(m_res['smile'], 1)} px", f"{round(t_res['smile'], 1)} px",
                            f"{round(smile_diff, 3)} px",
                            "PASS" if abs(smile_diff) <= float(tol_inputs['smile'].get()) else "FAIL")

        for k in metrics:
            if metrics[k][4] == "FAIL": any_fail = True
        results_db[pos_idx] = metrics
    return any_fail


def execute_assessment():
    global lh_results_db, rh_results_db, tx_camera_pass, tx_camera_fail, tx_error_code
    if master_df is None: return
    m_res = run_all_calculations(master_df)

    lh_failed = process_variant_database(lh_positions_db, lh_results_db, m_res)
    rh_failed = process_variant_database(rh_positions_db, rh_results_db, m_res)

    if lh_failed or rh_failed:
        tx_camera_pass, tx_camera_fail, tx_error_code = False, True, 101
    else:
        tx_camera_pass, tx_camera_fail, tx_error_code = True, False, 0
    refresh_displayed_position_metrics()


def refresh_displayed_position_metrics(*args):
    selected_variant = variant_view_var.get()
    selected_pos = int(pos_view_combobox.get().split(" ")[1])
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
    global tx_position_echo, tx_barcode_string, tx_barcode_pass, tx_barcode_fail, tx_error, tx_error_code, tx_camera_pass, tx_camera_fail
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(('0.0.0.0', PLC_PORT)); server_socket.listen(1)
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
                data = client_socket.recv(56)
                if not data or len(data) < 56: break
                byte0, byte1, error_code, robot_pos, barcode_bytes = struct.unpack("!BBHH50s", data[:56])
                tx_position_echo = robot_pos
                tx_barcode_string = barcode_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n ')

                if bool(byte0 & (1 << 3)) and vbai_socket:  # Trigger bit active
                    variant_cmd = "LHS" if bool(byte0 & (1 << 4)) else "RHS" if bool(byte0 & (1 << 5)) else "RUN"
                    vbai_reply = handle_vbai_block_comms(vbai_socket, f"{variant_cmd}_POS{robot_pos}")

                    if "PASS" in vbai_reply or vbai_reply.startswith("1"):
                        tx_camera_pass, tx_camera_fail, tx_error_code = True, False, 0
                        # Auto-ingest match-up type directly aligned to active line running variant profile
                        gui_queue.put(("AUTO_INGEST_TRIGGER", variant_cmd))
                    else:
                        tx_camera_pass, tx_camera_fail, tx_error_code = False, True, 102

                tx_byte0 = 0
                if tx_heartbeat:    tx_byte0 |= (1 << 0)
                if tx_barcode_pass: tx_byte0 |= (1 << 2)
                if tx_camera_pass:  tx_byte0 |= (1 << 4)
                if tx_camera_fail:  tx_byte0 |= (1 << 5)
                encoded_barcode = tx_barcode_string.encode('utf-8')[:50].ljust(50, b'\x00')
                client_socket.sendall(
                    struct.pack("!BBHH50s", tx_byte0, 0, tx_error_code, tx_position_echo, encoded_barcode))
        except Exception:
            pass
        finally:
            if client_socket: client_socket.close()
            if vbai_socket: vbai_socket.close()
            time.sleep(1.0)


def listen_for_network_queue():
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
                comms_terminal.insert(tk.END,
                                      f"[{datetime.now().strftime('%H:%M:%S')}] Automated {target_mode} file sweep active.\n")
                comms_terminal.see(tk.END)
                auto_ingest_pipeline(mode=target_mode)
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
root.title("GR1036 HUD Test Rig Image Assessment & Selected Automated Broker")
root.geometry("1150x880")

# Watch Directory Master Configuration Layout Block
upload_frame = tk.LabelFrame(root, text=" Target Ingestion Control Options Profile ", padx=10, pady=10)
upload_frame.pack(fill="x", padx=15, pady=5)

dir_btn = tk.Button(upload_frame, text="Set Watch Folder", command=change_watch_directory, width=18, bg="#e2e3e5")
dir_btn.grid(row=0, column=0, padx=5, pady=5)
dir_lbl = tk.Label(upload_frame, text=f"Watching: {watch_directory}", fg="blue", anchor="w")
dir_lbl.grid(row=0, column=1, columnspan=5, padx=5, pady=5, sticky="w")

master_btn = tk.Button(upload_frame, text="Upload Master CSV", command=select_master_file, width=18, bg="#d1e7dd")
master_btn.grid(row=1, column=0, padx=5, pady=5)
master_label = tk.Label(upload_frame, text="Master File Empty", fg="red", anchor="w", width=18)
master_label.grid(row=1, column=1, padx=5, pady=5)

# --- THE THREE DISCRETE AUTOMATED SYNC BUTTONS ---
lhs_sync_btn = tk.Button(upload_frame, text="Sync Only LHS (5)", command=lambda: auto_ingest_pipeline("LHS"),
                         bg="#cff4fc", width=16)
lhs_sync_btn.grid(row=1, column=2, padx=3, pady=5)

rhs_sync_btn = tk.Button(upload_frame, text="Sync Only RHS (5)", command=lambda: auto_ingest_pipeline("RHS"),
                         bg="#fff3cd", width=16)
rhs_sync_btn.grid(row=1, column=3, padx=3, pady=5)

both_sync_btn = tk.Button(upload_frame, text="Sync Both (10)", command=lambda: auto_ingest_pipeline("BOTH"),
                          bg="#d2f4ea", font=("Arial", 9, "bold"), width=15)
both_sync_btn.grid(row=1, column=4, padx=3, pady=5)

test_label = tk.Label(upload_frame, text="Test Files Empty", fg="red", anchor="w", width=30)
test_label.grid(row=1, column=5, padx=5, pady=5)

run_btn = tk.Button(upload_frame, text="Assess Data", command=execute_assessment, state=tk.DISABLED, bg="#e0e0e0",
                    fg="#a0a0a0")
run_btn.grid(row=2, column=0, padx=5, pady=5, sticky="ew")
clear_btn = tk.Button(upload_frame, text="Clear Logs", command=clear_all_data, bg="#dc3545", fg="white").grid(row=2,
                                                                                                              column=1,
                                                                                                              padx=5,
                                                                                                              pady=5,
                                                                                                              sticky="w")

# Calculations Metrics Framework Block
matrix_frame = tk.LabelFrame(root, text=" Inspection Data Assessment Matrix ", padx=10, pady=10)
matrix_frame.pack(fill="x", padx=15, pady=5)

selector_subframe = tk.Frame(matrix_frame, pady=5)
selector_subframe.grid(row=0, column=0, columnspan=6, sticky="w")

tk.Label(selector_subframe, text="Select Variant View: ", font=("Arial", 9, "bold")).pack(side="left")
variant_view_var = tk.StringVar(value="LHS")
tk.Radiobutton(selector_subframe, text="Left-Hand Side (LHS)", variable=variant_view_var, value="LHS",
               command=refresh_displayed_position_metrics).pack(side="left", padx=5)
tk.Radiobutton(selector_subframe, text="Right-Hand Side (RHS)", variable=variant_view_var, value="RHS",
               command=refresh_displayed_position_metrics).pack(side="left", padx=5)

tk.Label(selector_subframe, text=" | Robot Position Target: ", font=("Arial", 9, "bold")).pack(side="left", padx=5)
pos_view_combobox = ttk.Combobox(selector_subframe, values=[f"Position {i}" for i in range(1, 6)], width=12,
                                 state="readonly")
pos_view_combobox.set("Position 1")
pos_view_combobox.pack(side="left", padx=5)
pos_view_combobox.bind("<<ComboboxSelected>>", refresh_displayed_position_metrics)

headers = ["Evaluation Metric", "Master Baseline", "Test Target", "Tolerance Value", "Calculated Variance",
           "Status Indicator"]
for col_idx, text_header in enumerate(headers):
    tk.Label(matrix_frame, text=text_header, font=("Arial", 9, "bold"), borderwidth=1, relief="solid", padx=5, pady=5,
             bg="#f8f9fa").grid(row=1, column=col_idx, sticky="nsew")

metrics_list = [('size', 'Image Size'), ('rotation', 'Image Rotation'), ('trap', 'Trapezoidal Dist.'),
                ('ar', 'Aspect Ratio'), ('translation', 'Translation'), ('smile', 'Smile Distortion')]
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

# Comms Monitor Panels
comms_frame = tk.LabelFrame(root, text=" Network Handshake Interface Monitor Panel ", padx=10, pady=10)
comms_frame.pack(fill="both", expand=True, padx=15, pady=10)
status_bar_frame = tk.Frame(comms_frame).pack(fill="x", pady=2)
plc_status_lbl = tk.Label(status_bar_frame, text="NO CONNECTION", bg="red", fg="white", font=("Arial", 9, "bold"),
                          width=16)
plc_status_lbl.pack(side="left", padx=10)
vbai_status_lbl = tk.Label(status_bar_frame, text="NO CONNECTION", bg="red", fg="white", font=("Arial", 9, "bold"),
                           width=16)
vbai_status_lbl.pack(side="left")
comms_terminal = tk.Text(comms_frame, height=5, bg="black", fg="#00FF00", font=("Consolas", 9))
comms_terminal.pack(fill="both", expand=True, pady=5)

# Initialize defaults
for k, e in tol_inputs.items(): e.insert(0,
                                         "1.0" if "size" in k or "trap" in k else "2.0" if "rot" in k else "0.05" if "ar" in k else "5.0")
threading.Thread(target=plc_network_broker_worker, daemon=True).start()
threading.Thread(target=plc_heartbeat_worker, daemon=True).start()
root.after(100, listen_for_network_queue)
root.protocol("WM_DELETE_WINDOW", shutdown_application)
root.mainloop()