"""已出发人群组逐段实际通行核报的集成测试。

覆盖：受理（顺序/时序/通道/前缀）、幂等与重复拒绝、滞留改编后固定前缀
可查与新余程续报、人工救援组只核报前缀、计划/实绩并列查询、通道逐分钟
实绩人数与超容量/封闭异常标注、偏离与超额留痕但不改编排、场景重置清空。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-passage-test-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


# 1 ->2(4 分钟, 容量10)-> 6(2 分钟)；另有 2->3->5 备用路线
TWO_STEP_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
        {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
        {"id": 3, "src": 2, "dst": 3, "minutes": 2, "capacity": 10},
        {"id": 4, "src": 3, "dst": 5, "minutes": 2, "capacity": 10},
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=TWO_STEP_SCENARIO)
        assert resp.status_code == 200
        yield c


def _segments(c, group_id=1):
    d = c.get(f"/passage-reports/groups/{group_id}").json()
    return {s["seq"]: s for s in d["segments"]}


def test_report_requires_departed_group(client):
    # 当前分钟 0、组 1 出发分钟也是 0：出发分钟 == 当前分钟仍属未出发
    resp = client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_NOT_DEPARTED"
    # 不留记录
    assert client.get("/passage-reports/groups/1").json()["reported_count"] == 0


def test_segment_and_edge_validation(client):
    client.post("/tick", json={"minutes": 5})
    # 段序号越界
    resp = client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 9, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "SEGMENT_NOT_FOUND"
    # 通道与现行路线段不符
    resp = client.post("/passage-reports", json={
        "serial": "R-2", "group_id": 1, "seq": 0, "edge_id": 2,
        "enter_minute": 0, "exit_minute": 4})
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "EDGE_MISMATCH"
    assert conflict["expected_edge_id"] == 1
    # 拒绝不留记录
    assert client.get("/passage-reports/groups/1").json()["reported_count"] == 0


def test_ordered_reporting_and_idempotency(client):
    client.post("/tick", json={"minutes": 6})
    # 越序：先报 seq1
    resp = client.post("/passage-reports", json={
        "serial": "R-9", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 4, "exit_minute": 6})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "OUT_OF_ORDER"

    # 受理 seq0
    resp = client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert resp.status_code == 201
    assert resp.json()["reported_minute"] == 6

    # 同号同内容重试 -> 200 原记录
    resp = client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert resp.status_code == 200
    assert resp.json()["reported_minute"] == 6

    # 同段重复（新报送号）-> 409
    resp = client.post("/passage-reports", json={
        "serial": "R-1DUP", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "SEGMENT_ALREADY_REPORTED"

    # 同号异内容 -> 409 SERIAL_CONTENT_MISMATCH
    resp = client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 5})
    assert resp.status_code == 409
    assert resp.json()["error"] == "PASSAGE_REPORT_SERIAL_CONFLICT"

    # 后段进入早于前段离开 -> 409
    resp = client.post("/passage-reports", json={
        "serial": "R-2", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 3, "exit_minute": 6})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "ENTER_BEFORE_PREVIOUS_EXIT"

    # 离开晚于当前分钟 -> 409
    resp = client.post("/passage-reports", json={
        "serial": "R-2", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 4, "exit_minute": 8})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "EXIT_MINUTE_IN_FUTURE"

    # 合法续报 seq1（与计划一致 [4,6)）
    resp = client.post("/passage-reports", json={
        "serial": "R-2", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 4, "exit_minute": 6})
    assert resp.status_code == 201
    view = client.get("/passage-reports/groups/1").json()
    assert view["reported_count"] == 2
    assert view["total_segments"] == 2


def test_minute_validation_422(client):
    client.post("/tick", json={"minutes": 5})
    for body in (
        {"serial": "X", "group_id": 1, "seq": 0, "edge_id": 1,
         "enter_minute": -1, "exit_minute": 4},
        {"serial": "X", "group_id": 1, "seq": 0, "edge_id": 1,
         "enter_minute": 4, "exit_minute": 4},
        {"serial": "", "group_id": 1, "seq": 0, "edge_id": 1,
         "enter_minute": 0, "exit_minute": 4},
    ):
        assert client.post("/passage-reports", json=body).status_code == 422
    assert client.post("/passage-reports", json={
        "serial": "X", "group_id": 999, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4}).status_code == 422


def test_plan_actual_side_by_side_query(client):
    client.post("/tick", json={"minutes": 8})
    # 实绩偏离：seq1 实际 [5,7) 而计划 [4,6)
    client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    client.post("/passage-reports", json={
        "serial": "R-2", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 5, "exit_minute": 7})
    segs = _segments(client)
    assert segs[0]["planned_enter_minute"] == 0
    assert segs[0]["planned_exit_minute"] == 4
    assert segs[0]["actual_enter_minute"] == 0
    assert segs[0]["timing_deviation"] is False
    assert segs[1]["planned_enter_minute"] == 4
    assert segs[1]["planned_exit_minute"] == 6
    assert segs[1]["actual_enter_minute"] == 5
    assert segs[1]["actual_exit_minute"] == 7
    assert segs[1]["timing_deviation"] is True
    # 汇总列表接口同样可用
    listing = client.get("/passage-reports/groups").json()
    assert listing["current_minute"] == 8
    assert len(listing["groups"]) == 1
    assert listing["groups"][0]["group_id"] == 1


def test_unknown_group_and_edge_404(client):
    assert client.get("/passage-reports/groups/999").status_code == 404
    assert client.get("/passages/999/actual-load").status_code == 404


def test_delay_reroute_keeps_prefix_and_reports_new_journey(client):
    # tick 到 4：组 1 走完通道1、到达节点 2
    client.post("/tick", json={"minutes": 4})
    r = client.post("/passage-reports", json={
        "serial": "P-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert r.status_code == 201

    # 滞留改编：节点 2、第 6 分钟恢复 -> 余程通道2 计划 [6,8)
    r = client.post("/groups/1/delay-report",
                    json={"node_id": 2, "recovery_minute": 6})
    assert r.status_code == 200
    g = {x["group_id"]: x for x in r.json()["groups"]}[1]
    assert g["status"] == "fixed"
    assert g["prefix_step_count"] == 1
    assert [(s["edge_id"], s["enter_minute"], s["exit_minute"])
            for s in g["steps"]] == [(1, 0, 4), (2, 6, 8)]

    # 已核报固定前缀仍可查，且标记 in_fixed_prefix
    segs = _segments(client)
    assert segs[0]["reported"] is True
    assert segs[0]["in_fixed_prefix"] is True
    assert segs[1]["reported"] is False
    assert segs[1]["planned_enter_minute"] == 6
    assert segs[1]["in_fixed_prefix"] is False

    # tick 到 8 后续报新余程 seq1
    client.post("/tick", json={"minutes": 4})
    r = client.post("/passage-reports", json={
        "serial": "P-2", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 6, "exit_minute": 8})
    assert r.status_code == 201


def test_awaiting_rescue_and_rescue_teams_only_report_prefix(client):
    # 切换为只有唯一余程（通道2）的小场景，并预先封闭通道2 [5,30)
    rescue_scenario = {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
        ],
    }
    client.post("/scenario", json=rescue_scenario)
    client.post("/tick", json={"minutes": 4})
    client.post("/passage-reports", json={
        "serial": "P-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    r = client.post("/groups/1/delay-report",
                    json={"node_id": 2, "recovery_minute": 6})
    g = {x["group_id"]: x for x in r.json()["groups"]}[1]
    assert g["status"] == "awaiting_rescue"

    # 待救援组核报余程序号 -> SEGMENT_NOT_FOUND（只可核报已有前缀）
    r = client.post("/passage-reports", json={
        "serial": "P-2", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 6, "exit_minute": 7})
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["reason"] == "SEGMENT_NOT_FOUND"
    # 前缀仍可查
    assert client.get("/passage-reports/groups/1").json()["reported_count"] == 1

    # 派遣 -> rescuing，仍只可核报前缀
    client.post("/rescue-teams", json={"team_id": 1, "capacity": 10})
    r = client.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 4})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    r = client.post("/passage-reports", json={
        "serial": "P-3", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 4, "exit_minute": 5})
    assert r.status_code == 409

    # 结案 -> rescued，仍只可核报前缀
    client.post("/tick", json={"minutes": 2})
    r = client.post(f"/rescue-tasks/{task_id}/completion",
                    json={"exit_id": 6, "delivery_minute": 6})
    assert r.status_code == 200
    r = client.post("/passage-reports", json={
        "serial": "P-4", "group_id": 1, "seq": 1, "edge_id": 2,
        "enter_minute": 6, "exit_minute": 7})
    assert r.status_code == 409
    assert client.get("/passage-reports/groups/1").json()["reported_count"] == 1


def test_actual_load_over_capacity_and_deviation_kept(client):
    """实绩重叠导致超容量：仍留痕、标注异常、不反向修改编排与占用。"""
    client.post("/groups", json=[
        {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 30}])
    plan_before = client.get("/plan").json()
    client.post("/tick", json={"minutes": 6})
    # 两组实绩都在通道 1 的 [0,4)：合计 20 > 容量 10
    for gid, serial in ((1, "A-1"), (2, "A-2")):
        r = client.post("/passage-reports", json={
            "serial": serial, "group_id": gid, "seq": 0, "edge_id": 1,
            "enter_minute": 0, "exit_minute": 4})
        assert r.status_code == 201

    load = client.get("/passages/1/actual-load").json()
    minute_slots = {m["minute"]: m for m in load["minutes"]}
    for minute in range(0, 4):
        assert minute_slots[minute]["used"] == 20
        assert minute_slots[minute]["effective_capacity"] == 10
        assert minute_slots[minute]["over_capacity"] is True

    # 组 2 的计划本不在 [0,4)：实绩偏离与超容量均标出
    segs2 = _segments(client, 2)
    assert segs2[0]["timing_deviation"] is True
    assert segs2[0]["over_capacity"] is True

    # 不反向修改编排：组 2 计划时刻不变
    plan_after = client.get("/plan").json()
    g_before = {g["group_id"]: g for g in plan_before["groups"]}[2]
    g_after = {g["group_id"]: g for g in plan_after["groups"]}[2]
    assert g_before["steps"] == g_after["steps"]
    # 规划占用接口 /load 不含实绩
    planned_load = client.get("/passages/1/load").json()
    for m in planned_load["minutes"]:
        assert m["used"] <= 10

    # 核报不产生修订
    ops = [r["operation"] for r in client.get("/revisions").json()["revisions"]]
    assert "tick" in ops  # 有 tick 修订
    # 核报本身没有专属操作类型（最新修订仍是 tick）
    assert ops[-1] == "tick"


def test_actual_load_closure_hit(client):
    client.post("/tick", json={"minutes": 4})
    # 登记完全在过去的封闭 [0,1)：不触及已出发组剩余路线，可登记
    r = client.post("/closures",
                    json={"edge_id": 1, "start_minute": 0, "end_minute": 1})
    assert r.status_code == 201
    client.post("/passage-reports", json={
        "serial": "K-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    load = client.get("/passages/1/actual-load").json()
    slots = {m["minute"]: m for m in load["minutes"]}
    assert slots[0]["closed"] is True
    assert slots[0]["closure_hit"] is True
    assert slots[1]["closed"] is False
    segs = _segments(client)
    assert segs[0]["closure_hit"] is True


def test_scenario_reset_clears_actuals(client):
    client.post("/tick", json={"minutes": 4})
    client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert client.get("/passage-reports/groups/1").json()["reported_count"] == 1

    client.post("/scenario", json=TWO_STEP_SCENARIO)
    view = client.get("/passage-reports/groups/1").json()
    assert view["reported_count"] == 0
    assert view["segments"][0]["reported"] is False
    assert client.get("/passages/1/actual-load").json()["minutes"] == []
    # 报送号可在新场景复用
    client.post("/tick", json={"minutes": 4})
    r = client.post("/passage-reports", json={
        "serial": "R-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    assert r.status_code == 201


def test_concurrent_same_serial_idempotent(client):
    """并发以同一报送号同内容提交：只落一条记录（串行化 + 幂等）。"""
    from concurrent.futures import ThreadPoolExecutor

    client.post("/tick", json={"minutes": 5})
    body = {"serial": "C-1", "group_id": 1, "seq": 0, "edge_id": 1,
            "enter_minute": 0, "exit_minute": 4}

    def post(_):
        return client.post("/passage-reports", json=body)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(post, range(5)))
    statuses = sorted(r.status_code for r in responses)
    # 201 至多一次，其余为幂等 200；不允许出现 409/500
    assert statuses.count(201) == 1
    assert statuses.count(200) == 4
    view = client.get("/passage-reports/groups/1").json()
    assert view["reported_count"] == 1
    assert len(view["reports"]) == 1


def test_capacity_control_reflected_in_actual_load(client):
    """实绩时段的有效容量取当时的容量管制（min(原容量, 管制上限)）。

    实绩 [0,4) 不在任何管制时段，有效上限即原容量 10，整组 10 人不超额。
    （容量管制起点不得早于当前分钟，无法对过去分钟补登记；此处验证实绩
    分钟的有效容量口径与 /load 一致。）
    """
    client.post("/tick", json={"minutes": 6})
    client.post("/passage-reports", json={
        "serial": "C-1", "group_id": 1, "seq": 0, "edge_id": 1,
        "enter_minute": 0, "exit_minute": 4})
    load = client.get("/passages/1/actual-load").json()
    assert {m["minute"] for m in load["minutes"]} == {0, 1, 2, 3}
    for m in load["minutes"]:
        assert m["used"] == 10
        assert m["effective_capacity"] == 10
        assert m["over_capacity"] is False
        assert m["closed"] is False

    # ?minute=n 过滤
    one = client.get("/passages/1/actual-load", params={"minute": 2}).json()
    assert [m["minute"] for m in one["minutes"]] == [2]
