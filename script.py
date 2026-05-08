""""
GR1036 HUD TEST RIG IMAGE ASSESSMENT

Customer Order:
1) GUI Framework
2) Image Size
3) Aspect Ratio
4) Image Rotation
5) Translation
6) Trapezoidal Distortion
8) Smile
"""


import pandas as pd
import scipy as sp
import matplotlib.pyplot as plt
import tkinter as tk
from tkinter import filedialog
import numpy as np
import os
import time
from datetime import datetime
import cv2



#============================ Function Definitions ================================
def select_file_():
    root = tk.Tk()
    root.withdraw()  # Hide the tiny tkinter window
    root.attributes("-topmost", True)  # Bring the file selector to the front

    print("Select the CSV file...")
    file_path = filedialog.askopenfilename(
        title="Select CSV Data File",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )
    csv_path = select_file()

    if not csv_path:
        print("Error: No file selected. Exiting script.")
        exit()
    root.destroy()
    return file_path

def load_data():
    try:
        df = pd.read_csv(csv_path, sep=';')
    except Exception as e:
        print(f"Error reading CSV: {e}")  # error event to exit the script in the event of a failed read.
        exit()

    df = df.rename(columns={  # rename the columns to make things cleaner for us to work with.
        'Find Primary Centre:Center.X Position (Pixel) - Check for Part and Inspect': 'x_prim',
        'Find Primary Centre:Center.Y Position (Pixel) - Check for Part and Inspect': 'y_prim',
        # matches what the default naming for data points in VBAI csv export
        'Find Ghost Centre:Center.X Position (Pixel) - Check for Part and Inspect': 'x_ghost',
        # Check exported csv file naming convention and copy/paste it here to match
        'Find Ghost Centre:Center.Y Position (Pixel) - Check for Part and Inspect': 'y_ghost'
    })

    # Data cleaning: Convert commas to dots (Excel format) and cast to float for pandas to work with
    for col in ['x_prim', 'y_prim', 'x_ghost', 'y_ghost']:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace(',', '.').astype(float)

    # read and write the columns in the .csv file to variables for the pandas library to work with them
    x_prim = df['x_prim'].values
    y_prim = df['y_prim'].values
    x_ghost = df['x_ghost'].values
    y_ghost = df['y_ghost'].values
    return x_prim, y_prim, x_ghost, y_ghost



root = tk.Tk()
# Widgets are added here


button = tk.Button(root,text="Import test Data", command=select_file_)
button.pack()



root.mainloop()  #blocking code, anything after this won't run at all or properly