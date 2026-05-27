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
from tkinter import filedialog, messagebox, ttk
import numpy as np

# Global variables to store our data states
master_df = None
test_df = None


# ============================ Data Management & Core Sorting ================================

def load_data(file_path):
    """Reads and cleans the CSV data for primary coordinates."""
    try:
        df = pd.read_csv(file_path, sep=';')

        df = df.rename(columns={
            'Find Primary Centre:Center.X Position (Pixel) - Check for Part and Inspect': 'x_prim',
            'Find Primary Centre:Center.Y Position (Pixel) - Check for Part and Inspect': 'y_prim',
        })

        # Data cleaning: Convert commas to dots and cast to float
        for col in ['x_prim', 'y_prim']:
            if col in df.columns:
                df[col] = df[col].astype(str).str.replace(',', '.').astype(float)

        if len(df) != 77:
            messagebox.showwarning("Warning", f"File contains {len(df)} points instead of expected 77 grid dots.")

        return df
    except Exception as e:
        messagebox.showerror("Error", f"Failed to load CSV: {e}")
        return None


def get_grid_points(df):
    """
    Takes the 77 jumbled primary coordinates from the HUD test rig CSV
    and reliably reconstructs them into a 11x7 grid map.
    """
    # 1. Ensure we actually have 77 points to avoid index crashes
    if len(df) != 77:
        raise ValueError(f"Grid integrity check failed. Expected 77 points, found {len(df)}.")

    # 2. Dynamic Row Binning: Calculate the total vertical span of the grid
    y_min = df['y_prim'].min()
    y_max = df['y_prim'].max()
    total_height = y_max - y_min

    # Divide the total height by 6 (the spaces between 7 rows) to get average row spacing
    approx_row_spacing = total_height / 6

    # Assign each point to a row index (0 to 6) by shifting to 0 and dividing by spacing
    # This guarantees that points on the same tilted line share the exact same row_group identifier
    df['row_group'] = ((df['y_prim'] - y_min) / approx_row_spacing).round()

    # 3. Two-step sort: First sort by row top-to-bottom, then by X left-to-right
    grid = df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True)

    # 4. Map our critical target points using explicit 11x7 matrix indices
    # Row 0 (Top): Indices 0 to 10  -> Midpoint is 5
    # Row 3 (Mid): Indices 33 to 43 -> Midpoint is 38 (Absolute Center)
    # Row 6 (Bot): Indices 66 to 76 -> Midpoint is 71
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


# ============================ UI Interaction Functions ================================

def select_master_file():
    global master_df
    file_path = filedialog.askopenfilename(title="Select Master CSV File", filetypes=[("CSV files", "*.csv")])
    if file_path:
        master_df = load_data(file_path)
        master_label.config(text=f"Master: Loaded...", fg="green")
        check_run_conditions()


def select_test_file():
    global test_df
    file_path = filedialog.askopenfilename(title="Select Test Data CSV File", filetypes=[("CSV files", "*.csv")])
    if file_path:
        test_df = load_data(file_path)
        test_label.config(text=f"Test: Loaded...", fg="green")
        check_run_conditions()


def check_run_conditions():
    """Enables the assessment button only when both data states are satisfied."""
    if master_df is not None and test_df is not None:
        run_btn.config(state=tk.NORMAL)


# ============================ Specific Math Functions =============================

def imsize_calc(df):
    """Returns perimeter metrics calculated from the primary corner coordinates."""
    pts = get_grid_points(df)
    width = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    height = pts['bottom_left']['y_prim'] - pts['top_left']['y_prim']
    return width, height


def ar_calc(df):
    """Calculates aspect ratio width over height from bounding nodes."""
    pts = get_grid_points(df)
    width = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    height = pts['bottom_left']['y_prim'] - pts['top_left']['y_prim']
    return width / height if height != 0 else 0


def smile_calc(df):
    """Finds target geometric curvature across the upper row edge profile."""
    pts = get_grid_points(df)
    corners_y_avg = (pts['top_left']['y_prim'] + pts['top_right']['y_prim']) / 2
    return pts['top_mid']['y_prim'] - corners_y_avg


def imrot_calc(df):
    """Calculates absolute inclination angle of the top row vector in degrees."""
    pts = get_grid_points(df)
    dx = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    dy = pts['top_right']['y_prim'] - pts['top_left']['y_prim']
    return np.degrees(np.arctan2(dy, dx))


def transl_calc(df):
    """Extracts raw central coordinate pairing point."""
    pts = get_grid_points(df)
    return pts['center']['x_prim'], pts['center']['y_prim']


def trapdist_calc(df):
    """Computes separate vector percentages comparing parallel layout limits."""
    pts = get_grid_points(df)

    top_w = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    bot_w = pts['bottom_right']['x_prim'] - pts['bottom_left']['x_prim']
    h_trap = ((top_w - bot_w) / top_w) * 100

    left_h = pts['bottom_left']['y_prim'] - pts['top_left']['y_prim']
    right_h = pts['bottom_right']['y_prim'] - pts['top_right']['y_prim']
    v_trap = ((left_h - right_h) / left_h) * 100

    return h_trap, v_trap


# ============================ Orchestrator & Comparison Logic =============================

def run_all_calculations(df):
    """Runs data profile extraction on a single dataset frame state."""
    return {
        'image_size': imsize_calc(df),
        'aspect_ratio': ar_calc(df),
        'smile': smile_calc(df),
        'rotation': imrot_calc(df),
        'translation': transl_calc(df),
        'trap_dist': trapdist_calc(df)
    }


