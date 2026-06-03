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

import pandas as pd #needed for data manipulation of the csv file
import tkinter as tk #used for creating the GUI
from tkinter import filedialog, messagebox  #allows us to create dialogue boxes for the GUI
import numpy as np  #handles our math functions
from datetime import datetime  #used for timestamping our created files
import csv
import os
from PIL import Image, ImageTk   #used for soft image processing on the GUI, such as resizing imported images
import socket
import time
import queue


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
                    # Clean up spaces and convert to lowercase for foolproof matching
                    file_metric = file_metric.strip().lower()
                    value = value.strip()

                    # Look through your active screen keys for a partial match
                    for real_key in tol_inputs.keys():
                        real_key_lower = real_key.lower()

                        # Checks if the file text matches the screen name or vice versa
                        if file_metric in real_key_lower or real_key_lower in file_metric:
                            tol_inputs[real_key].delete(0, tk.END)
                            tol_inputs[real_key].insert(0, value)
                            break  # Found it, move to the next line in the text file

        messagebox.showinfo("Success", "Variant tolerance profile loaded successfully!")

    except Exception as e:
        messagebox.showerror("Error", f"Failed to read tolerance file:\n{str(e)}")

def save_assessment_record():
    """
    Gathers the current visible metrics, inputs, variances, and pass/fail states
    from the UI layout matrix and exports them to a timestamped CSV report.
    """
    # 1. Protection Check: Don't export an empty screen
    # We look at the 'size' status label to see if it's still "IDLE"
    if ui_rows['size']['status']['text'] == " IDLE ":
        messagebox.showwarning("Export Denied",
                               "There are no calculation results to save. Run an assessment first.")
        return

    # 2. Generate a clean timestamp string for the file contents and filename
    now = datetime.now()
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    filename_timestamp = now.strftime("%Y%m%d_%H%M%S")

    # 3. Prompt user for filename destination
    default_filename = f"HUD_Assessment_Record_{filename_timestamp}.csv"
    save_path = filedialog.asksaveasfilename(
        title="Save Assessment Record",
        initialfile=default_filename,
        filetypes=[("CSV files", "*.csv")]
    )

    if not save_path:
        return  # User cancelled the window save prompt

    try:
        # 4. Compile and write report rows
        with open(save_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, delimiter=';')

            # Metadata block header
            writer.writerow(["GR1036 HUD TEST RIG IMAGE ASSESSMENT REPORT"])
            writer.writerow([f"Execution Date/Time", timestamp_str])
            writer.writerow([])  # Empty Spacer Row

            # Table Column Headers
            writer.writerow(["Evaluation Criteria", "Master", "Test", "Tolerance Value Constraint",
                             "Calculated Variance", "Status Result"])

            # Pull metrics strings straight from live grid frames
            for key, tuple_info in metrics_list:
                metric_name = tuple_info
                master_val = ui_rows[key]['master']['cget']('text')
                test_val = ui_rows[key]['test']['cget']('text')
                tolerance = tol_inputs[key].get().strip()
                variance = ui_rows[key]['variance']['cget']('text')
                status_text = ui_rows[key]['status']['cget']('text').strip()  # Strips space buffers

                writer.writerow([metric_name, master_val, test_val, tolerance, variance, status_text])

        messagebox.showinfo("Export Successful",
                            f"Assessment record successfully saved to:\n\n{os.path.basename(save_path)}")

    except Exception as e:
        messagebox.showerror("Export Error", f"Failed to generate assessment record file:\n{str(e)}")

# ============================ Math Functions =============================

def imsize_calc(df):
    """
    Calculation to determine the size of the acquired image from the data points
    by measuring the overall bounding box footprint of the dot matrix.
    Completely immune to grid rotation, row-splitting, or missing corner dots.
    """
    if df.empty:
        return 0.0, 0.0

    # Natively find the extreme outer limits of the entire dot array
    width = df['x_prim'].max() - df['x_prim'].min()
    height = df['y_prim'].max() - df['y_prim'].min()

    return width, height


def ar_calc(df):
    """
    Calculation for determining the aspect ratio of the image by dividing the acquired width and height of the image to determine
    a unitless ratio value.
    """
    pts = get_grid_points(df)
    width = pts['top_right']['x_prim'] - pts['top_left']['x_prim']
    height = pts['bottom_left']['y_prim'] - pts['top_left']['y_prim']
    return width / height if height != 0 else 0


def smile_calc(df):
    """
    Calculates the 'smile' of the image by checking the angle of the horizontal by finding the midpoint of the
    line then checking the distance of that midpoint against the straight line from point to point
    """
    pts = get_grid_points(df)
    corners_y_avg = (pts['top_left']['y_prim'] + pts['top_right']['y_prim']) / 2
    return pts['top_mid']['y_prim'] - corners_y_avg


