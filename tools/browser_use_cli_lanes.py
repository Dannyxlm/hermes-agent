"""Task selection and tab-ledger preparation for browser-use idle lanes."""

from typing import NamedTuple


class LaneCall(NamedTuple):
    code: str
    key: tuple[str, str] | None
    browser_cfg: dict
    busy: bool = False


def prepare_lane_call(env: dict, code: str, session: str, task_id: str | None,
                      local_sidecar: bool) -> LaneCall:
    from tools import browser_use_cli as cli

    private_browser = env.pop(cli._PRIVATE_BROWSER_SENTINEL, None)
    shared_browser = bool(env.pop(cli._SHARED_BROWSER_SENTINEL, None))
    browser_cfg = cli._read_browser_cfg()
    # The shared default daemon attaches to the first real page. Opt-in task lanes avoid collisions.
    if (shared_browser and not session and not local_sidecar and not private_browser and task_id
            and cli._exec_task_tabs_enabled(browser_cfg)):
        env["BU_NAME"] = cli._task_session_name(task_id)
    lane = env.get("BU_NAME", "default")
    touch = cli._touch_lane(env, lane, cli._exec_idle_seconds(browser_cfg))
    if touch.busy:
        return LaneCall(code, None, browser_cfg, busy=True)
    tabs_file = touch.tabs
    lane_key = (str(tabs_file.parent), lane) if tabs_file is not None else None
    if tabs_file is not None:
        env[cli._TABS_FILE_ENV] = str(tabs_file)
        code = cli._TAB_LEDGER_PRELUDE + code
    return LaneCall(code, lane_key, browser_cfg)