def execute_assessment():
    """Runs calculations for master and test datasets and builds comparison metrics."""
    m_res = run_all_calculations(master_df)
    t_res = run_all_calculations(test_df)

    # Clear previous entries inside layout view frame tree
    for item in result_tree.get_children():
        result_tree.delete(item)

    # 1. Image Size Delta
    m_perimeter = 2 * (m_res['image_size'][0] + m_res['image_size'][1])
    t_perimeter = 2 * (t_res['image_size'][0] + t_res['image_size'][1])
    size_pct_diff = ((t_perimeter - m_perimeter) / m_perimeter) * 100
    insert_row("Image Size", f"{round(m_res['image_size'][0], 1)}x{round(m_res['image_size'][1], 1)} px",
               f"{round(t_res['image_size'][0], 1)}x{round(t_res['image_size'][1], 1)} px",
               f"{round(size_pct_diff, 2)} % (Perimeter)")

    # 2. Image Rotation Delta
    rot_diff = t_res['rotation'] - m_res['rotation']
    insert_row("Image Rotation", f"{round(m_res['rotation'], 2)}°", f"{round(t_res['rotation'], 2)}°",
               f"{round(rot_diff, 3)}°")

    # 3. Trapezoidal Distortion Delta
    h_diff = t_res['trap_dist'][0] - m_res['trap_dist'][0]
    v_diff = t_res['trap_dist'][1] - m_res['trap_dist'][1]
    insert_row("Trapezoidal Dist.", f"H:{round(m_res['trap_dist'][0], 2)}% V:{round(m_res['trap_dist'][1], 2)}%",
               f"H:{round(t_res['trap_dist'][0], 2)}% V:{round(t_res['trap_dist'][1], 2)}%",
               f"ΔH: {round(h_diff, 2)}° | ΔV: {round(v_diff, 2)}°")

    # 4. Aspect Ratio Delta
    ar_diff = t_res['aspect_ratio'] - m_res['aspect_ratio']
    insert_row("Aspect Ratio", f"{round(m_res['aspect_ratio'], 3)}", f"{round(t_res['aspect_ratio'], 3)}",
               f"{round(ar_diff, 3)}")

    # 5. Translation Delta (Linear Euclidean Distance between centroids)
    dist = np.sqrt((t_res['translation'][0] - m_res['translation'][0]) ** 2 + (
                t_res['translation'][1] - m_res['translation'][1]) ** 2)
    insert_row("Translation", f"X:{round(m_res['translation'][0], 1)} Y:{round(m_res['translation'][1], 1)}",
               f"X:{round(t_res['translation'][0], 1)} Y:{round(t_res['translation'][1], 1)}",
               f"{round(dist, 2)} px (Linear)")

    # 6. Smile Delta
    smile_pct_diff = ((t_res['smile'] - m_res['smile']) / (m_res['smile'] if m_res['smile'] != 0 else 1)) * 100
    insert_row("Smile Distortion", f"{round(m_res['smile'], 2)} px", f"{round(t_res['smile'], 2)} px",
               f"{round(smile_pct_diff, 2)} %")


def insert_row(metric, master_val, test_val, deviation):
    result_tree.insert("", tk.END, values=(metric, master_val, test_val, deviation))


# ============================ GUI Framework Construction =======================================

root = tk.Tk()
root.title("GR1036 HUD Test Rig - Matrix Evaluation Environment")
root.geometry("650x450")

# Input File Action Frame Container
upload_frame = tk.LabelFrame(root, text=" Data Import Interface Components ", padx=10, pady=10)
upload_frame.pack(fill="x", padx=15, pady=10)

master_btn = tk.Button(upload_frame, text="Upload Master CSV", command=select_master_file, width=18, bg="#d1e7dd")
master_btn.grid(row=0, column=0, padx=5, pady=5)
master_label = tk.Label(upload_frame, text="Master File Empty", fg="red", anchor="w", width=20)
master_label.grid(row=0, column=1, padx=5, pady=5)

test_btn = tk.Button(upload_frame, text="Upload Test CSV", command=select_test_file, width=18, bg="#fff3cd")
test_btn.grid(row=1, column=0, padx=5, pady=5)
test_label = tk.Label(upload_frame, text="Test File Empty", fg="red", anchor="w", width=20)
test_label.grid(row=1, column=1, padx=5, pady=5)

run_btn = tk.Button(upload_frame, text="Run Assessment Match", command=execute_assessment, state=tk.DISABLED,
                    bg="#0d6efd", fg="white", font=("Arial", 10, "bold"))
run_btn.grid(row=0, column=2, rowspan=2, padx=25, pady=5, ipady=10)

# Treeview Output Visualization Frame Container
results_frame = tk.LabelFrame(root, text=" Calculated Parameter Metrics Assessment Matrix ", padx=10, pady=10)
results_frame.pack(fill="both", expand=True, padx=15, pady=10)

columns = ("metric", "master", "test", "deviation")
result_tree = ttk.Treeview(results_frame, columns=columns, show="headings", height=7)

result_tree.heading("metric", text="Evaluation Parameter Metric")
result_tree.heading("master", text="Master Baseline Configuration Value")
result_tree.heading("test", text="Current Target Run Capture Value")
result_tree.heading("deviation", text="Calculated Comparative Variance")

result_tree.column("metric", width=140, anchor="w")
result_tree.column("master", width=140, anchor="center")
result_tree.column("test", width=140, anchor="center")
result_tree.column("deviation", width=160, anchor="center")

result_tree.pack(fill="both", expand=True)

root.mainloop()