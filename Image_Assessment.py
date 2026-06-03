"""
GR1036 HUD Test Rig
Image Assessment GUI & PLC Communication Broker

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
test_df = None

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


# ============================ Data Management & Core Sorting ================================

def load_data(file_path):
    """
    Reads and cleans CSV data. Accounts for a multi-row header, targets
    Columns F and G, and forcefully strips out any lingering text header rows.
    Raises ValueError on structural failures.
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
            raise ValueError(f"The CSV structure is invalid. Expected at least 7 columns, found {len(df.columns)}.")

    df = df.rename(columns={target_x: 'x_prim', target_y: 'y_prim'})

    for col in ['x_prim', 'y_prim']:
        df[col] = df[col].astype(str).str.replace(',', '.')
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=['x_prim', 'y_prim'])

    if len(df) != 77:
        raise ValueError(f"Grid integrity check failed. Expected exactly 77 points, found {len(df)}.")

    return df


def get_grid_points(df):
    """
    Takes a jumbled CSV of 77 points and organizes them into a 11x7 grid map.
    """
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


def load_custom_tolerances():
    """
    Opens a file dialogue to upload a saved tolerance profile configuration text file
    and updates the active GUI input fields automatically using fuzzy partial matching.
    """
    file_path = filedialog.askopenfilename(
        title="Select Variant Tolerance Profile",
        filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
    )

    if not file_path:
        return

    try:
        with open(file_path, 'r') as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "=" in line:
                    file_metric, value = line.split("=", 1)
                    file_metric = file_metric.strip().lower()
                    value = value.strip()

                    for real_key in tol_inputs.keys():
                        real_key_lower = real_key.lower()
                        if file_metric in real_key_lower or real_key_lower in file_metric:
                            tol_inputs[real_key].delete(0, tk.END)
                            tol_inputs[real_key].insert(0, value)
                            break

        messagebox.showinfo("Success", "Variant tolerance profile loaded successfully!")

    except Exception as e:
        messagebox.showerror("Error", f"Failed to read tolerance file:\n{str(e)}")


