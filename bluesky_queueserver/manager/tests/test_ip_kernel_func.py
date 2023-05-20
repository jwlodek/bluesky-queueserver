import pprint
import time as ttime

import pytest

from ..comms import zmq_single_request
from .common import re_manager  # noqa: F401
from .common import re_manager_cmd  # noqa: F401
from .common import (
    _user,
    _user_group,
    append_code_to_last_startup_file,
    condition_environment_closed,
    condition_environment_created,
    condition_manager_idle,
    condition_manager_paused,
    copy_default_profile_collection,
    get_queue_state,
    use_ipykernel_for_tests,
    wait_for_condition,
    wait_for_task_result,
)

timeout_env_open = 10

# Plans used in most of the tests: '_plan1' and '_plan2' are quickly executed '_plan3' runs for 5 seconds.
_plan1 = {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}
_plan2 = {"name": "scan", "args": [["det1", "det2"], "motor", -1, 1, 10], "item_type": "plan"}
_plan3 = {"name": "count", "args": [["det1", "det2"]], "kwargs": {"num": 5, "delay": 1}, "item_type": "plan"}
_plan4 = {"name": "count", "args": [["det1", "det2"]], "kwargs": {"num": 10, "delay": 1}, "item_type": "plan"}
_instruction_stop = {"name": "queue_stop", "item_type": "instruction"}

_script_with_ip_features = """
from IPython.core.magic import register_line_magic, register_cell_magic

@register_line_magic
def lmagic(line):
    return line

@register_cell_magic
def cmagic(line, cell):
    return line, cell
"""


def test_ip_kernel_loading_script_01(tmp_path, re_manager_cmd):  # noqa: F811
    """
    Test that the IPython-based worker can load startup code with IPython-specific features,
    and regular worker fails.
    """
    using_ipython = use_ipykernel_for_tests()

    pc_path = copy_default_profile_collection(tmp_path)
    append_code_to_last_startup_file(pc_path, additional_code=_script_with_ip_features)

    params = ["--startup-dir", pc_path]
    re_manager_cmd(params)

    resp2, _ = zmq_single_request("environment_open")
    assert resp2["success"] is True
    assert resp2["msg"] == ""

    if not using_ipython:
        assert not wait_for_condition(time=timeout_env_open, condition=condition_environment_created)

    else:
        assert wait_for_condition(time=timeout_env_open, condition=condition_environment_created)

        resp9, _ = zmq_single_request("environment_close")
        assert resp9["success"] is True
        assert resp9["msg"] == ""

        assert wait_for_condition(time=3, condition=condition_environment_closed)


def test_ip_kernel_loading_script_02(re_manager):  # noqa: F811
    """
    Test that the IPython-based worker accepts uploaded scripts with IPython-specific code
    and the regular worker fails.
    """
    using_ipython = use_ipykernel_for_tests()

    resp2, _ = zmq_single_request("environment_open")
    assert resp2["success"] is True
    assert resp2["msg"] == ""

    assert wait_for_condition(time=timeout_env_open, condition=condition_environment_created)

    resp3, _ = zmq_single_request("script_upload", params={"script": _script_with_ip_features})
    assert resp3["success"] is True, pprint.pformat(resp3)

    result = wait_for_task_result(10, resp3["task_uid"])
    if not using_ipython:
        assert result["success"] is False, pprint.pformat(result)
        assert "Failed to execute stript" in result["msg"]
    else:
        assert result["success"] is True, pprint.pformat(result)
        assert result["msg"] == "", pprint.pformat(result)

    resp9, _ = zmq_single_request("environment_close")
    assert resp9["success"] is True
    assert resp9["msg"] == ""

    assert wait_for_condition(time=3, condition=condition_environment_closed)