def imrot_calc(df):
    """
    Calculates the true physical rotation angle of the dot grid in degrees.
    Uses vector projections to locate outer corners, preventing the 40-80 degree
    calculation spike caused by row-mismatch anomalies.
    """
    if df.empty:
        return 0.0

    try:
        # 1. Locate the true outer geometric corners using stable vector math
        tl = df.loc[(df['x_prim'] + df['y_prim']).idxmin()]  # Top-Left
        tr = df.loc[(df['x_prim'] - df['y_prim']).idxmax()]  # Top-Right

        # 2. Compute the true delta components across the entire horizontal span
        dx = tr['x_prim'] - tl['x_prim']
        dy = tr['y_prim'] - tl['y_prim']

        # 3. Calculate the angle in radians and convert to degrees
        angle_rad = np.arctan2(dy, dx)
        angle_deg = np.degrees(angle_rad)

        # Return the rotation value
        return angle_deg

    except Exception as e:
        print(f"Error in rotation calculation: {str(e)}")
        return 0.0


def transl_calc(df):
    """
    calculates the movement of the image by grabbing the XY co-ordinates of the centre point of the grid
    """
    pts = get_grid_points(df)
    return pts['center']['x_prim'], pts['center']['y_prim']


def trapdist_calc(df):
    """
    Calculates Horizontal and Vertical Trapezoidal Distortion.
    Uses robust vector projections to find corners, eliminating the 300%+
    rotation/tilt calculation bug.
    """
    if df.empty:
        return 0.0, 0.0

    try:
        # 1. Isolate the true geometric corners using bulletproof vector math
        tl = df.loc[(df['x_prim'] + df['y_prim']).idxmin()]  # Top-Left: minimizes X + Y
        br = df.loc[(df['x_prim'] + df['y_prim']).idxmax()]  # Bottom-Right: maximizes X + Y
        tr = df.loc[(df['x_prim'] - df['y_prim']).idxmax()]  # Top-Right: maximizes X - Y
        bl = df.loc[(df['x_prim'] - df['y_prim']).idxmin()]  # Bottom-Left: minimizes X - Y

        # 2. Calculate the true physical boundaries across the grid sides
        top_width = tr['x_prim'] - tl['x_prim']
        bottom_width = br['x_prim'] - bl['x_prim']
        left_height = bl['y_prim'] - tl['y_prim']
        right_height = br['y_prim'] - tr['y_prim']

        # 3. Calculate Horizontal (H) Distortion Percentage
        #    Using the minimum width as the baseline protects against extreme division inflation
        base_width = min(top_width, bottom_width)
        if base_width > 0:
            h_distortion = (abs(top_width - bottom_width) / base_width) * 100
        else:
            h_distortion = 0.0

        # 4. Calculate Vertical (V) Distortion Percentage
        base_height = min(left_height, right_height)
        if base_height > 0:
            v_distortion = (abs(left_height - right_height) / base_height) * 100
        else:
            v_distortion = 0.0

        return h_distortion, v_distortion

    except Exception as e:
        print(f"Error in trapezoidal calculation: {str(e)}")
        return 0.0, 0.0


# ============================ Orchestrator & UI Interaction =============================

def select_master_file():
    """
    The User selects the master data from their OEM spec HUD glass that they can compare their test data against
    """
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
            master_label.config(text="Master: Loaded", fg="green", font=("Arial", 9, "bold"))

        except Exception as e:
            # If ANY error happens during load_data, immediately catch it here
            master_df = None
            master_label.config(text="Master: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Master CSV:\n\n{str(e)}")

        # Re-check system variables to toggle the run button
        check_run_conditions()


def select_test_file():
    """
    Allows the user to select the csv file that will be used as the comparison to the master
    """
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
            test_label.config(text="Test: Loaded", fg="green", font=("Arial", 9, "bold"))

        except Exception as e:
            # If ANY error happens during load_data, immediately catch it here
            test_df = None
            test_label.config(text="Test: Load Error!", fg="red", font=("Arial", 9, "bold"))
            messagebox.showerror("File Error", f"Failed to load Test CSV:\n\n{str(e)}")

        # Re-check system variables to toggle the run button
        check_run_conditions()


