from datetime import datetime

from _ert.events import (
    ForwardModelStepFailure,
    ForwardModelStepRunning,
    ForwardModelStepSuccess,
    RealizationSuccess,
)
from ert.ensemble_evaluator import state
from ert.ensemble_evaluator.snapshot import (
    ForwardModel,
    Snapshot,
)
from tests import SnapshotBuilder


def test_snapshot_merge(snapshot: Snapshot):
    update_event = Snapshot()
    update_event.update_forward_model(
        real_id="1",
        forward_model_id="0",
        forward_model=ForwardModel(
            status="Finished",
            index="0",
            start_time=datetime(year=2020, month=10, day=27),
            end_time=datetime(year=2020, month=10, day=28),
        ),
    )
    update_event.update_forward_model(
        real_id="1",
        forward_model_id="1",
        forward_model=ForwardModel(
            status="Running",
            index="1",
            start_time=datetime(year=2020, month=10, day=27),
        ),
    )
    update_event.update_forward_model(
        real_id="9",
        forward_model_id="0",
        forward_model=ForwardModel(
            status="Running",
            index="0",
            start_time=datetime(year=2020, month=10, day=27),
        ),
    )

    snapshot.merge_snapshot(update_event)

    assert snapshot.status == state.ENSEMBLE_STATE_UNKNOWN

    assert snapshot.get_job(real_id="1", forward_model_id="0") == ForwardModel(
        status="Finished",
        index="0",
        start_time=datetime(year=2020, month=10, day=27),
        end_time=datetime(year=2020, month=10, day=28),
        name="forward_model0",
    )

    assert snapshot.get_job(real_id="1", forward_model_id="1") == ForwardModel(
        status="Running",
        index="1",
        start_time=datetime(year=2020, month=10, day=27),
        name="forward_model1",
    )

    assert (
        snapshot.get_job(real_id="9", forward_model_id="0").get("status") == "Running"
    )
    assert snapshot.get_job(real_id="9", forward_model_id="0") == ForwardModel(
        status="Running",
        index="0",
        start_time=datetime(year=2020, month=10, day=27),
        name="forward_model0",
    )


def test_update_forward_models_in_partial_from_multiple_messages(snapshot):
    new_snapshot = Snapshot()
    new_snapshot.update_from_event(
        ForwardModelStepRunning(
            ensemble="1",
            real="0",
            fm_step="0",
            current_memory_usage=5,
            max_memory_usage=6,
        )
    )
    new_snapshot.update_from_event(
        ForwardModelStepFailure(
            ensemble="1", real="0", fm_step="0", error_msg="failed"
        ),
    )
    new_snapshot.update_from_event(
        ForwardModelStepSuccess(ensemble="1", real="0", fm_step="1")
    )
    forward_models = new_snapshot.to_dict()["reals"]["0"]["forward_models"]
    assert forward_models["0"]["status"] == state.FORWARD_MODEL_STATE_FAILURE
    assert forward_models["1"]["status"] == state.FORWARD_MODEL_STATE_FINISHED


def test_that_realization_success_message_updates_state():
    snapshot = SnapshotBuilder().build(["0"], status="Unknown")
    snapshot.update_from_event(RealizationSuccess(ensemble="0", real="0"))
    assert (
        snapshot.to_dict()["reals"]["0"]["status"] == state.REALIZATION_STATE_FINISHED
    )
