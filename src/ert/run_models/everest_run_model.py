from __future__ import annotations

import datetime
import functools
import json
import logging
import os
import queue
import shutil
from collections import defaultdict
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from numpy import float64
from numpy._typing import NDArray
from ropt.enums import EventType, OptimizerExitCode
from ropt.evaluator import EvaluatorContext, EvaluatorResult
from ropt.plan import BasicOptimizer
from ropt.plan import Event as OptimizerEvent
from ropt.transforms import OptModelTransforms
from typing_extensions import TypedDict

from _ert.events import EESnapshot, EESnapshotUpdate, Event
from ert.config import ErtConfig, ExtParamConfig
from ert.ensemble_evaluator import EnsembleSnapshot, EvaluatorServerConfig
from ert.runpaths import Runpaths
from ert.storage import open_storage
from everest.config import ControlConfig, ControlVariableGuessListConfig, EverestConfig
from everest.config.utils import FlattenedControls
from everest.everest_storage import EverestStorage, OptimalResult
from everest.optimizer.everest2ropt import everest2ropt
from everest.optimizer.opt_model_transforms import (
    ObjectiveScaler,
    get_optimization_domain_transforms,
)
from everest.simulator.everest_to_ert import everest_to_ert_config
from everest.strings import EVEREST

from ..run_arg import RunArg, create_run_arguments
from .base_run_model import BaseRunModel, StatusEvents

if TYPE_CHECKING:
    from ert.storage import Ensemble, Experiment


logger = logging.getLogger(__name__)


class SimulationStatus(TypedDict):
    status: dict[str, int]
    progress: list[list[JobProgress]]
    batch_number: int


class JobProgress(TypedDict):
    name: str
    status: str
    error: str | None
    start_time: datetime.datetime | None
    end_time: datetime.datetime | None
    realization: str
    simulation: str


class SimulationCallback(Protocol):
    def __call__(self, simulation_status: SimulationStatus | None) -> str | None: ...


class OptimizerCallback(Protocol):
    def __call__(self) -> str | None: ...


class EverestExitCode(IntEnum):
    COMPLETED = 1
    TOO_FEW_REALIZATIONS = 2
    MAX_FUNCTIONS_REACHED = 3
    MAX_BATCH_NUM_REACHED = 4
    USER_ABORT = 5
    EXCEPTION = 6