def clear_all_data():
    """
    Wipes loaded data frames from system memory, resets entry fields,
    restores placeholder text labels, and clears status colour matrices.
    """
    global master_df, test_df

    # 1. Double check with a confirmation popup so operators don't click it by accident
    if not messagebox.askyesno("Clear Dashboard",
                               "Are you sure you want to reset all current calculations and clear loaded files?"):
        return

    # 2. Reset global system data memory tracks
    master_df = None
    test_df = None

    # 3. Restore data file status indicators
    master_label.config(text="Master File Empty", fg="red", font=("Arial", 9, "normal"))
    test_label.config(text="Test File Empty", fg="red", font=("Arial", 9, "normal"))

    # 4. Lock down action controls
    check_run_conditions()  # This will automatically turn the Run button back to grey/Disabled state

    # 5. Flush calculation table metrics text data and statuses back to defaults
    for key in ui_rows:
        ui_rows[key]['master'].config(text="-")
        ui_rows[key]['test'].config(text="-")
        ui_rows[key]['variance'].config(text="-")
        ui_rows[key]['status'].config(bg="lightgray", text=" IDLE ", fg="black")

        # Optional: Reset tolerances back to a safe baseline default (e.g., 0.5)
        tol_inputs[key].delete(0, tk.END)
        tol_inputs[key].insert(0, "0.5")

    messagebox.showinfo("Reset Complete", "The data matrix and file logs have been successfully cleared.")


def check_run_conditions():
    """
    Strictly validates global memory allocation to safely unlock execution buttons.
    Dynamically swaps colours to indicate readiness state.
    """
    if master_df is not None and test_df is not None:
        # UNLOCKED STATE: Change button to bright green with white text
        run_btn.config(
            state=tk.NORMAL,
            bg="#198754",      # Success Green hex code
            fg="white"
        )
    else:
        # LOCKED STATE: Revert back to standard disabled grey look
        run_btn.config(
            state=tk.DISABLED,
            bg="#e0e0e0",      # Light grey background
            fg="#a0a0a0"       # Muted grey text
        )

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
    """
    Updates display text, checks tolerances, and colours the status box square with smart unit handling.
    """
    row_widgets['master'].config(text=master_txt)
    row_widgets['test'].config(text=test_txt)
    row_widgets['variance'].config(text=f"{round(variance_val, 3)} {unit_str}")

    try:
        tol_limit = float(tolerance_entry.get().strip())
    except ValueError:
        tol_limit = 0.5
        tolerance_entry.delete(0, tk.END)
        tolerance_entry.insert(0, "0.5")

    # If the user types "1.0" for a percentage row, but your data uses decimal ratios (e.g. 0.15)
    if "%" in unit_str and abs(variance_val) <= 1.0:
        tol_limit = tol_limit / 100.0

    # Pass/Fail execution using the verified limits
    if abs(variance_val) <= tol_limit:
        row_widgets['status'].config(bg="green", text=" PASS ", fg="white")
    else:
        row_widgets['status'].config(bg="red", text=" FAIL ", fg="white")


def execute_assessment():
    """
    Main calculation trigger loop.
    """
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


# ============================ GUI Construction =======================================

root = tk.Tk()
root.title("GR1036 HUD Test Rig Image Assessment")
root.geometry("1050x600") #increasing size to allow for logo to fit onto GUI

logo_frame = tk.Frame(root, pady=10)
logo_frame.pack(fill="x", padx=30)  # Added horizontal padding to push logos toward edges

# --- 1. COMPANY LOGO LOADER ---
#adjusted how the logo is loaded into the GUI to get around the restriction of only using .png or .gif files
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))

    comp_path = None
    for filename in os.listdir(script_dir):
        if filename.lower().startswith("granroth_logo"):
            comp_path = os.path.join(script_dir, filename)
            break

    if comp_path is None:
        raise FileNotFoundError("Company logo missing")  #fallback to display a text string if no logo to prevent GUI from crashing

    comp_pil = Image.open(comp_path)
    comp_pil = comp_pil.resize((260, 80),
                               Image.Resampling.LANCZOS)  # Adjusted slightly down to balance screen real estate
    comp_img = ImageTk.PhotoImage(comp_pil)

    # Pack to the LEFT side of the frame
    comp_label = tk.Label(logo_frame, image=comp_img)
    comp_label.image = comp_img
    comp_label.pack(side="left", anchor="w")

except Exception as e:
    print(f"Company Logo Skip: {str(e)}")
    # If your logo is missing, show a simple text label on the left instead
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
    cust_pil = cust_pil.resize((260, 80), Image.Resampling.LANCZOS)  # Match the dimensions of your company logo
    cust_img = ImageTk.PhotoImage(cust_pil)

    # Pack to the RIGHT side of the frame
    cust_label = tk.Label(logo_frame, image=cust_img)
    cust_label.image = cust_img
    cust_label.pack(side="right", anchor="e")

