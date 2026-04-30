""""
GR1036 HUD TEST RIG IMAGE ASSESSMENT Program GUI
"""

import pandas as pd
import scipy as sp
import matplotlib.pyplot as plt
import tkinter as tk
from tkinter import filedialog
import numpy as np
import os


#============================ Functions ================================
def select_file_():
    root = tk.Tk()
    root.withdraw()  # Hide the tiny tkinter window
    root.attributes("-topmost", True)  # Bring the file selector to the front

    print("Select the CSV file...")
    file_path = filedialog.askopenfilename(
        title="Select CSV Data File",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )
    root.destroy()
    return file_path


root = tk.Tk()
# Widgets are added here


button = tk.Button(root,text="Call function", command=select_file_)
button.pack()
root.mainloop()