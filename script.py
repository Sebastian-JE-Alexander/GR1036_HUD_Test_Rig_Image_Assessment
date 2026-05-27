"""
GR1036 HUD Test Rig
Image Assessment GUI

Customer calculations:
1) Image Size
2) Image Rotation
3) Trapezoidal Distortion
4) Aspect Ratio
5) Translation
6) Smile

We then need to compare each to master image:
1) Image Size as a percentage difference
2) Image Rotation as a difference in degrees
3) Trapezoidal Distortion as a difference in degrees
4) Aspect Ratio as a difference in decimal
5) Translation as a straight line distance between master and test
6) Smile expressed as a difference in degrees, or as percentage difference
"""

import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox
import numpy as np
from datetime import datetime
import csv
import os

# Global variables to store our data states
master_df = None
test_df = None


# ============================ Data Management & Core Sorting ================================

def load_data(file_path):
    """
    Reads and cleans CSV data. Accounts for a multi-row header, targets
    Columns F and G, and forcefully strips out any lingering text header rows.
    Raises ValueError on structural failures.
    """
    # Step 1: Scan the file to find where the headers start
    skip_rows = 0
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for i, line in enumerate(f):
            if "Center.X" in line or "Primary" in line:
                skip_rows = i
                break

    # Step 2: Read the CSV
    df = pd.read_csv(file_path, sep=';', skiprows=skip_rows)
    df.columns = df.columns.str.strip()

    # Step 3: Identify target columns (Flexible string matching or Column F/G fallback)
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

    # Step 4: Force text to numbers
    for col in ['x_prim', 'y_prim']:
        df[col] = df[col].astype(str).str.replace(',', '.')
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Drop any row that isn't a pure number
    df = df.dropna(subset=['x_prim', 'y_prim'])

    # Step 5: Verify we have our 77 dots
    if len(df) != 77:
        raise ValueError(f"Grid integrity check failed. Expected exactly 77 points, found {len(df)}.")

    return df


def get_grid_points(df):
    """Takes a jumbled CSV of 77 points and organizes them into an 11x7 grid map."""
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


# ============================ Specific Math Functions =============================

def imsize_calc(df):
    pts = get_grid_points(df)
    width = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    height = pts['bottom_left']['y_prim'] - pts['top_left']['y_prim']
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
    pts = get_grid_points(df)
    dx = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    dy = pts['top_right']['y_prim'] - pts['top_left']['y_prim']
    return np.degrees(np.arctan2(dy, dx))


def transl_calc(df):
    pts = get_grid_points(df)
    return pts['center']['x_prim'], pts['center']['y_prim']


def trapdist_calc(df):
    pts = get_grid_points(df)
    top_w = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    bot_w = pts['bottom_right']['x_prim'] - pts['bottom_left']['x_prim']
    h_trap = ((top_w - bot_w) / top_w) * 100

    left_h = pts['bottom_left']['y_prim'] - pts['top_left']['y_prim']
    right_h = pts['bottom_right']['y_prim'] - pts['top_right']['y_prim']
    v_trap = ((left_h - right_h) / left_h) * 100
    return h_trap, v_trap


# ============================ Orchestrator & UI Interaction =============================

def select_master_file():
    global master_df
    file_path = filedialog.askopenfilename(title="Select Master CSV File", filetypes=[("CSV files", "*.csv")])

    if file_path:
        # Reset current state and interface elements immediately
        master_df = None
        master_label.config(text="Processing...", fg="orange", font=("Arial", 9, "italic"))
        run_btn.config(state=tk.DISABLED)
        root.update_idletasks()

        try:
            # Attempt to execute data loading process
            master_df = load_data(file_path)
            # If load_data finishes without throwing an error, it succeeded:
            master_label.config(text="Master: Loaded...", fg="green", font=("Arial", 9, "bold"))

        except Exception as e:
            # If ANY error happens during load_data, immediately catch it here
            master_df = None
            master_label.config(text="Master: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Master CSV:\n\n{str(e)}")

        # Re-check system variables to toggle the run button
        check_run_conditions()


def select_test_file():
    global test_df
    file_path = filedialog.askopenfilename(title="Select Test Data CSV File", filetypes=[("CSV files", "*.csv")])

    if file_path:
        # Reset current state and interface elements immediately
        test_df = None
        test_label.config(text="Processing...", fg="orange", font=("Arial", 9, "italic"))
        run_btn.config(state=tk.DISABLED)
        root.update_idletasks()

        try:
            # Attempt to execute data loading process
            test_df = load_data(file_path)
            # If load_data finishes without throwing an error, it succeeded:
            test_label.config(text="Test: Loaded...", fg="green", font=("Arial", 9, "bold"))

        except Exception as e:
            # If ANY error happens during load_data, immediately catch it here
            test_df = None
            test_label.config(text="Test: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Test CSV:\n\n{str(e)}")

        # Re-check system variables to toggle the run button
        check_run_conditions()


