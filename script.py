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
    #Take two outer points and create a straight line, find the mid-point, then find the perpendicular distance between the midpoint and its closet other point
    #Will need to repeat this for both horizontal smiles
    return "N/A"

def imrot_calc(df):
    # choose the center point of the image and one other outer point of the image, then check the angle of inclination/declination between the two points
    #return as degrees
    return "0.0°"

def transl_calc(df):
    # Will be taking note of the X-Y co-ordinates of the center point of the image
    #return the XY co-ordinates of the center point
    #When comparing to master we will look to see how different they are
    return "0.0"

def trapdist_calc(df):
    #
    return "0.0%"

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