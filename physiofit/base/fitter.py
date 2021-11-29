"""
PhysioFit software main module
"""
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from pandas import read_csv, DataFrame
from scipy.optimize import minimize


# from typing import Union


class PhysioFitter:

    def __init__(self, path_to_data, vini=0.04, mc=True, iterations=50, pos=True, up_flux_bound=50, low_flux_bound=-50,
                 up_conc_bound=50, low_conc_bound=1.e-6, weight=None, sd_X=0.002, sd_M=0.5, save=True,
                 summary=False):

        """
        The PhysioFitter class is responsible for most of Physiofit's heavy lifting. Features included are:
            * loading of data from csv or tsv file
            * equation system initialization using the following analytical functions (in absence of lag and
              degradation:
                X(t) = X0 * exp(mu * t)
                Mi(t) = qMi * (X0 / mu) * (exp(mu * t) - 1) + Mi0
            * simulation of data points from given initial parameters
            * cost calculation using the equation:
                residuum = sum((sim - meas) / weight)²
            * optimization of the initial parameters using scipy.optimize.minimize ('L-BFGS-B' method)
            * calling the Stat Analyzer objet for sensitivity analysis, khi2 tests and plotting (see documentation
              relative to the component Stat Analyzer class for more details)

        :param path_to_data: path to input file
        :param mc: Should Monte-Carlo sensitivity analysis be performed (default=True)
        :param vini: initial value for fluxes and concentrations (default=1)
        :type vini: int or float
        :type mc: Boolean
        :param iterations: number of iterations for Monte-Carlo simulation (default=50)
        :type iterations: int
        :param pos: Negative concentrations of noisy datasets generated during Monte-Carlo iterations are set to 0 if
                    True (default=True)
        :type pos: Boolean
        :param up_flux_bound: Upper constraints on initial fluxes (default = 50)
        :type up_flux_bound: int or float
        :param low_flux_bound: Lower constraints on initial fluxes (default = -50)
        :type low_flux_bound: int or float
        :param up_conc_bound: Upper constraints on initial concentrations (default = 50)
        :type up_conc_bound: int or float
        :param low_conc_bound: Lower contratints on initial concentrations (default = 1e-6)
        :type low_conc_bound: in or float
        :param weight: weight matrix used for residuum calculations. Can be:
                       * a matrix with the same dimensions as the measurements matrix (but without the time column)
                       * a named vector containing weights for all the metabolites provided in the input file
                       * 0 (by default), in which case the matrix is automatically loaded from the file xxx_sd.csv/.tsv
                       (where xxx is the data file name) if the file exists. Otherwise, weight is constructed from sd_X
                       and sd_M arguments
        :type weight: int, float, list or ndarray
        :param sd_X: Standard deviation on biomass concentrations (default = 0.002), used only if weight = 0
        :type sd_X: int or flaot
        :param sd_M: Standard deviation on metabolite concentrations (defaul = 0.5), used only if weight = 0
        :type sd_M: int or float
        :param save: Should results be saved
        :type save: Boolean
        :param summary: Should results of the khi-2 test be displayed
        :type summary: Boolean
        """

        self.data = PhysioFitter._read_data(path_to_data)
        self.data = self.data.sort_values("time", ignore_index=True)
        self.vini = vini
        self.mc = mc
        self.iterations = iterations
        self.pos = pos
        self.up_flux_bound = up_flux_bound
        self.low_flux_bound = low_flux_bound
        self.up_conc_bound = up_conc_bound
        self.low_conc_bound = low_conc_bound
        self.weight = weight
        self.sd_X = sd_X
        self.sd_M = sd_M
        self.save = save
        self.summary = summary

        self.simulated_matrix = None
        self.optimize_results = None
        self.params = None
        self.ids = None
        self.bounds = None

        self._initialize_vectors()
        self._initialize_weight_matrix()
        self._initialize_bounds()

    @staticmethod
    def _read_data(path_to_data: str) -> DataFrame:
        """
        Read initial data file (csv or tsv)

        :param path_to_data: str containing the relative or absolute path to the data
        :return: pandas DataFrame containing the data
        """

        data_path = Path(path_to_data).resolve()
        if data_path.suffix == ".tsv":
            data = read_csv(data_path, sep="\t")
        elif data_path.suffix == ".csv":
            data = read_csv(data_path, sep=";")
        else:
            if not data_path.exists():
                raise ValueError(f"{data_path} is not a valid file")
            else:
                raise TypeError(f"{data_path} is not a valid format. Accepted formats are .csv or .tsv")
        PhysioFitter._verify_data(data)
        return data

    @staticmethod
    def _verify_data(data: DataFrame):
        """
        Perform checks on DataFrame returned by the _read_data function

        :param data: pandas DataFrame containing the data
        :return: None
        """

        if not isinstance(data, DataFrame):
            raise TypeError("There was an error reading the data: DataFrame has not been generated")
        for col in ["time", "X"]:
            if col not in data.columns:
                raise ValueError(f"The column {col} is missing from the dataset")
        if len(data.columns) <= 2:
            raise ValueError(f"The data does not contain any metabolite columns")
        for col in data.columns:
            if data[col].dtypes != np.int64 and data[col].dtypes != np.float64:
                raise ValueError(f"The column {col} has values that are not of numeric type")

    def _initialize_vectors(self):
        """
        Initialize the vectors needed for flux calculations from the input parameters

        :return: None
        """

        self.time_vector = self.data.time.to_numpy()
        self.name_vector = self.data.drop("time", axis=1).columns.to_list()
        self.exp_data_matrix = self.data.drop("time", axis=1).to_numpy()
        metabolites = self.name_vector[1:]
        mu = self.vini
        x_0 = self.vini
        self.params = [x_0, mu]
        self.ids = ["X_0", "mu"]
        for met in metabolites:
            self.params.append(self.vini)
            self.params.append(self.vini)
            self.ids.append(f"{met}_q")
            self.ids.append(f"{met}_M0")

    def _initialize_weight_matrix(self):
        """
        Initialize the weight matrix from different types of inputs: single value, vector or matrix.

        :return: None
        """

        # TODO: This function can be optimized, if the input is a matrix we should detect it directly

        # When 0 is given as input weight, we assume the weights are given in an external file
        if self.weight is None:
            self._read_weight_file()
            return
        # When weight is a single value, we build a weight matrix containing the value in all positions
        if isinstance(self.weight, int) or isinstance(self.weight, float):
            self._build_weight_matrix()
            return
        if not isinstance(self.weight, np.ndarray):
            if not isinstance(self.weight, list):
                raise TypeError(f"Cannot coerce weights to array. Please check that a list or array is given as input."
                                f"\nCurrent input: \n{self.weight}")
            else:
                self.weight = np.array(self.weight)
        if not np.issubdtype(self.weight.dtype, np.number):
            try:
                self.weight = self.weight.astype(float)
            except ValueError:
                raise ValueError(f"The weight vector/matrix contains values that are not numbers. \n"
                                 f"Current weight vector/matrix: \n{self.weight}")
            except Exception as e:
                raise RuntimeError(f"Unknown error: {e}")
        else:
            # If the array is not the right shape, we assume it is a vector that needs to be tiled into a matrix
            if self.weight.shape != self.exp_data_matrix.shape:
                try:
                    self._build_weight_matrix()
                except ValueError:
                    raise
                except RuntimeError:
                    raise
            else:
                return

    def _initialize_bounds(self):

        # We set the bounds for x0 and mu
        bounds = [
            (self.low_conc_bound, self.up_conc_bound),  # X_0
            (1e-6, self.up_flux_bound)  # mu
        ]
        # We get the number of times that we must add the m0 and q0 bounds (once per metabolite)
        ids_range = int((len(self.ids) - 2) / 2)  # We force int so that Python does not think it could be float
        for _ in range(ids_range):
            bounds.append(
                (self.low_flux_bound, self.up_flux_bound)  # q_0
            )
            bounds.append(
                (self.low_conc_bound, self.up_conc_bound)  # M_0
            )
        self.bounds = tuple(bounds)

    def _read_weight_file(self):
        pass

    def _build_weight_matrix(self):
        """
        Build the weight matrix from different input types

        :return: None
        """

        if isinstance(self.weight, np.ndarray):
            if self.weight.size != self.exp_data_matrix[0].size:
                raise ValueError("Weight vector not of right size")
            else:
                self.weight = np.tile(self.weight, (len(self.exp_data_matrix), 1))
        elif isinstance(self.weight, int) or isinstance(self.weight, float):
            self.weight = np.full(self.exp_data_matrix.shape, self.weight)
        else:
            raise RuntimeError("Unknown error")

    def simulate(self, equation_type="simple"):

        if equation_type == "simple":
            self.simulated_matrix = PhysioFitter._simple_sim(self.params, self.exp_data_matrix, self.time_vector)
        else:
            pass

    def optimize(self):

        self.optimize_results = PhysioFitter._run_optimization(self.params, self.exp_data_matrix,
                                                               self.time_vector, self.weight, self.bounds)
        self.final_simulated_matrix = PhysioFitter._simple_sim(self.optimize_results.x, self.exp_data_matrix,
                                                               self.time_vector)

    def test_plot(self):

        fig, (ax1, ax2, ax3) = plt.subplots(3)
        x = self.time_vector
        exp_biomass = self.exp_data_matrix[:, 0]
        exp_glc = self.exp_data_matrix[:, 1]
        exp_ace = self.exp_data_matrix[:, 2]
        sim_biomass = self.final_simulated_matrix[:, 0]
        sim_glc = self.final_simulated_matrix[:, 1]
        sim_ace = self.final_simulated_matrix[:, 2]

        # Work on biomass ax
        exp_biomass_line, = ax1.scatter(x, exp_biomass, marker='o')
        exp_biomass_line.set_label("Exp Biomass")
        sim_biomass_line, = ax1.plot(x, sim_biomass, linestyle='--', marker='o')
        sim_biomass_line.set_label("Sim Biomass")
        ax1.set(xlim=0, ylim=0, xlabel="Time", ylabel="Concentration")
        ax1.legend()
        ax1.set_title("Sim/Exp comparison")

        # Work on glucose axe
        exp_glucose_line, = ax2.scatter(x, exp_glc, marker='o')
        exp_glucose_line.set_label("Exp Glucose")
        exp_glucose_line, = ax2.plot(x, sim_glc,linestyle='--', marker='o')
        exp_glucose_line.set_label("Sim Glucose")
        ax2.set(xlim=0, ylim=0, xlabel="Time", ylabel="Concentration")
        ax2.legend()

        #Work on acetate axe
        exp_Ace_line, = ax3.scatter(x, exp_ace, marker='o')
        exp_Ace_line.set_label("Exp Acetate")
        exp_Ace_line, = ax3.plot(x, sim_ace, linestyle='--', marker='o')
        exp_Ace_line.set_label("Sim Acetate")
        ax3.set(xlim=0, ylim=0, xlabel="Time", ylabel="Concentration")
        ax3.legend()

        plt.show()

    @staticmethod
    def _simple_sim(params, exp_data_matrix, time_vector):

        simulated_matrix = np.empty_like(exp_data_matrix)
        x_0 = params[0]
        mu = params[1]
        exp_mu_t = np.exp(mu * time_vector)
        simulated_matrix[:, 0] = x_0 * exp_mu_t
        for i in range(1, int(len(params) / 2)):
            q = params[i * 2]
            m_0 = params[i * 2 + 1]
            simulated_matrix[:, i] = q * (x_0 / mu) * (exp_mu_t - 1) + m_0
        return simulated_matrix

    @staticmethod
    def _calculate_cost(params, exp_data_matrix, time_vector, weight_matrix):

        simulated_matrix = PhysioFitter._simple_sim(params, exp_data_matrix, time_vector)
        cost_val = np.square((simulated_matrix - exp_data_matrix) / weight_matrix)
        residuum = np.nansum(cost_val)
        return residuum

    @staticmethod
    def _run_optimization(params, exp_data_matrix, time_vector, weight_matrix, bounds):

        optimize_results = minimize(PhysioFitter._calculate_cost, x0=params, args=(
            exp_data_matrix, time_vector, weight_matrix), method="L-BFGS-B", bounds=bounds)
        return optimize_results


if __name__ == "__main__":
    test = PhysioFitter(r"C:\Users\legregam\Documents\Projets\PhysioFit\Example\KEIO_test_data\KEIO_ROBOT6_1.tsv",
                        vini=1, weight=[0.02, 0.46, 0.1])
    test.optimize()
    test.test_plot()

# TODO: Build plotting function for testing