def check_run_conditions():
    """Strictly validates global memory allocation to safely unlock execution buttons."""
    if master_df is not None and test_df is not None:
        run_btn.config(state=tk.NORMAL)
    else:
        run_btn.config(state=tk.DISABLED)

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
    """Updates display text, checks tolerances, and colors the status box square."""
    row_widgets['master'].config(text=master_txt)
    row_widgets['test'].config(text=test_txt)
    row_widgets['variance'].config(text=f"{round(variance_val, 3)} {unit_str}")

    # Get user validation limit from entry box
    try:
        tol_limit = float(tolerance_entry.get().strip())
    except ValueError:
        tol_limit = 0.0  # Default to 0 if empty or invalid string text
        tolerance_entry.delete(0, tk.END)
        tolerance_entry.insert(0, "0.0")

    # Pass/Fail conditions check
    if abs(variance_val) <= tol_limit:
        row_widgets['status'].config(bg="green", text=" PASS ", fg="white")
    else:
        row_widgets['status'].config(bg="red", text=" FAIL ", fg="white")


def execute_assessment():
    """Main calculation trigger loop."""
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

    # 3. Trapezoidal Distortion (Using maximum variance between H and V profiles)
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

    # 5. Translation (Euclidean Vector Shift distance calculation)
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


# ============================ GUI Construction Framework =======================================

root = tk.Tk()
root.title("GR1036 HUD Test Rig Assessment Environment")
root.geometry("800x480")

# Input Controller Frame Panel
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

run_btn = tk.Button(upload_frame, text="Run Data Assessment", command=execute_assessment, state=tk.DISABLED,
                    bg="#0d6efd", fg="white", font=("Arial", 10, "bold"))
run_btn.grid(row=0, column=2, rowspan=2, padx=30, pady=5, ipady=8)

# Calculations Metrics Framework Block
matrix_frame = tk.LabelFrame(root, text=" Assessment Parameters Window ", padx=10, pady=10)
matrix_frame.pack(fill="both", expand=True, padx=15, pady=10)

# Matrix Headers
headers = ["Evaluation Metric", "Master Baseline", "Test Target", "Tolerance Value Entry", "Calculated Variance",
           "Status Indicator"]
for col_idx, text_header in enumerate(headers):
    lbl = tk.Label(matrix_frame, text=text_header, font=("Arial", 9, "bold"), borderwidth=1, relief="solid", padx=5,
                   pady=5, bg="#f8f9fa")
    lbl.grid(row=0, column=col_idx, sticky="nsew")

metrics_list = [
    ('size', '1) Image Size'),
    ('rotation', '2) Image Rotation'),
    ('trap', '3) Trapezoidal Dist.'),
    ('ar', '4) Aspect Ratio'),
    ('translation', '5) Translation'),
    ('smile', '6) Smile Distortion')
]

ui_rows = {}
tol_inputs = {}

# Build out row components
for row_idx, (key, label_text) in enumerate(metrics_list, start=1):
    # Metric Label Name
    m_lbl = tk.Label(matrix_frame, text=label_text, anchor="w", font=("Arial", 9), borderwidth=1, relief="groove",
                     padx=5, pady=5)
    m_lbl.grid(row=row_idx, column=0, sticky="nsew")

    # Master Value Placeholder
    m_val = tk.Label(matrix_frame, text="-", font=("Arial", 9), borderwidth=1, relief="groove", width=14)
    m_val.grid(row=row_idx, column=1, sticky="nsew")

    # Test Value Placeholder
    t_val = tk.Label(matrix_frame, text="-", font=("Arial", 9), borderwidth=1, relief="groove", width=14)
    t_val.grid(row=row_idx, column=2, sticky="nsew")

    # Tolerance User Input Entry Field box
    tol_ent = tk.Entry(matrix_frame, font=("Arial", 9), justify="center", width=12)
    tol_ent.insert(0, "0.5")  # Seed a basic template value default constraint
    tol_ent.grid(row=row_idx, column=3, padx=10, pady=5)
    tol_inputs[key] = tol_ent

    # Variance Output Window Text
    v_val = tk.Label(matrix_frame, text="-", font=("Arial", 9), borderwidth=1, relief="groove", width=15)
    v_val.grid(row=row_idx, column=4, sticky="nsew")

    # Status colored block placeholder panel
    s_box = tk.Label(matrix_frame, text=" IDLE ", bg="lightgray", font=("Arial", 9, "bold"), borderwidth=1,
                     relief="sunken", width=10)
    s_box.grid(row=row_idx, column=5, padx=15, pady=5)

    # Bundle reference targets for extraction loops
    ui_rows[key] = {'master': m_val, 'test': t_val, 'variance': v_val, 'status': s_box}

# Standard grid config normalization parameters
for c in range(6):
    matrix_frame.grid_columnconfigure(c, weight=1)

root.mainloop()