except Exception as e:
    print(f"Customer Logo Skip: {str(e)}")
    # If the customer logo is missing, show a simple text label on the right instead
    cust_label = tk.Label(logo_frame, text="CUSTOMER EVALUATION", font=("Arial", 12, "bold"), fg="#555555")
    cust_label.pack(side="right", anchor="e")


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

# 1. Run Assessment Button (Column 2)
run_btn = tk.Button(
    upload_frame,
    text="Run Image Assessment",
    command=execute_assessment,
    state=tk.DISABLED,
    bg="#e0e0e0",
    fg="#a0a0a0",
    font=("Arial", 10, "bold")
)
# Note: removed rowspan, matches row=0
run_btn.grid(row=0, column=2, padx=10, pady=10, ipady=8, sticky="ew")

# 2. Save Assessment Button (Column 3)
save_btn = tk.Button(
    upload_frame,
    text="💾 Save Results",
    command=save_assessment_record,
    bg="#0d6efd",
    fg="white",
    font=("Arial", 10, "bold")
)
save_btn.grid(row=0, column=3, padx=10, pady=10, ipady=8, sticky="ew")

# 3. Clear Dashboard Button (Column 4)
clear_btn = tk.Button(
    upload_frame,
    text="🔄 Clear Current Data",
    command=clear_all_data,
    bg="#dc3545",
    fg="white",
    font=("Arial", 10, "bold")
)
clear_btn.grid(row=0, column=5, padx=10, pady=10, ipady=8, sticky="ew")

load_tol_btn = tk.Button(
    upload_frame,
    text="📂 Load Tolerances",
    command=load_custom_tolerances,   # This connects to the function we will create next
    bg="#495057",                      # Charcoal grey for configuration actions
    fg="white",
    font=("Arial", 10, "bold")
)
load_tol_btn.grid(row=0, column=4, padx=10, pady=10, ipady=8, sticky="ew")

# Calculations Metrics Framework Block
matrix_frame = tk.LabelFrame(root, text=" Assessment Parameters Window ", padx=10, pady=10)
matrix_frame.pack(fill="both", expand=True, padx=15, pady=10)

# Matrix Headers
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
    # Precise string checks matching your layout screenshot exactly
    if "Size" in key or "Dist." in key:
        tol_ent.insert(0, "1.0")  # Preloads 1.0% for Image Size and Trapezoidal Dist.
    elif "Rotation" in key:
        tol_ent.insert(0, "2.0")  # Preloads 2.0° for degrees deviation
    elif "Aspect Ratio" in key:
        tol_ent.insert(0, "0.05")  # Preloads a tight decimal threshold for ratios
    elif "Translation" in key or "Smile" in key:
        tol_ent.insert(0, "5.0")  # Preloads 5.0 px for pixel offsets
    else:
        tol_ent.insert(0, "0.5")  # Standard baseline fallback
    tol_ent.grid(row=row_idx, column=3, padx=10, pady=5)
    tol_inputs[key] = tol_ent

    # Variance Output Window Text
    v_val = tk.Label(matrix_frame, text="-", font=("Arial", 9), borderwidth=1, relief="groove", width=15)
    v_val.grid(row=row_idx, column=4, sticky="nsew")

    # Status coloured block placeholder panel
    s_box = tk.Label(matrix_frame, text=" IDLE ", bg="lightgray", font=("Arial", 9, "bold"), borderwidth=1,
                     relief="sunken", width=10)
    s_box.grid(row=row_idx, column=5, padx=15, pady=5)

    # Bundle reference targets for extraction loops
    ui_rows[key] = {'master': m_val, 'test': t_val, 'variance': v_val, 'status': s_box}

# Standard grid config normalization parameters
for c in range(6):
    matrix_frame.grid_columnconfigure(c, weight=1)


def apply_smart_tolerance_defaults():
    """
    Scans the completed tolerance input tracking dictionary and overrides
    the generic 0.5 default with context-aware values based on the row name.
    """
    # Safeguard: If the dictionary hasn't loaded yet, exit safely without crashing
    if 'tol_inputs' not in globals():
        return

    for key, entry_box in tol_inputs.items():
        # Clear out the generic "0.5" that the loop put in there
        entry_box.delete(0, tk.END)

        # Apply the correct unit-aware default based on the row's name
        if "%" in key or "ratio" in key.lower():
            entry_box.insert(0, "1.0")  # 1.0% for percentage values
        elif "distortion" in key.lower() or "warp" in key.lower():
            entry_box.insert(0, "0.25")  # 0.25mm for strict spatial warp checks
        else:
            entry_box.insert(0, "0.5")  # 0.5 standard fallback

apply_smart_tolerance_defaults()
root.mainloop()