def save_assessment_record():
    """
    Gathers the current visible metrics, inputs, variances, and pass/fail states
    from the UI layout matrix and exports them to a timestamped CSV report.
    """
    if ui_rows['size']['status']['text'] == " IDLE ":
        messagebox.showwarning("Export Denied",
                               "There are no calculation results to save. Run an assessment first.")
        return

    now = datetime.now()
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    filename_timestamp = now.strftime("%Y%m%d_%H%M%S")

    default_filename = f"HUD_Assessment_Record_{filename_timestamp}.csv"
    save_path = filedialog.asksaveasfilename(
        title="Save Assessment Record",
        initialfile=default_filename,
        filetypes=[("CSV files", "*.csv")]
    )

    if not save_path:
        return

    try:
        with open(save_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(["GR1036 HUD TEST RIG IMAGE ASSESSMENT REPORT"])
            writer.writerow([f"Execution Date/Time", timestamp_str])
            writer.writerow([])

            writer.writerow(["Evaluation Criteria", "Master", "Test", "Tolerance Value Constraint",
                             "Calculated Variance", "Status Result"])

            for key, tuple_info in metrics_list:
                metric_name = tuple_info
                master_val = ui_rows[key]['master']['cget']('text')
                test_val = ui_rows[key]['test']['cget']('text')
                tolerance = tol_inputs[key].get().strip()
                variance = ui_rows[key]['variance']['cget']('text')
                status_text = ui_rows[key]['status']['cget']('text').strip()

                writer.writerow([metric_name, master_val, test_val, tolerance, variance, status_text])

        messagebox.showinfo("Export Successful",
                            f"Assessment record successfully saved to:\n\n{os.path.basename(save_path)}")

    except Exception as e:
        messagebox.showerror("Export Error", f"Failed to generate assessment record file:\n{str(e)}")


# ============================ Math Functions =============================

def imsize_calc(df):
    if df.empty:
        return 0.0, 0.0
    width = df['x_prim'].max() - df['x_prim'].min()
    height = df['y_prim'].max() - df['y_prim'].min()
    return width, height


def ar_calc(df):
    pts = get_grid_points(df)
    width = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    height = pts['bottom_left']['y_prim'] - pts['top_left']['y_prim']
    return width / height if height != 0 else 0


def smile_calc(df):
    pts = get_grid_points(df)
    corners_y_avg = (pts['top_left']['y_prim'] + pts['top_right']['y_prim']) / 2
    return pts['top_mid']['y_prim'] - corners_y_avg


def imrot_calc(df):
    if df.empty:
        return 0.0
    try:
        tl = df.loc[(df['x_prim'] + df['y_prim']).idxmin()]
        tr = df.loc[(df['x_prim'] - df['y_prim']).idxmax()]
        dx = tr['x_prim'] - tl['x_prim']
        dy = tr['y_prim'] - tl['y_prim']
        angle_rad = np.arctan2(dy, dx)
        return np.degrees(angle_rad)
    except Exception as e:
        print(f"Error in rotation calculation: {str(e)}")
        return 0.0


def transl_calc(df):
    pts = get_grid_points(df)
    return pts['center']['x_prim'], pts['center']['y_prim']


def trapdist_calc(df):
    if df.empty:
        return 0.0, 0.0
    try:
        tl = df.loc[(df['x_prim'] + df['y_prim']).idxmin()]
        br = df.loc[(df['x_prim'] + df['y_prim']).idxmax()]
        tr = df.loc[(df['x_prim'] - df['y_prim']).idxmax()]
        bl = df.loc[(df['x_prim'] - df['y_prim']).idxmin()]

        top_width = tr['x_prim'] - tl['x_prim']
        bottom_width = br['x_prim'] - bl['x_prim']
        left_height = bl['y_prim'] - tl['y_prim']
        right_height = br['y_prim'] - tr['y_prim']

        base_width = min(top_width, bottom_width)
        h_distortion = (abs(top_width - bottom_width) / base_width) * 100 if base_width > 0 else 0.0

        base_height = min(left_height, right_height)
        v_distortion = (abs(left_height - right_height) / base_height) * 100 if base_height > 0 else 0.0

        return h_distortion, v_distortion
    except Exception as e:
        print(f"Error in trapezoidal calculation: {str(e)}")
        return 0.0, 0.0


# ============================ Orchestrator & UI Interaction =============================

def select_master_file():
    global master_df
    file_path = filedialog.askopenfilename(title="Select Master CSV File", filetypes=[("CSV files", "*.csv")])

    if file_path:
        master_df = None
        master_label.config(text="Processing...", fg="orange", font=("Arial", 9, "italic"))
        run_btn.config(state=tk.DISABLED)
        root.update_idletasks()

        try:
            master_df = load_data(file_path)
            master_label.config(text="Master: Loaded", fg="green", font=("Arial", 9, "bold"))
        except Exception as e:
            master_df = None
            master_label.config(text="Master: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Master CSV:\n\n{str(e)}")

        check_run_conditions()


def select_test_file():
    global test_df
    file_path = filedialog.askopenfilename(title="Select Test Data CSV File", filetypes=[("CSV files", "*.csv")])

    if file_path:
        test_df = None
        test_label.config(text="Processing...", fg="orange", font=("Arial", 9, "italic"))
        run_btn.config(state=tk.DISABLED)
        root.update_idletasks()

        try:
            test_df = load_data(file_path)
            test_label.config(text="Test: Loaded", fg="green", font=("Arial", 9, "bold"))
        except Exception as e:
            test_df = None
            test_label.config(text="Test: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Test CSV:\n\n{str(e)}")

        check_run_conditions()


def clear_all_data():
    global master_df, test_df
    if not messagebox.askyesno("Clear Dashboard",
                               "Are you sure you want to reset all current calculations and clear loaded files?"):
        return

    master_df = None
    test_df = None
    master_label.config(text="Master File Empty", fg="red", font=("Arial", 9, "normal"))
    test_label.config(text="Test File Empty", fg="red", font=("Arial", 9, "normal"))
    check_run_conditions()

    for key in ui_rows:
        ui_rows[key]['master'].config(text="-")
        ui_rows[key]['test'].config(text="-")
        ui_rows[key]['variance'].config(text="-")
        ui_rows[key]['status'].config(bg="lightgray", text=" IDLE ", fg="black")

        tol_inputs[key].delete(0, tk.END)
        tol_inputs[key].insert(0, "0.5")

    messagebox.showinfo("Reset Complete", "The data matrix and file logs have been successfully cleared.")


def check_run_conditions():
    if master_df is not None and test_df is not None:
        run_btn.config(state=tk.NORMAL, bg="#198754", fg="white")
    else:
        run_btn.config(state=tk.DISABLED, bg="#e0e0e0", fg="#a0a0a0")


def run_all_calculations(df):
    return {
        'image_size': imsize_calc(df),
        'aspect_ratio': ar_calc(df),
        'smile': smile_calc(df),
        'rotation': imrot_calc(df),
        'translation': transl_calc(df),
        'trap_dist': trapdist_calc(df)
    }


def update_ui_row(row_widgets, master_txt, test_txt, variance_val, unit_str, tolerance_entry):
    row_widgets['master'].config(text=master_txt)
    row_widgets['test'].config(text=test_txt)
    row_widgets['variance'].config(text=f"{round(variance_val, 3)} {unit_str}")

    try:
        tol_limit = float(tolerance_entry.get().strip())
    except ValueError:
        tol_limit = 0.5
        tolerance_entry.delete(0, tk.END)
        tolerance_entry.insert(0, "0.5")

    if "%" in unit_str and abs(variance_val) <= 1.0:
        tol_limit = tol_limit / 100.0

    if abs(variance_val) <= tol_limit:
        row_widgets['status'].config(bg="green", text=" PASS ", fg="white")
    else:
        row_widgets['status'].config(bg="red", text=" FAIL ", fg="white")


def execute_assessment():
    global tx_camera_pass, tx_camera_fail, tx_error_code

    m_res = run_all_calculations(master_df)
    t_res = run_all_calculations(test_df)

    # 1. Image Size
    m_p = 2 * (m_res['image_size'][0] + m_res['image_size'][1])
    t_p = 2 * (t_res['image_size'][0] + t_res['image_size'][1])
    size_diff = ((t_p - m_p) / m_p) * 100
    update_ui_row(ui_rows['size'], f"{round(m_res['image_size'][0], 1)}x{round(m_res['image_size'][1], 1)}",
                  f"{round(t_res['image_size'][0], 1)}x{round(t_res['image_size'][1], 1)}", size_diff, "%",
                  tol_inputs['size'])

    # 2. Image Rotation
    rot_diff = t_res['rotation'] - m_res['rotation']
    update_ui_row(ui_rows['rotation'], f"{round(m_res['rotation'], 2)}°", f"{round(t_res['rotation'], 2)}°", rot_diff,
                  "°", tol_inputs['rotation'])

    # 3. Trapezoidal Distortion
    h_diff = t_res['trap_dist'][0] - m_res['trap_dist'][0]
    v_diff = t_res['trap_dist'][1] - m_res['trap_dist'][1]
    max_trap_diff = h_diff if abs(h_diff) > abs(v_diff) else v_diff
    update_ui_row(ui_rows['trap'], f"H:{round(m_res['trap_dist'][0], 1)}% V:{round(m_res['trap_dist'][1], 1)}%",
                  f"H:{round(t_res['trap_dist'][0], 1)}% V:{round(t_res['trap_dist'][1], 1)}%", max_trap_diff,
                  "% delta", tol_inputs['trap'])

    # 4. Aspect Ratio
    ar_diff = t_res['aspect_ratio'] - m_res['aspect_ratio']
    update_ui_row(ui_rows['ar'], f"{round(m_res['aspect_ratio'], 3)}", f"{round(t_res['aspect_ratio'], 3)}", ar_diff,
                  "", tol_inputs['ar'])

    # 5. Translation
    dist = np.sqrt((t_res['translation'][0] - m_res['translation'][0]) ** 2 + (
            t_res['translation'][1] - m_res['translation'][1]) ** 2)
    update_ui_row(ui_rows['translation'],
                  f"X:{round(m_res['translation'][0], 1)} Y:{round(m_res['translation'][1], 1)}",
                  f"X:{round(t_res['translation'][0], 1)} Y:{round(t_res['translation'][1], 1)}", dist, "px",
                  tol_inputs['translation'])

    # 6. Smile Distortion
    smile_diff = t_res['smile'] - m_res['smile']
    update_ui_row(ui_rows['smile'], f"{round(m_res['smile'], 1)} px", f"{round(t_res['smile'], 1)} px", smile_diff,
                  "px", tol_inputs['smile'])

    # Determine aggregated pass/fail result to update PLC registers
    overall_pass = True
    for key in ui_rows:
        if ui_rows[key]['status']['cget']('text').strip() == "FAIL":
            overall_pass = False
            break

    if overall_pass:
        tx_camera_pass = True
        tx_camera_fail = False
        tx_error_code = 0
    else:
        tx_camera_pass = False
        tx_camera_fail = True
        tx_error_code = 101  # Custom Out-Of-Tolerance flag


# ============================ PLC Networking Protocol Engine ============================

def plc_heartbeat_worker():
    """Toggles the watchdog bit state independently every 1 second."""
    global tx_heartbeat
    while system_running:
        tx_heartbeat = not tx_heartbeat
        time.sleep(1.0)


def plc_network_broker_worker():
    """Persistent server background worker loop. Listens for 56-byte PLC blocks on port 5002."""
    global tx_position_echo, tx_barcode_string, tx_barcode_pass, tx_barcode_fail, tx_error, tx_error_code

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind(('0.0.0.0', 5002))
        server_socket.listen(1)
    except Exception as e:
        gui_queue.put(("NETWORK_LOG", f"CRITICAL: Failed to bind port 5002: {e}"))
        return

    while system_running:
        client_socket = None
        try:
            gui_queue.put(("PLC_CONNECTION", "DISCONNECTED"))
            client_socket, addr = server_socket.accept()
            gui_queue.put(("PLC_CONNECTION", "CONNECTED"))
            gui_queue.put(("NETWORK_LOG", f"PLC Connected from: {addr}"))

            while system_running:
                data = client_socket.recv(56)
                if not data or len(data) < 56:
                    break  # Connection severed

                # Unpack big-endian industrial packet chunk
                byte0, byte1, error_code, robot_pos, barcode_bytes = struct.unpack("!BBHH50s", data[:56])

                rx_signals = {
                    "heartbeat": bool(byte0 & (1 << 0)),
                    "error": bool(byte0 & (1 << 1)),
                    "capture_barcode": bool(byte0 & (1 << 2)),
                    "trigger_camera": bool(byte0 & (1 << 3)),
                    "lhs_variant": bool(byte0 & (1 << 4)),
                    "rhs_variant": bool(byte0 & (1 << 5)),
                    "error_code": error_code,
                    "robot_position": robot_pos,
                    "barcode": barcode_bytes.decode('utf-8', errors='ignore').strip('\x00\r\n ')
                }

                # Update outbox global memory states to match PLC configurations
                tx_position_echo = rx_signals['robot_position']
                tx_barcode_string = rx_signals['barcode']
                tx_error = rx_signals['error']

                # Process automated incoming commands
                if rx_signals['capture_barcode']:
                    if len(rx_signals['barcode']) > 3:
                        tx_barcode_pass = True
                        tx_barcode_fail = False
                    else:
                        tx_barcode_pass = False
                        tx_barcode_fail = True
                        tx_error_code = 404

                # Forward data packet to main UI thread queue loop
                gui_queue.put(("PLC_PACKET_RX", rx_signals))

                # Assemble return byte array structure (Python -> PLC)
                tx_byte0 = 0
                if tx_heartbeat:    tx_byte0 |= (1 << 0)
                if tx_error:        tx_byte0 |= (1 << 1)
                if tx_barcode_pass: tx_byte0 |= (1 << 2)
                if tx_barcode_fail: tx_byte0 |= (1 << 3)
                if tx_camera_pass:  tx_byte0 |= (1 << 4)
                if tx_camera_fail:  tx_byte0 |= (1 << 5)

                encoded_barcode = tx_barcode_string.encode('utf-8')[:50].ljust(50, b'\x00')
                response_packet = struct.pack("!BBHH50s", tx_byte0, 0, tx_error_code, tx_position_echo, encoded_barcode)

                client_socket.sendall(response_packet)

        except Exception as e:
            gui_queue.put(("NETWORK_LOG", f"Socket Exception: {e}"))
        finally:
            if client_socket:
                client_socket.close()
            time.sleep(1.0)

    server_socket.close()


def listen_for_network_queue():
    """Monitors the communication queue from the main thread without blocking UI drawing operations."""
    try:
        while True:
            event_type, payload = gui_queue.get_nowait()

            if event_type == "PLC_CONNECTION":
                if payload == "CONNECTED":
                    plc_status_lbl.config(text="LINK ACTIVE", bg="green", fg="white")
                else:
                    plc_status_lbl.config(text="NO CONNECTION", bg="red", fg="white")

            elif event_type == "NETWORK_LOG":
                comms_terminal.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {payload}\n")
                comms_terminal.see(tk.END)

            elif event_type == "PLC_PACKET_RX":
                # Clear and append current cycle logs to console terminal
                variant_str = "LHS" if payload["lhs_variant"] else "RHS" if payload["rhs_variant"] else "None"
                log_line = f"Rx Frame -> Pos: {payload['robot_position']} | Var: {variant_str} | Code: '{payload['barcode']}'"

                comms_terminal.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {log_line}\n")
                comms_terminal.see(tk.END)

                # Optional automation anchor point:
                # if payload['trigger_camera']:
                #      execute_assessment()

            gui_queue.task_done()
    except Empty:
        pass

    if system_running:
        root.after(50, listen_for_network_queue)


def shutdown_application():
    """Cleans up sockets and background processes before closing the window."""
    global system_running
    system_running = False
    root.destroy()


# ============================ GUI Construction =======================================

root = tk.Tk()
root.title("GR1036 HUD Test Rig Image Assessment & Comms Broker")
root.geometry("1150x780")  # Expanded vertically to cleanly support the networking panel

logo_frame = tk.Frame(root, pady=10)
logo_frame.pack(fill="x", padx=30)

# --- 1. COMPANY LOGO LOADER ---
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    comp_path = None
    for filename in os.listdir(script_dir):
        if filename.lower().startswith("granroth_logo"):
            comp_path = os.path.join(script_dir, filename)
            break

    if comp_path is None:
        raise FileNotFoundError("Company logo missing")

    comp_pil = Image.open(comp_path)
    comp_pil = comp_pil.resize((260, 80), Image.Resampling.LANCZOS)
    comp_img = ImageTk.PhotoImage(comp_pil)

    comp_label = tk.Label(logo_frame, image=comp_img)
    comp_label.image = comp_img
    comp_label.pack(side="left", anchor="w")
except Exception as e:
    comp_label = tk.Label(logo_frame, text="GRANROTH HUD TEST RIG", font=("Arial", 12, "bold"), fg="#555555")
    comp_label.pack(side="left", anchor="w")

# --- 2. CUSTOMER LOGO LOADER ---
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cust_path = None
    for filename in os.listdir(script_dir):
        if filename.lower().startswith("shatterprufe_logo"):
            cust_path = os.path.join(script_dir, filename)
            break

    if cust_path is None:
        raise FileNotFoundError("Shatterprufe logo missing")

    cust_pil = Image.open(cust_path)
    cust_pil = cust_pil.resize((260, 80), Image.Resampling.LANCZOS)
    cust_img = ImageTk.PhotoImage(cust_pil)

    cust_label = tk.Label(logo_frame, image=cust_img)
    cust_label.image = cust_img
    cust_label.pack(side="right", anchor="e")
except Exception as e:
    cust_label = tk.Label(logo_frame, text="CUSTOMER EVALUATION", font=("Arial", 12, "bold"), fg="#555555")
    cust_label.pack(side="right", anchor="e")

# Data Import Control Frame Panel
upload_frame = tk.LabelFrame(root, text=" Data Import Options ", padx=10, pady=10)
upload_frame.pack(fill="x", padx=15, pady=5)

master_btn = tk.Button(upload_frame, text="Upload Master CSV", command=select_master_file, width=18, bg="#d1e7dd")
master_btn.grid(row=0, column=0, padx=5, pady=5)
master_label = tk.Label(upload_frame, text="Master File Empty", fg="red", anchor="w", width=18)
master_label.grid(row=0, column=1, padx=5, pady=5)

test_btn = tk.Button(upload_frame, text="Upload Test CSV", command=select_test_file, width=18, bg="#fff3cd")
test_btn.grid(row=1, column=0, padx=5, pady=5)
test_label = tk.Label(upload_frame, text="Test File Empty", fg="red", anchor="w", width=18)
test_label.grid(row=1, column=1, padx=5, pady=5)

run_btn = tk.Button(upload_frame, text="Run Image Assessment", command=execute_assessment, state=tk.DISABLED,
                    bg="#e0e0e0", fg="#a0a0a0", font=("Arial", 10, "bold"))
run_btn.grid(row=0, column=2, padx=10, pady=10, ipady=8, sticky="ew")

save_btn = tk.Button(upload_frame, text="💾 Save Results", command=save_assessment_record, bg="#0d6efd", fg="white",
                     font=("Arial", 10, "bold"))
save_btn.grid(row=0, column=3, padx=10, pady=10, ipady=8, sticky="ew")

load_tol_btn = tk.Button(upload_frame, text="📂 Load Tolerances", command=load_custom_tolerances, bg="#495057",
                         fg="white", font=("Arial", 10, "bold"))
load_tol_btn.grid(row=0, column=4, padx=10, pady=10, ipady=8, sticky="ew")

clear_btn = tk.Button(upload_frame, text="🔄 Clear Current Data", command=clear_all_data, bg="#dc3545", fg="white",
                      font=("Arial", 10, "bold"))
clear_btn.grid(row=0, column=5, padx=10, pady=10, ipady=8, sticky="ew")

# Calculations Metrics Framework Block
matrix_frame = tk.LabelFrame(root, text=" Assessment Parameters Window ", padx=10, pady=10)
matrix_frame.pack(fill="x", padx=15, pady=5)

headers = ["Evaluation Metric", "Master Baseline", "Test Target", "Tolerance Value", "Calculated Variance",
           "Status Indicator"]
for col_idx, text_header in enumerate(headers):
    lbl = tk.Label(matrix_frame, text=text_header, font=("Arial", 9, "bold"), borderwidth=1, relief="solid", padx=5,
                   pady=5, bg="#f8f9fa")
    lbl.grid(row=0, column=col_idx, sticky="nsew")

metrics_list = [
    ('size', 'Image Size'),
    ('rotation', 'Image Rotation'),
    ('trap', 'Trapezoidal Dist.'),
    ('ar', 'Aspect Ratio'),
    ('translation', 'Translation'),
    ('smile', 'Smile Distortion')
]

ui_rows = {}
tol_inputs = {}

for row_idx, (key, label_text) in enumerate(metrics_list, start=1):
    m_lbl = tk.Label(matrix_frame, text=label_text, anchor="w", font=("Arial", 9), borderwidth=1, relief="groove",
                     padx=5, pady=5)
    m_lbl.grid(row=row_idx, column=0, sticky="nsew")

    m_val = tk.Label(matrix_frame, text="-", font=("Arial", 9), borderwidth=1, relief="groove", width=14)
    m_val.grid(row=row_idx, column=1, sticky="nsew")

    t_val = tk.Label(matrix_frame, text="-", font=("Arial", 9), borderwidth=1, relief="groove", width=14)
    t_val.grid(row=row_idx, column=2, sticky="nsew")

    tol_ent = tk.Entry(matrix_frame, font=("Arial", 9), justify="center", width=12)
    if "Size" in key or "Dist." in key:
        tol_ent.insert(0, "1.0")
    elif "Rotation" in key:
        tol_ent.insert(0, "2.0")
    elif "Aspect Ratio" in key:
        tol_ent.insert(0, "0.05")
    elif "Translation" in key or "Smile" in key:
        tol_ent.insert(0, "5.0")
    else:
        tol_ent.insert(0, "0.5")
    tol_ent.grid(row=row_idx, column=3, padx=10, pady=5)
    tol_inputs[key] = tol_ent

    v_val = tk.Label(matrix_frame, text="-", font=("Arial", 9), borderwidth=1, relief="groove", width=15)
    v_val.grid(row=row_idx, column=4, sticky="nsew")

    s_box = tk.Label(matrix_frame, text=" IDLE ", bg="lightgray", font=("Arial", 9, "bold"), borderwidth=1,
                     relief="sunken", width=10)
    s_box.grid(row=row_idx, column=5, padx=15, pady=5)

    ui_rows[key] = {'master': m_val, 'test': t_val, 'variance': v_val, 'status': s_box}

for c in range(6):
    matrix_frame.grid_columnconfigure(c, weight=1)

# --- TCP NETWORK TERMINAL FRAME PANEL ---
comms_frame = tk.LabelFrame(root, text=" Live PLC Interface Connection ", padx=10, pady=10)
comms_frame.pack(fill="both", expand=True, padx=15, pady=10)

# Connection Status Indicator bar
status_bar_frame = tk.Frame(comms_frame)
status_bar_frame.pack(fill="x", pady=2)

tk.Label(status_bar_frame, text="TCP Server Socket Line Status:", font=("Arial", 9, "bold")).pack(side="left")
plc_status_lbl = tk.Label(status_bar_frame, text="NO CONNECTION", bg="red", fg="white", font=("Arial", 9, "bold"),
                          width=16, relief="groove")
plc_status_lbl.pack(side="left", padx=10)

# Scroll terminal interface logs
comms_terminal = tk.Text(comms_frame, height=8, bg="black", fg="#00FF00", font=("Consolas", 9))
comms_terminal.pack(fill="both", expand=True, pady=5)
comms_terminal.insert(tk.END, "[SYSTEM INITIALIZATION] Awaiting background thread handshake initialization...\n")


def apply_smart_tolerance_defaults():
    if 'tol_inputs' not in globals():
        return
    for key, entry_box in tol_inputs.items():
        entry_box.delete(0, tk.END)
        if "size" in key or "trap" in key:
            entry_box.insert(0, "1.0")
        elif "rotation" in key:
            entry_box.insert(0, "2.0")
        elif "ar" in key:
            entry_box.insert(0, "0.05")
        else:
            entry_box.insert(0, "5.0")


# Kick off worker loops
apply_smart_tolerance_defaults()

# Spin up independent background threads
threading.Thread(target=plc_network_broker_worker, daemon=True).start()
threading.Thread(target=plc_heartbeat_worker, daemon=True).start()

# Start checking queue data strings within the Tkinter loop
root.after(100, listen_for_network_queue)

# Graceful Window Termination management intercept hook
root.protocol("WM_DELETE_WINDOW", shutdown_application)

root.mainloop()