class EverestRunModel(BaseRunModel):
    def __init__(
        self,
        config: ErtConfig,
        everest_config: EverestConfig,
        simulation_callback: SimulationCallback | None,
        optimization_callback: OptimizerCallback | None,
    ):
        Path(everest_config.log_dir).mkdir(parents=True, exist_ok=True)
        Path(everest_config.optimization_output_dir).mkdir(parents=True, exist_ok=True)

        assert everest_config.environment is not None
        logging.getLogger(EVEREST).info(
            "Using random seed: %d. To deterministically reproduce this experiment, "
            "add the above random seed to your configuration file.",
            everest_config.environment.random_seed,
        )

        self._everest_config = everest_config
        self._sim_callback = simulation_callback
        self._opt_callback = optimization_callback
        self._fm_errors: dict[int, dict[str, Any]] = {}
        self._result: OptimalResult | None = None
        self._exit_code: EverestExitCode | None = None
        self._simulator_cache = (
            SimulatorCache()
            if (
                everest_config.simulator is not None
                and everest_config.simulator.enable_cache
            )
            else None
        )
        self._experiment: Experiment | None = None
        self._eval_server_cfg: EvaluatorServerConfig | None = None
        self._batch_id: int = 0
        self._status: SimulationStatus | None = None

        storage = open_storage(config.ens_path, mode="w")
        status_queue: queue.SimpleQueue[StatusEvents] = queue.SimpleQueue()

        super().__init__(
            storage,
            config.runpath_file,
            Path(config.user_config_file),
            config.env_vars,
            config.env_pr_fm_step,
            config.model_config,
            config.queue_config,
            config.forward_model_steps,
            status_queue,
            config.substitutions,
            config.ert_templates,
            config.hooked_workflows,
            active_realizations=[],  # Set dynamically in run_forward_model()
        )
        self.support_restart = False
        self._parameter_configuration = config.ensemble_config.parameter_configuration
        self._parameter_configs = config.ensemble_config.parameter_configs
        self._response_configuration = config.ensemble_config.response_configuration

    @classmethod
    def create(
        cls,
        ever_config: EverestConfig,
        simulation_callback: SimulationCallback | None = None,
        optimization_callback: OptimizerCallback | None = None,
    ) -> EverestRunModel:
        return cls(
            config=everest_to_ert_config(ever_config),
            everest_config=ever_config,
            simulation_callback=simulation_callback,
            optimization_callback=optimization_callback,
        )

    @classmethod
    def name(cls) -> str:
        return "Optimization run"

    @classmethod
    def description(cls) -> str:
        return "Run batches "

    @property
    def exit_code(self) -> EverestExitCode | None:
        return self._exit_code

    @property
    def result(self) -> OptimalResult | None:
        return self._result

    def __repr__(self) -> str:
        config_json = json.dumps(self._everest_config, sort_keys=True, indent=2)
        return f"EverestRunModel(config={config_json})"

    def run_experiment(
        self, evaluator_server_config: EvaluatorServerConfig, restart: bool = False
    ) -> None:
        self.log_at_startup()
        self._eval_server_cfg = evaluator_server_config
        self._experiment = self._storage.create_experiment(
            name=f"EnOpt@{datetime.datetime.now().strftime('%Y-%m-%d@%H:%M:%S')}",
            parameters=self._parameter_configuration,
            responses=self._response_configuration,
        )

        # Initialize the ropt optimizer:
        optimizer = self._create_optimizer()

        self.ever_storage = EverestStorage(
            output_dir=Path(self._everest_config.optimization_output_dir),
        )
        self.ever_storage.init(self._everest_config)
        self.ever_storage.observe_optimizer(optimizer)

        # Run the optimization:
        optimizer_exit_code = optimizer.run().exit_code

        # Extract the best result from the storage.
        self._result = self.ever_storage.get_optimal_result()

        if self._exit_code is None:
            match optimizer_exit_code:
                case OptimizerExitCode.MAX_FUNCTIONS_REACHED:
                    self._exit_code = EverestExitCode.MAX_FUNCTIONS_REACHED
                case OptimizerExitCode.USER_ABORT:
                    self._exit_code = EverestExitCode.USER_ABORT
                case OptimizerExitCode.TOO_FEW_REALIZATIONS:
                    self._exit_code = EverestExitCode.TOO_FEW_REALIZATIONS
                case _:
                    self._exit_code = EverestExitCode.COMPLETED

    def _init_transforms(self, variables: NDArray[np.float64]) -> OptModelTransforms:
        realizations = self._everest_config.model.realizations
        nreal = len(realizations)
        realization_weights = self._everest_config.model.realizations_weights
        if realization_weights is None:
            realization_weights = [1.0 / nreal] * nreal
        transforms = get_optimization_domain_transforms(
            self._everest_config.controls,
            self._everest_config.objective_functions,
            realization_weights,
        )
        # If required, initialize auto-scaling:
        assert isinstance(transforms.objectives, ObjectiveScaler)
        if transforms.objectives.has_auto_scale:
            objectives, _, _ = self._run_forward_model(
                np.repeat(np.expand_dims(variables, axis=0), nreal, axis=0),
                realizations,
            )
            transforms.objectives.calculate_auto_scales(objectives)
        return transforms

    def _create_optimizer(self) -> BasicOptimizer:
        # Initialize the optimization model transforms:
        transforms = self._init_transforms(
            np.asarray(
                FlattenedControls(self._everest_config.controls).initial_guesses,
                dtype=np.float64,
            )
        )
        optimizer = BasicOptimizer(
            enopt_config=everest2ropt(self._everest_config, transforms=transforms),
            evaluator=self._forward_model_evaluator,
        )

        # Before each batch evaluation we check if we should abort:
        optimizer.add_observer(
            EventType.START_EVALUATION,
            functools.partial(
                self._on_before_forward_model_evaluation,
                optimizer=optimizer,
            ),
        )

        return optimizer

    def _on_before_forward_model_evaluation(
        self, _: OptimizerEvent, optimizer: BasicOptimizer
    ) -> None:
        logging.getLogger(EVEREST).debug("Optimization callback called")

        if (
            self._everest_config.optimization is not None
            and self._everest_config.optimization.max_batch_num is not None
            and (self._batch_id >= self._everest_config.optimization.max_batch_num)
        ):
            self._exit_code = EverestExitCode.MAX_BATCH_NUM_REACHED
            logging.getLogger(EVEREST).info("Maximum number of batches reached")
            optimizer.abort_optimization()
        if (
            self._opt_callback is not None
            and self._opt_callback() == "stop_optimization"
        ):
            logging.getLogger(EVEREST).info("User abort requested.")
            optimizer.abort_optimization()

    def _run_forward_model(
        self,
        control_values: NDArray[np.float64],
        realizations: list[int],
        active_control_vectors: list[bool] | None = None,
    ) -> tuple[NDArray[np.float64], NDArray[np.float64] | None, list[int]]:
        # Reset the current run status:
        self._status = None

        # Get cached_results:
        cached_results = self._get_cached_results(control_values, realizations)

        # Collect the indices of the controls that must be evaluated in the batch:
        evaluated_control_indices = [
            idx
            for idx in range(control_values.shape[0])
            if idx not in cached_results
            and (active_control_vectors is None or active_control_vectors[idx])
        ]

        # Create the batch to run:
        batch_data = self._init_batch_data(control_values, evaluated_control_indices)

        # Initialize a new ensemble in storage:
        assert self._experiment is not None
        ensemble = self._experiment.create_ensemble(
            name=f"batch_{self._batch_id}",
            ensemble_size=len(batch_data),
        )
        for sim_id, controls in enumerate(batch_data.values()):
            self._setup_sim(sim_id, controls, ensemble)

        # Evaluate the batch:
        run_args = self._get_run_args(ensemble, realizations, batch_data)
        self._context_env.update(
            {
                "_ERT_EXPERIMENT_ID": str(ensemble.experiment_id),
                "_ERT_ENSEMBLE_ID": str(ensemble.id),
                "_ERT_SIMULATION_MODE": "batch_simulation",
            }
        )
        assert self._eval_server_cfg is not None
        self._evaluate_and_postprocess(run_args, ensemble, self._eval_server_cfg)

        # If necessary, delete the run path:
        self._delete_runpath(run_args)

        # Gather the results and create the result for ropt:
        results = self._gather_simulation_results(ensemble)
        objectives, constraints = self._get_objectives_and_constraints(
            control_values, batch_data, results, cached_results
        )

        # Add the results from the evaluations to the cache:
        self._add_results_to_cache(
            control_values, realizations, batch_data, objectives, constraints
        )

        # Increase the batch ID for the next evaluation:
        self._batch_id += 1

        # Return the results, together with the indices of the evaluated controls:
        return objectives, constraints, evaluated_control_indices

    def _forward_model_evaluator(
        self, control_values: NDArray[np.float64], evaluator_context: EvaluatorContext
    ) -> EvaluatorResult:
        control_indices = list(range(control_values.shape[0]))
        realizations = [
            self._everest_config.model.realizations[evaluator_context.realizations[idx]]
            for idx in control_indices
        ]
        active_control_vectors = [
            evaluator_context.active is None
            or bool(evaluator_context.active[evaluator_context.realizations[idx]])
            for idx in control_indices
        ]
        batch_id = self._batch_id  # Save the batch ID, it will be modified.
        objectives, constraints, evaluated_control_indices = self._run_forward_model(
            control_values, realizations, active_control_vectors
        )

        # The simulation id's are a simple enumeration over the evaluated
        # forward models. For the evaluated controls they are therefore
        # implicitly given by there position in the evaluated_control_indices
        # list. We store for each control vector that id, or -1 if it was not
        # evaluated:
        sim_ids = np.fromiter(
            (
                evaluated_control_indices.index(idx)
                if idx in evaluated_control_indices
                else -1
                for idx in control_indices
            ),
            dtype=np.intc,
        )

        return EvaluatorResult(
            objectives=objectives,
            constraints=constraints,
            batch_id=batch_id,
            evaluation_ids=sim_ids,
        )

    def _get_cached_results(
        self, control_values: NDArray[np.float64], realizations: list[int]
    ) -> dict[int, Any]:
        cached_results: dict[int, Any] = {}
        if self._simulator_cache is not None:
            for control_idx, realization in enumerate(realizations):
                cached_data = self._simulator_cache.get(
                    realization, control_values[control_idx, :]
                )
                if cached_data is not None:
                    cached_results[control_idx] = cached_data
        return cached_results

    def _init_batch_data(
        self,
        control_values: NDArray[np.float64],
        controls_to_evaluate: list[int],
    ) -> dict[int, dict[str, Any]]:
        def _add_controls(
            controls_config: list[ControlConfig], values: NDArray[np.float64]
        ) -> dict[str, Any]:
            batch_data_item: dict[str, Any] = {}
            value_list = values.tolist()
            for control in controls_config:
                control_dict: dict[str, Any] = batch_data_item.get(control.name, {})
                for variable in control.variables:
                    variable_value = control_dict.get(variable.name, {})
                    if isinstance(variable, ControlVariableGuessListConfig):
                        for index in range(1, len(variable.initial_guess) + 1):
                            variable_value[str(index)] = value_list.pop(0)
                    elif variable.index is not None:
                        variable_value[str(variable.index)] = value_list.pop(0)
                    else:
                        variable_value = value_list.pop(0)
                    control_dict[variable.name] = variable_value
                batch_data_item[control.name] = control_dict
            return batch_data_item

        return {
            idx: _add_controls(self._everest_config.controls, control_values[idx, :])
            for idx in controls_to_evaluate
        }

    def _setup_sim(
        self,
        sim_id: int,
        controls: dict[str, dict[str, Any]],
        ensemble: Ensemble,
    ) -> None:
        def _check_suffix(
            ext_config: ExtParamConfig,
            key: str,
            assignment: dict[str, Any] | tuple[str, str] | str | int,
        ) -> None:
            if key not in ext_config:
                raise KeyError(f"No such key: {key}")
            if isinstance(assignment, dict):  # handle suffixes
                suffixes = ext_config[key]
                if len(assignment) != len(suffixes):
                    missingsuffixes = set(suffixes).difference(set(assignment.keys()))
                    raise KeyError(
                        f"Key {key} is missing values for "
                        f"these suffixes: {missingsuffixes}"
                    )
                for suffix in assignment:
                    if suffix not in suffixes:
                        raise KeyError(
                            f"Key {key} has suffixes {suffixes}. "
                            f"Can't find the requested suffix {suffix}"
                        )
            else:
                suffixes = ext_config[key]
                if suffixes:
                    raise KeyError(
                        f"Key {key} has suffixes, a suffix must be specified"
                    )

        if set(controls.keys()) != set(self._everest_config.control_names):
            err_msg = "Mismatch between initialized and provided control names."
            raise KeyError(err_msg)

        for control_name, control in controls.items():
            ext_config = self._parameter_configs[control_name]
            if isinstance(ext_config, ExtParamConfig):
                if len(ext_config) != len(control.keys()):
                    raise KeyError(
                        f"Expected {len(ext_config)} variables for "
                        f"control {control_name}, "
                        f"received {len(control.keys())}."
                    )
                for var_name, var_setting in control.items():
                    _check_suffix(ext_config, var_name, var_setting)

                ensemble.save_parameters(
                    control_name, sim_id, ExtParamConfig.to_dataset(control)
                )

    def _get_run_args(
        self,
        ensemble: Ensemble,
        realizations: list[int],
        batch_data: dict[int, Any],
    ) -> list[RunArg]:
        substitutions = self._substitutions
        substitutions["<BATCH_NAME>"] = ensemble.name
        self.active_realizations = [True] * len(batch_data)
        for sim_id, control_idx in enumerate(batch_data.keys()):
            substitutions[f"<GEO_ID_{sim_id}_0>"] = str(realizations[control_idx])
        run_paths = Runpaths(
            jobname_format=self._model_config.jobname_format_string,
            runpath_format=self._model_config.runpath_format_string,
            filename=str(self._runpath_file),
            substitutions=substitutions,
            eclbase=self._model_config.eclbase_format_string,
        )
        return create_run_arguments(
            run_paths,
            self.active_realizations,
            ensemble=ensemble,
        )

    def _delete_runpath(self, run_args: list[RunArg]) -> None:
        logging.getLogger(EVEREST).debug("Simulation callback called")
        if (
            self._everest_config.simulator is not None
            and self._everest_config.simulator.delete_run_path
        ):
            for i, real in self.get_current_snapshot().reals.items():
                path_to_delete = run_args[int(i)].runpath
                if real["status"] == "Finished" and os.path.isdir(path_to_delete):

                    def onerror(
                        _: Callable[..., Any],
                        path: str,
                        sys_info: tuple[
                            type[BaseException], BaseException, TracebackType
                        ],
                    ) -> None:
                        logging.getLogger(EVEREST).debug(
                            f"Failed to remove {path}, {sys_info}"
                        )

                    shutil.rmtree(path_to_delete, onerror=onerror)  # pylint: disable=deprecated-argument

    def _gather_simulation_results(
        self, ensemble: Ensemble
    ) -> list[dict[str, NDArray[np.float64]]]:
        results: list[dict[str, NDArray[np.float64]]] = []
        for sim_id, successful in enumerate(self.active_realizations):
            if not successful:
                logger.error(f"Simulation {sim_id} failed.")
                results.append({})
                continue
            d = {}
            for key in self._everest_config.result_names:
                data = ensemble.load_responses(key, (sim_id,))
                d[key] = data["values"].to_numpy()
            results.append(d)
        for fnc_name, alias in self._everest_config.function_aliases.items():
            for result in results:
                result[fnc_name] = result[alias]
        return results

    def _get_objectives_and_constraints(
        self,
        control_values: NDArray[np.float64],
        batch_data: dict[int, Any],
        results: list[dict[str, NDArray[np.float64]]],
        cached_results: dict[int, Any],
    ) -> tuple[NDArray[np.float64], NDArray[np.float64] | None]:
        # We minimize the negative of the objectives:
        objectives = -self._get_simulation_results(
            results, self._everest_config.objective_names, control_values, batch_data
        )

        constraints = None
        if self._everest_config.output_constraints:
            constraints = self._get_simulation_results(
                results,
                self._everest_config.constraint_names,
                control_values,
                batch_data,
            )

        if self._simulator_cache is not None:
            for control_idx, (
                cached_objectives,
                cached_constraints,
            ) in cached_results.items():
                objectives[control_idx, ...] = cached_objectives
                if constraints is not None:
                    assert cached_constraints is not None
                    constraints[control_idx, ...] = cached_constraints

        return objectives, constraints

    @staticmethod
    def _get_simulation_results(
        results: list[dict[str, NDArray[np.float64]]],
        names: list[str],
        controls: NDArray[np.float64],
        batch_data: dict[int, Any],
    ) -> NDArray[np.float64]:
        control_indices = list(batch_data.keys())
        values = np.zeros((controls.shape[0], len(names)), dtype=float64)
        for func_idx, name in enumerate(names):
            values[control_indices, func_idx] = np.fromiter(
                (np.nan if not result else result[name][0] for result in results),
                dtype=np.float64,
            )
        return values

    def _add_results_to_cache(
        self,
        control_values: NDArray[np.float64],
        realizations: list[int],
        batch_data: dict[int, Any],
        objectives: NDArray[np.float64],
        constraints: NDArray[np.float64] | None,
    ) -> None:
        if self._simulator_cache is not None:
            for control_idx in batch_data:
                self._simulator_cache.add(
                    realizations[control_idx],
                    control_values[control_idx, ...],
                    objectives[control_idx, ...],
                    None if constraints is None else constraints[control_idx, ...],
                )

    def check_if_runpath_exists(self) -> bool:
        return (
            self._everest_config.simulation_dir is not None
            and os.path.exists(self._everest_config.simulation_dir)
            and any(os.listdir(self._everest_config.simulation_dir))
        )

    def send_snapshot_event(self, event: Event, iteration: int) -> None:
        super().send_snapshot_event(event, iteration)
        if type(event) in {EESnapshot, EESnapshotUpdate}:
            newstatus = self._simulation_status(self.get_current_snapshot())
            if self._status != newstatus:  # No change in status
                if self._sim_callback is not None:
                    self._sim_callback(newstatus)
                self._status = newstatus

    def _simulation_status(self, snapshot: EnsembleSnapshot) -> SimulationStatus:
        jobs_progress: list[list[JobProgress]] = []
        prev_realization = None
        jobs: list[JobProgress] = []
        for (realization, simulation), fm_step in snapshot.get_all_fm_steps().items():
            if realization != prev_realization:
                prev_realization = realization
                if jobs:
                    jobs_progress.append(jobs)
                jobs = []
            jobs.append(
                {
                    "name": fm_step.get("name") or "Unknown",
                    "status": fm_step.get("status") or "Unknown",
                    "error": fm_step.get("error", ""),
                    "start_time": fm_step.get("start_time", None),
                    "end_time": fm_step.get("end_time", None),
                    "realization": realization,
                    "simulation": simulation,
                }
            )
            if fm_step.get("error", ""):
                self._handle_errors(
                    batch=self._batch_id,
                    simulation=simulation,
                    realization=realization,
                    fm_name=fm_step.get("name", "Unknown"),  # type: ignore
                    error_path=fm_step.get("stderr", ""),  # type: ignore
                    fm_running_err=fm_step.get("error", ""),  # type: ignore
                )
        jobs_progress.append(jobs)

        return {
            "status": self.get_current_status(),
            "progress": jobs_progress,
            "batch_number": self._batch_id,
        }

    def _handle_errors(
        self,
        batch: int,
        simulation: Any,
        realization: str,
        fm_name: str,
        error_path: str,
        fm_running_err: str,
    ) -> None:
        fm_id = f"b_{batch}_r_{realization}_s_{simulation}_{fm_name}"
        fm_logger = logging.getLogger("forward_models")
        if Path(error_path).is_file():
            error_str = Path(error_path).read_text(encoding="utf-8") or fm_running_err
        else:
            error_str = fm_running_err
        error_hash = hash(error_str)
        err_msg = "Batch: {} Realization: {} Simulation: {} Job: {} Failed {}".format(
            batch, realization, simulation, fm_name, "\n Error: {} ID:{}"
        )

        if error_hash not in self._fm_errors:
            error_id = len(self._fm_errors)
            fm_logger.error(err_msg.format(error_str, error_id))
            self._fm_errors.update({error_hash: {"error_id": error_id, "ids": [fm_id]}})
        elif fm_id not in self._fm_errors[error_hash]["ids"]:
            self._fm_errors[error_hash]["ids"].append(fm_id)
            error_id = self._fm_errors[error_hash]["error_id"]
            fm_logger.error(err_msg.format("Already reported as", error_id))


