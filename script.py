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
import os
import scipy as sp
import matplotlib.pyplot as plt

#============================ Function Definitions ================================

def select_file():
    #Opens file dialog and triggers the calculation chain.
    file_path = filedialog.askopenfilename(
        title="Select CSV Data File",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )
    
    if file_path:
        # Instead of returning to a global variable, we trigger the master process
        master_calc(file_path)
    else:
        print("No file selected.")

def load_data(file_path):
    #Reads and cleans the CSV data.
    try:
        # Reading with your specific separator
        df = pd.read_csv(file_path, sep=';')
        
        # Rename columns for easier internal handling
        df = df.rename(columns={
            'Find Primary Centre:Center.X Position (Pixel) - Check for Part and Inspect': 'x_prim',
            'Find Primary Centre:Center.Y Position (Pixel) - Check for Part and Inspect': 'y_prim',
            'Find Ghost Centre:Center.X Position (Pixel) - Check for Part and Inspect': 'x_ghost',
            'Find Ghost Centre:Center.Y Position (Pixel) - Check for Part and Inspect': 'y_ghost'
        })

        # Data cleaning: Convert commas to dots and cast to float
        for col in ['x_prim', 'y_prim', 'x_ghost', 'y_ghost']:
            if col in df.columns:
                df[col] = df[col].astype(str).str.replace(',', '.').astype(float)
        
        return df
    except Exception as e:
        messagebox.showerror("Error", f"Failed to load CSV: {e}")
        return None


def get_grid_points(df):
    """
    Takes a jumbled CSV of 77 points and organizes them into an 11x7 grid.
    """
    # 1. Sort by Y-coordinate first to find rows
    # We round the Y values slightly so that dots in the same 'line'
    # but with minor tilt are treated as being in the same row.
    df['row_group'] = (df['y_prim'] / 10).round()

    # 2. Sort by the row group, then by X to get left-to-right order
    grid = df.sort_values(by=['row_group', 'x_prim']).reset_index(drop=True)

    # Now 'grid' is ordered:
    # Index 0-10   = Top Row (Left to Right)
    # Index 11-21  = Second Row ...
    # Index 66-76  = Bottom Row

    points = {
        "top_left": grid.iloc[0],  # First point found
        "top_mid": grid.iloc[5],  # 6th point in first row
        "top_right": grid.iloc[10],  # 11th point in first row

        "center": grid.iloc[38],  # Exact middle of 77 points

        "bottom_left": grid.iloc[66],  # 1st point in last row
        "bottom_mid": grid.iloc[71],  # 6th point in last row
        "bottom_right": grid.iloc[76]  # 11th point in last row
    }
    return points




def master_calc(file_path):
    #The 'Upper' function that orchestrates the workflow.
    # 1. Load
    df = load_data(file_path)
    if df is None:
        return

    # 2. Calculate
    results = run_all_calculations(df)

    # 3. Output (For now, printing to console and showing a popup)
    print("\n--- Assessment Results ---")
    output_string = ""
    for key, value in results.items():
        line = f"{key.replace('_', ' ').title()}: {value}\n"
        output_string += line
        print(line.strip())
    
    messagebox.showinfo("Calculations Complete", output_string)
    
    # 4. Save (Optional - requires filename logic)
    # save_file(file_path, results)

def run_all_calculations(df):
    #Calls each specific calculation function and returns a dictionary of results.
    # Each function receives the cleaned dataframe 'df'
    results = {'image_size': imsize_calc(df), 'aspect_ratio': ar_calc(df), 'smile': smile_calc(df),
               'rotation': imrot_calc(df), 'translation': transl_calc(df), 'trap_dist': trapdist_calc(df)}

    return results

#============================ Specific Math Functions =============================

def imsize_calc(df):
    # Logic: Range of X and Y primary coordinates
    #We will be using the 4 corner Co-ords of the image to calculate the distances then take the sum to get the permimeter
    #return as decimal value, as we will be comparing it to master then expressing as percentage difference
    width = df['x_prim'].max() - df['x_prim'].min()
    height = df['y_prim'].max() - df['y_prim'].min()
    return f"{round(width, 2)}x{round(height, 2)} px"

def ar_calc(df):
    #Logic: Range of X and Y primary coordinates
    #We will use part of the previous img size, we need to the height and width using 3 corner values
    #Then we use those two new variables to calculate the aspect ratio using width/height.
    # return as a decimal
    width = df['x_prim'].max() - df['x_prim'].min()
    height = df['y_prim'].max() - df['y_prim'].min()
    return round(width / height, 3) if height != 0 else 0


def smile_calc(df):
    pts = get_grid_points(df)

    # Average Y of the corners
    corners_y_avg = (pts['top_left']['y_prim'] + pts['top_right']['y_prim']) / 2

    # Deviation of the middle dot from that average
    smile_val = pts['top_mid']['y_prim'] - corners_y_avg
    return f"{round(smile_val, 3)} px"

def imrot_calc(df):
    # choose the centre point of the image and one other outer point of the image, then check the angle of inclination/declination between the two points
    #return as degrees
    return "0.0°"

def transl_calc(df):
    # Will be taking note of the X-Y co-ordinates of the centre point of the image
    #return the XY co-ordinates of the centre point
    #When comparing to master we will look to see how different they are
    return "0.0"


def trapdist_calc(df):
    """
    Calculates Trapezoidal Distortion by comparing edges as pairs.
    Returns a string with both Horizontal and Vertical results for full image keystone analysis
    """
    # 1. Get the sorted grid
    # (Assuming you use the sorting method to ensure iloc 0, 10, 66, 76 are the corners)
    grid = df.sort_values(by=['y_prim', 'x_prim']).reset_index(drop=True)

    # Identify Corners
    tl = grid.iloc[0]  # Top Left
    tr = grid.iloc[10]  # Top Right
    bl = grid.iloc[66]  # Bottom Left
    br = grid.iloc[76]  # Bottom Right

    # --- Horizontal Pair (Widths) ---
    top_width = tr['x_prim'] - tl['x_prim']
    bottom_width = br['x_prim'] - bl['x_prim']
    # Difference as a percentage of the top width
    h_trap = ((top_width - bottom_width) / top_width) * 100

    # --- Vertical Pair (Heights) ---
    left_height = bl['y_prim'] - tl['y_prim']
    right_height = br['y_prim'] - tr['y_prim']
    # Difference as a percentage of the left height
    v_trap = ((left_height - right_height) / left_height) * 100

    return f"H: {round(h_trap, 2)}% | V: {round(v_trap, 2)}%"

#============================ GUI Framework =======================================

root = tk.Tk()
root.title("GR1036 HUD Test Rig")
root.geometry("300x150")

# Instruction Label
label = tk.Label(root, text="HUD Image Assessment", font=("Arial", 10, "bold"))
label.pack(pady=10)

# Import Button
import_btn = tk.Button(
    root, 
    text="Import Test Data", 
    command=select_file, 
    bg="#e1e1e1", 
    width=20
)
import_btn.pack(pady=10)

root.mainloop()