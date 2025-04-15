import numpy as np, matplotlib.pyplot as plt, pandas as pd
import subprocess, os, shutil, warnings, csv, json
from skopt import gp_minimize, Optimizer
from skopt.space import Real
from skopt.utils import dump
import multiprocessing as mp
from mpl_toolkits.axes_grid1 import make_axes_locatable

from Circuit_Properties import *
from Stats import *
from Functions import *
from HexProperties import *
from rstools import RSoftUserFunction, RSoftCircuit

with open("variable_paras.json", "r") as f:
    dat = json.load(f)
    name = dat["Name"]
    background_index = dat["background_index"]

with open("prior_space.json", "r") as read:
    param_range = json.load(read)

para_space = [Real(low, high, name=prior_name) for prior_name, (low, high) in param_range.items()]

def RunRsoft(params): 
    param_dict = {dim.name: val for dim, val in zip(para_space, params)}

    '''
    Manual setup to loop through a list of values
    Runs the terminal line that will initiate RSoft. 
    All output files will appear in a subfolder on the Desktop (windows)
    '''
    name_tag = "_".join(f"{key}_{val:.4f}" for key, val in param_dict.items())

    filename = f"MCF_{name_tag}.ind"
    prefix   = f"prefix={name_tag}"
    folder   = f"Sim_{name_tag}"

    # Read original template
    with open(f"{name}.ind", "r") as r:
        lines = r.readlines()

    # Construct symbolic delta expression
    delta_expr = param_dict["Core_index"] - background_index

    # Build the updated lines
    modified_lines = []
    for line in lines:
        line_strip = line.strip()
        replaced = False

        # Insert delta after segment start
        if line_strip.startswith("segment"):
            modified_lines.append(line)
            modified_lines.append(f"\tbegin.delta = {delta_expr}\n")
            modified_lines.append(f"\tend.delta = {delta_expr}\n")
            continue

        # Skip replacing Core_index directly (if already covered elsewhere)
        for param, val in param_dict.items():
            if param == "Core_index":
                continue
            if line_strip.startswith(f"{param} ="):
                modified_lines.append(f"{param} = {val:.4f}\n")
                replaced = True
                break

        if not replaced:
            modified_lines.append(line)

    # Write the final .ind file with symbolic delta expression
    with open(filename, "w") as out:
        out.writelines(modified_lines)
    
    # Set up folders
    user_home = os.path.expanduser("~")
    desktop_path = os.path.join(user_home, "Desktop")
    results_root = os.path.join(desktop_path, "Results")
    results_folder = os.path.join(results_root, folder)
    os.makedirs(results_folder, exist_ok=True)

    # Run simulation, if multiprocessing takes too long time it out and raise error
    try:
        subprocess.run(["bsimw32", filename, prefix, "wait=0"], check=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("RSoft timed out")

    # Read .mon file
    uf = RSoftUserFunction()
    uf.read(f"{name_tag}.mon")
    x, y = uf.get_arrays()

    # Save results
    results = {0: y}  # just one run per call
    csv_tag = f"Throughput_{name_tag}"
    with open(csv_tag, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["x", name_tag])
        for i in range(len(x)):
            writer.writerow([x[i], y[i]])

    # Move files to results folder
    for file in os.listdir():
        if file.startswith(name_tag) or file == filename:
            shutil.move(file, os.path.join(results_folder, file))

    df = pd.read_csv(csv_tag)
    average = df[name_tag].mean()
    return -average

# initialises multiprocessing function that runs the RSoft simulation
def mp_eval(params):
    # just for tracking purposes if running from the terminal
    print(f"[PID {os.getpid()}] Starting RunRsoft with {params}")
    return RunRsoft(params, skip=False)

# backend of skopt.gp_minimize that can handle multiprocessing
opt = Optimizer(
    dimensions = para_space,
    base_estimator = "GP",
    acq_func = "EI",
    random_state=42
)

# the multiprocessing in all its glory. The first if __name__ == "__main__" is required 
if __name__ == "__main__":    
    # how many values in each parameter space to run simulation with
    total_calls = 100
    # this is the number of points to sample simultaneously. Increase to cycle through prior space quicker at the cost of CPU computation
    batch_size = 5
    all_results = []

    for i in range(0, total_calls, batch_size):
        # print(f"\n Batch {i//batch_size + 1}")
        
        # Suggest next batch of points
        param_batch = opt.ask(batch_size)

        # Evaluate in parallel
        with mp.Pool(batch_size) as pool:
            result_batch = pool.map(mp_eval, param_batch)

        # Feed results back to optimizer
        opt.tell(param_batch, result_batch)
        
        all_results.extend(zip(param_batch, result_batch))
        # save results for plotting/analysis
        dump(opt, "rsoft_opt_checkpoint.pkl", store_objective=False)

        # Unpack results
        x_iters = [r[0] for r in all_results]  # parameter sets
        y_vals = [-r[1] for r in all_results]  # throughput values

        c_num = np.arange(len(x_iters))  # iteration counter
        param_names = [dim.name for dim in opt.space.dimensions]
        n_params = len(param_names)

        # Find best result
        best_idx = np.argmax(y_vals)
        best_params = x_iters[best_idx]
        best_throughput = y_vals[best_idx]

        # Create plots
        fig, axes = plt.subplots(n_params, 1, figsize=(8, 4.5 + 1.5 * n_params), sharex=False)

        if n_params == 1:
            axes = [axes]

        for k, (param_name, ax) in enumerate(zip(param_names, axes)):
            x_vals = [x[k] for x in x_iters]

            scatter = ax.scatter(x_vals, y_vals, c=c_num, cmap='viridis_r', s=60, edgecolor='k', label="Evaluations")
            ax.scatter(best_params[k], best_throughput, c='red', s=100, label="Best", zorder=3, edgecolor='black')
            
            ax.set_ylabel("Throughput", fontsize=12)
            ax.set_xlabel(param_name, fontsize=12)
            ax.set_title(f"{param_name} vs Throughput", fontsize=14)
            ax.grid(True, linestyle='--', alpha=0.5)
            ax.legend()

            # Add colorbar
            divider = make_axes_locatable(ax)
            cax = divider.append_axes("right", size="4%", pad=0.05)
            cbar = plt.colorbar(scatter, cax=cax)
            cbar.set_label("Iteration")

        plt.tight_layout()
        plt.savefig(f"plot_iteration_{i//batch_size + 1}.png", dpi=300)

        with open("best_params_log.csv", "a", newline="") as log:
            writer = csv.writer(log)
            writer.writerow(["Batch Iteration"] + [prior_name for prior_name, (_,_) in param_range.items()] + ["Throughput"])
            # organised as follows: batch #| best_param_value | Throughput
            writer.writerow([i // batch_size + 1] + list(best_params) + [best_throughput])