class SimulatorCache:
    EPS = float(np.finfo(np.float32).eps)

    def __init__(self) -> None:
        self._data: defaultdict[
            int,
            list[
                tuple[
                    NDArray[np.float64], NDArray[np.float64], NDArray[np.float64] | None
                ]
            ],
        ] = defaultdict(list)

    def add(
        self,
        realization: int,
        control_values: NDArray[np.float64],
        objectives: NDArray[np.float64],
        constraints: NDArray[np.float64] | None,
    ) -> None:
        """Add objective and constraints for a given realization and control values.

        The realization is the index of the realization in the ensemble, as specified
        in by the realizations entry in the everest model configuration. Both the control
        values and the realization are used as keys to retrieve the objectives and
        constraints later.
        """
        self._data[realization].append(
            (
                control_values.copy(),
                objectives.copy(),
                None if constraints is None else constraints.copy(),
            ),
        )

    def get(
        self, realization: int, controls: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64] | None] | None:
        """Get objective and constraints for a given realization and control values.

        The realization is the index of the realization in the ensemble, as specified
        in by the realizations entry in the everest model configuration. Both the control
        values and the realization are used as keys to retrieve the objectives and
        constraints from the cached values.
        """
        for control_values, objectives, constraints in self._data.get(realization, []):
            if np.allclose(controls, control_values, rtol=0.0, atol=self.EPS):
                return objectives, constraints
        return None