# fmt: off
@pytest.mark.parametrize("resume_option", ["resume", "stop", "halt", "abort"])
@pytest.mark.parametrize("plan_option", ["queue", "plan"])
# fmt: on
def test_ip_kernel_loading_script_03(re_manager, plan_option, resume_option):  # noqa: F811
    """
    Test basic operations: execute a plan (as part of queue or individually), pause and
    resume/stop/halt/abort the plan. Check that ``ip_kernel_state`` and ``ip_kernel_captured``
    are properly set at every stage.
    """
    using_ipython = use_ipykernel_for_tests()

    def check_status(ip_kernel_state, ip_kernel_captured):
        # Returned status may be used to do additional checks
        status = get_queue_state()
        if isinstance(ip_kernel_state, (str, type(None))):
            ip_kernel_state = [ip_kernel_state]
        assert status["ip_kernel_state"] in ip_kernel_state
        assert status["ip_kernel_captured"] == ip_kernel_captured
        return status

    check_status(None, None)

    resp2, _ = zmq_single_request("environment_open")
    assert resp2["success"] is True
    assert resp2["msg"] == ""

    assert wait_for_condition(time=timeout_env_open, condition=condition_environment_created)

    check_status("idle" if using_ipython else "disabled", False if using_ipython else True)

    if plan_option in ("queue", "plan"):
        if plan_option == "queue":
            resp, _ = zmq_single_request(
                "queue_item_add", {"item": _plan4, "user": _user, "user_group": _user_group}
            )
            assert resp["success"] is True
            resp, _ = zmq_single_request("queue_start")
            assert resp["success"] is True
        elif plan_option == "plan":
            resp, _ = zmq_single_request(
                "queue_item_execute", {"item": _plan4, "user": _user, "user_group": _user_group}
            )
            assert resp["success"] is True
        else:
            assert False, f"Unsupported option: {plan_option!r}"

        s = get_queue_state()  # Kernel may not be 'captured' at this point
        assert s["manager_state"] == "executing_queue"
        assert s["worker_environment_state"] in ("idle", "executing_plan")

        ttime.sleep(1)

        s = check_status("busy" if using_ipython else "disabled", True)
        assert s["manager_state"] == "executing_queue"
        assert s["worker_environment_state"] == "executing_plan"

        ttime.sleep(1)

        resp, _ = zmq_single_request("re_pause")
        assert resp["success"] is True, pprint.pformat(resp)

        wait_for_condition(time=10, condition=condition_manager_paused)

        s = check_status("idle" if using_ipython else "disabled", False if using_ipython else True)
        assert s["manager_state"] == "paused"
        assert s["worker_environment_state"] == "idle"

        resp, _ = zmq_single_request(f"re_{resume_option}")
        assert resp["success"] is True, pprint.pformat(resp)

        if resume_option == "resume":
            s = get_queue_state()  # Kernel may not be 'captured' at this point
            assert s["manager_state"] == "executing_queue"
            assert s["worker_environment_state"] in ("idle", "executing_plan")

            ttime.sleep(1)

            s = check_status("busy" if using_ipython else "disabled", True)
            assert s["manager_state"] == "executing_queue"
            assert s["worker_environment_state"] == "executing_plan"

        assert wait_for_condition(time=20, condition=condition_manager_idle)

    else:
        assert False, f"Unsupported option: {plan_option!r}"

    resp9, _ = zmq_single_request("environment_close")
    assert resp9["success"] is True
    assert resp9["msg"] == ""

    assert wait_for_condition(time=3, condition=condition_environment_closed)

    check_status(None, None)


# fmt: off
@pytest.mark.parametrize("option", ["function", "script"])
@pytest.mark.parametrize("run_in_background", [False, True])
# fmt: on
def test_ip_kernel_loading_script_04(re_manager, option, run_in_background):  # noqa: F811
    """
    Test basic operations: execute a function or a script as a foreground or background task.
    Check that ``ip_kernel_state`` and ``ip_kernel_captured`` are properly set at every stage.
    """
    using_ipython = use_ipykernel_for_tests()

    def check_status(ip_kernel_state, ip_kernel_captured):
        # Returned status may be used to do additional checks
        status = get_queue_state()
        if isinstance(ip_kernel_state, (str, type(None))):
            ip_kernel_state = [ip_kernel_state]
        assert status["ip_kernel_state"] in ip_kernel_state
        assert status["ip_kernel_captured"] == ip_kernel_captured
        return status

    check_status(None, None)

    resp2, _ = zmq_single_request("environment_open")
    assert resp2["success"] is True
    assert resp2["msg"] == ""

    assert wait_for_condition(time=timeout_env_open, condition=condition_environment_created)

    check_status("idle" if using_ipython else "disabled", False if using_ipython else True)

    if option == "function":
        # Upload a script with a function function
        script = "def func_for_test():\n    import time\n    time.sleep(3)"
        resp, _ = zmq_single_request("script_upload", params={"script": script})
        assert resp["success"] is True
        wait_for_condition(time=3, condition=condition_manager_idle)

        # Make sure that RE Manager and Worker are in the correct state
        s = check_status("idle" if using_ipython else "disabled", False if using_ipython else True)
        assert s["manager_state"] == "idle"
        assert s["worker_environment_state"] == "idle"

        wait_for_condition(time=30, condition=condition_manager_idle)

        func_info = {"name": "func_for_test", "item_type": "function"}
        resp, _ = zmq_single_request(
            "function_execute",
            params={
                "item": func_info,
                "user": _user,
                "user_group": _user_group,
                "run_in_background": run_in_background,
            },
        )
        assert resp["success"] is True, pprint.pformat(resp)
        task_uid = resp["task_uid"]

    elif option == "script":
        script = "import time\ntime.sleep(3)"
        resp, _ = zmq_single_request(
            "script_upload", params={"script": script, "run_in_background": run_in_background}
        )
        assert resp["success"] is True
        task_uid = resp["task_uid"]

    else:
        assert False, f"Unsupported option: {option!r}"

    if not run_in_background:
        s = get_queue_state()  # Kernel may or may not be captured at this point
        assert s["manager_state"] == "executing_task"
        assert s["worker_environment_state"] in ("idle", "executing_task")

        ttime.sleep(1)

        s = check_status("busy" if using_ipython else "disabled", True)
        assert s["manager_state"] == "executing_task"
        assert s["worker_environment_state"] == "executing_task"
    else:
        s = check_status("idle" if using_ipython else "disabled", False if using_ipython else True)
        assert s["manager_state"] == "idle"
        assert s["worker_environment_state"] == "idle"

        ttime.sleep(1)

        s = check_status("idle" if using_ipython else "disabled", False if using_ipython else True)
        assert s["manager_state"] == "idle"
        assert s["worker_environment_state"] == "idle"

    assert wait_for_task_result(10, task_uid)

    s = check_status("idle" if using_ipython else "disabled", False if using_ipython else True)
    assert s["manager_state"] == "idle"
    assert s["worker_environment_state"] == "idle"

    resp9, _ = zmq_single_request("environment_close")
    assert resp9["success"] is True
    assert resp9["msg"] == ""

    assert wait_for_condition(time=3, condition=condition_environment_closed)

    check_status(None, None)
