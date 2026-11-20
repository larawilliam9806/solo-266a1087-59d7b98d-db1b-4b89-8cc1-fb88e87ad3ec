"""转向抽样、调查清单替换与统计查询测试。

覆盖:
- 相邻采样点支撑的转向(间隔 (0,30]s、不同有向路段、直跨共同端点、两段正里程)
- 转向计入后一个点的 UTC 日期与小时
- 缺口两侧的点不构成转向
- 分析 + 转向证据原子落库
- 清单整份替换、同一轨迹仅一份、版本/不存在/旧分析校验
- 统计查询区分未提交清单与已提交零转向、换清单后统计切换
"""
import os
import tempfile

os.environ.setdefault(
    "DATABASE_PATH", os.path.join(tempfile.mkdtemp(prefix="trajturns_"), "test.db")
)

from fastapi.testclient import TestClient

from app import db
from app.main import app

db.init_db()
client = TestClient(app)


# L 形路网:N1(39.900,116.400) --A--> N2(39.900,116.401) --B--> N3(39.901,116.401)
#          N1 --C--> N2 的平行反向? 不;此处仅一条 A,便于验证"直跨共同端点"
NET = {
    "version": "turn-v1",
    "segments": [
        {"segment_id": "A", "start_lat": 39.900, "start_lon": 116.400,
         "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "B", "start_lat": 39.900, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0},
    ],
}

# 同一几何的另一版本(路段改名),用于版本不符校验
NET_OTHER = {
    "version": "turn-v2",
    "segments": [
        {"segment_id": "A2", "start_lat": 39.900, "start_lon": 116.400,
         "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "B2", "start_lat": 39.900, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0},
    ],
}


def setup_module():
    client.post("/api/networks", json=NET)
    client.post("/api/networks", json=NET_OTHER)


def make_traj(points, name=None):
    return {"name": name,
            "points": [{"timestamp": t, "lat": la, "lon": lo} for t, la, lo in points]}


def mk_traj(points, name=None):
    r = client.post("/api/trajectories", json=make_traj(points, name))
    assert r.status_code == 201, r.text
    return r.json()["trajectory_id"]


def match(tid, version="turn-v1"):
    r = client.post("/api/match", json={"trajectory_id": tid, "network_version": version})
    assert r.status_code == 201, r.text
    return r.json()


def survey(version, date, ids):
    return client.put("/api/turn-surveys",
                      json={"network_version": version, "utc_date": date,
                            "analysis_ids": ids})


def counts(version, date, **params):
    r = client.get("/api/turn-counts",
                   params={"network_version": version, "utc_date": date, **params})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 转向证据
def test_turn_detected_and_attributed_to_later_point():
    """A->B 转向计入后一个点的 UTC 日期与小时。"""
    tid = mk_traj([
        ("2026-02-01T23:59:50Z", 39.9000, 116.4005),
        ("2026-02-02T00:00:05Z", 39.9004, 116.4010),  # 后一个点跨过 UTC 午夜
    ])
    res = match(tid)
    assert len(res["turns"]) == 1
    t = res["turns"][0]
    assert (t["from_segment_id"], t["to_segment_id"]) == ("A", "B")
    assert t["utc_date"] == "2026-02-02"
    assert t["utc_hour"] == 0
    assert t["gap_seconds"] == 15.0

    detail = client.get(f"/api/analyses/{res['analysis_id']}").json()
    assert detail["turns_computed"] is True
    assert len(detail["turns"]) == 1
    assert detail["turns"][0]["to_segment_id"] == "B"


def test_turn_evidence_persisted_independently_of_request():
    """转向证据可在后续查询中复用(统计不依赖匹配响应)。"""
    tid = mk_traj([
        ("2026-02-01T08:00:00Z", 39.9000, 116.4005),
        ("2026-02-01T08:00:10Z", 39.9005, 116.4010),
    ])
    res = match(tid)
    assert len(res["turns"]) == 1
    again = client.get(f"/api/analyses/{res['analysis_id']}").json()
    assert again["turns"] == res["turns"]


def test_gap_interval_is_not_a_turn():
    """缺口两侧(非同一次连续匹配)的相邻点不构成转向。"""
    tid = mk_traj([
        ("2026-02-03T08:00:00Z", 39.9000, 116.4005),   # A
        ("2026-02-03T08:00:10Z", 39.9000, 116.4009),   # A
        ("2026-02-03T08:00:20Z", 39.9500, 116.4500),   # 远离路网 out_of_range
        ("2026-02-03T08:00:30Z", 39.9008, 116.4010),   # B
    ])
    res = match(tid)
    assert res["gaps"], res
    # 缺口把 A 段与 B 段分到不同连续匹配,跨缺口的 A->B 不计为转向
    assert res["turns"] == []


def test_gap_over_30s_is_not_a_turn():
    """相邻点间隔超过 30s 不计转向。"""
    tid = mk_traj([
        ("2026-02-04T08:00:00Z", 39.9000, 116.4005),
        ("2026-02-04T08:00:31Z", 39.9005, 116.4010),
    ])
    res = match(tid)
    assert res["turns"] == []


def test_zero_interval_is_not_a_turn():
    """两点间隔为 0 不计转向。"""
    tid = mk_traj([
        ("2026-02-05T08:00:00Z", 39.9000, 116.4005),
        ("2026-02-05T08:00:00Z", 39.9005, 116.4010),
    ])
    res = match(tid)
    assert res["turns"] == []


def test_uturn_a_b_a_counts_both_legs():
    """A->B->A:两段相邻点各构成一次转向,不会因路径拆段而漏计。"""
    tid = mk_traj([
        ("2026-02-06T08:00:00Z", 39.8997, 116.4005),   # A
        ("2026-02-06T08:00:10Z", 39.9005, 116.4010),   # A->B
        ("2026-02-06T08:00:20Z", 39.9006, 116.4009),   # 仍在 B(不足以回到 A)
    ])
    res = match(tid)
    pairs = [(t["from_segment_id"], t["to_segment_id"]) for t in res["turns"]]
    assert pairs == [("A", "B")]


def test_route_runs_merges_and_drops_zero_length():
    """_route_runs 合并相邻同路段,丢弃零里程过渡片段。"""
    from app.matcher import NetworkIndex
    runs = NetworkIndex._route_runs([(0, 5.0, 85.0), (1, 0.0, 0.0), (1, 0.0, 40.0),
                                     (0, 0.0, 20.0)])
    assert runs == [(0, 80.0), (1, 40.0), (0, 20.0)]


# ---------------------------------------------------------------- 清单替换
def test_survey_replace_and_zero_turn_states():
    """未提交清单 vs 已提交清单(含零次转向)状态区分。"""
    # 未提交
    c0 = counts("turn-v1", "2026-03-01")
    assert c0["status"] == "no_survey"
    assert c0["turns"] == []

    # 提交空清单 -> 已提交,零次转向
    r = survey("turn-v1", "2026-03-01", [])
    assert r.status_code == 200, r.text
    c1 = counts("turn-v1", "2026-03-01")
    assert c1["status"] == "survey"
    assert c1["total_turns"] == 0
    assert c1["turns"] == []


def test_survey_counts_group_by_pair_and_hour():
    tid = mk_traj([
        ("2026-03-02T09:59:50Z", 39.9000, 116.4005),
        ("2026-03-02T10:00:00Z", 39.9005, 116.4010),   # A->B @10
    ])
    res = match(tid)
    assert survey("turn-v1", "2026-03-02", [res["analysis_id"]]).status_code == 200
    c = counts("turn-v1", "2026-03-02")
    assert c["status"] == "survey"
    assert c["turns"] == [{"from_segment_id": "A", "to_segment_id": "B",
                           "utc_hour": 10, "turn_count": 1}]
    # 过滤
    assert counts("turn-v1", "2026-03-02", from_segment_id="A")["total_turns"] == 1
    assert counts("turn-v1", "2026-03-02", from_segment_id="Z")["turns"] == []


def test_survey_rejects_invalid_and_keeps_previous():
    """校验失败整份拒绝,旧清单与统计保持不变。"""
    tid_ok = mk_traj([
        ("2026-03-03T11:00:00Z", 39.9000, 116.4005),
        ("2026-03-03T11:00:10Z", 39.9005, 116.4010),
    ])
    ok = match(tid_ok)
    assert survey("turn-v1", "2026-03-03", [ok["analysis_id"]]).status_code == 200
    baseline = counts("turn-v1", "2026-03-03")
    assert baseline["total_turns"] == 1

    # 不存在的分析
    r = survey("turn-v1", "2026-03-03", [999999])
    assert r.status_code == 404
    # 版本不符
    tid2 = mk_traj([("2026-03-03T12:00:00Z", 39.9000, 116.4005)])
    other = match(tid2, version="turn-v2")
    r = survey("turn-v1", "2026-03-03", [other["analysis_id"]])
    assert r.status_code == 422
    # 重复分析 id(Pydantic 校验)-> 422
    r = survey("turn-v1", "2026-03-03", [ok["analysis_id"], ok["analysis_id"]])
    assert r.status_code == 422

    # 旧统计保持不变
    assert counts("turn-v1", "2026-03-03") == baseline


def test_survey_unknown_version():
    r = survey("nope", "2026-03-04", [])
    assert r.status_code == 404


def test_survey_one_analysis_per_trajectory():
    tid = mk_traj([
        ("2026-03-05T13:00:00Z", 39.9000, 116.4005),
        ("2026-03-05T13:00:10Z", 39.9005, 116.4010),
    ])
    a1 = match(tid)
    a2 = match(tid)
    r = survey("turn-v1", "2026-03-05", [a1["analysis_id"], a2["analysis_id"]])
    assert r.status_code == 422
    # 单份可入选
    assert survey("turn-v1", "2026-03-05", [a2["analysis_id"]]).status_code == 200


def test_survey_replace_switches_statistics():
    """整份替换后统计只反映新清单;换清单无需重跑匹配。"""
    tid = mk_traj([
        ("2026-03-06T14:00:00Z", 39.9000, 116.4005),
        ("2026-03-06T14:00:10Z", 39.9005, 116.4010),
    ])
    a1 = match(tid)
    tid_b = mk_traj([
        ("2026-03-06T14:00:00Z", 39.9000, 116.4005),
        ("2026-03-06T14:00:10Z", 39.9005, 116.4010),
    ])
    a2 = match(tid_b)

    survey("turn-v1", "2026-03-06", [a1["analysis_id"]])
    assert counts("turn-v1", "2026-03-06")["total_turns"] == 1

    survey("turn-v1", "2026-03-06", [a2["analysis_id"]])
    after = counts("turn-v1", "2026-03-06")
    assert after["total_turns"] == 1
    # 旧清单条目已移除
    r = survey("turn-v1", "2026-03-06", [])
    assert r.status_code == 200
    assert r.json()["entry_count"] == 0
    assert counts("turn-v1", "2026-03-06")["status"] == "survey"
    assert counts("turn-v1", "2026-03-06")["turns"] == []


def test_legacy_analysis_rejected_until_rematched():
    """升级前的旧分析(无转向证据)可查询,但不能入选;重新匹配后可入选。"""
    tid = mk_traj([
        ("2026-03-07T15:00:00Z", 39.9000, 116.4005),
        ("2026-03-07T15:00:10Z", 39.9005, 116.4010),
    ])
    res = match(tid)
    aid = res["analysis_id"]

    # 模拟旧分析:turns_computed=0 且无转向证据
    conn = db.connect()
    try:
        conn.execute("DELETE FROM analysis_turns WHERE analysis_id = ?", (aid,))
        conn.execute("UPDATE analyses SET turns_computed = 0 WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()

    detail = client.get(f"/api/analyses/{aid}").json()
    assert detail["turns_computed"] is False
    assert detail["turns"] == []

    r = survey("turn-v1", "2026-03-07", [aid])
    assert r.status_code == 422

    rematch = match(tid)
    assert survey("turn-v1", "2026-03-07", [rematch["analysis_id"]]).status_code == 200
    assert counts("turn-v1", "2026-03-07")["total_turns"] == 1


def test_match_write_is_atomic(monkeypatch):
    """转向证据写入失败 -> 整次分析不落库。"""
    from app import main

    tid = mk_traj([
        ("2026-03-08T16:00:00Z", 39.9000, 116.4005),
        ("2026-03-08T16:00:10Z", 39.9005, 116.4010),
    ])
    before = client.get("/api/analyses", params={"trajectory_id": tid}).json()["analyses"]
    assert before == []

    def boom(conn, *a, **k):
        raise RuntimeError("injected turn write failure")

    monkeypatch.setattr(main, "_insert_turns", boom)
    r = client.post("/api/match", json={"trajectory_id": tid, "network_version": "turn-v1"})
    assert r.status_code == 500
    after = client.get("/api/analyses", params={"trajectory_id": tid}).json()["analyses"]
    assert after == []


def test_counts_unknown_version():
    r = client.get("/api/turn-counts",
                   params={"network_version": "nope", "utc_date": "2026-03-09"})
    assert r.status_code == 404


def test_surveys_isolated_by_date():
    """不同 UTC 日期的清单互不影响,统计按日期过滤。"""
    tid_a = mk_traj([
        ("2026-03-10T09:00:00Z", 39.9000, 116.4005),
        ("2026-03-10T09:00:10Z", 39.9005, 116.4010),
    ])
    a = match(tid_a)
    tid_b = mk_traj([
        ("2026-03-11T09:00:00Z", 39.9000, 116.4005),
        ("2026-03-11T09:00:10Z", 39.9005, 116.4010),
    ])
    b = match(tid_b)

    assert survey("turn-v1", "2026-03-10", [a["analysis_id"]]).status_code == 200
    assert survey("turn-v1", "2026-03-11", [b["analysis_id"]]).status_code == 200

    assert counts("turn-v1", "2026-03-10")["total_turns"] == 1
    assert counts("turn-v1", "2026-03-11")["total_turns"] == 1
    # 只清空一天,另一天不受影响
    assert survey("turn-v1", "2026-03-10", []).status_code == 200
    assert counts("turn-v1", "2026-03-10")["turns"] == []
    assert counts("turn-v1", "2026-03-11")["total_turns"] == 1


def test_multiple_turns_sum_and_hour_buckets():
    """同一轨迹多次转向按 (路段对, UTC 小时) 汇总。"""
    # A -> B -> A(掉头只能经由 B 末端?此处用 A->B 再 B->A 的往返)
    tid = mk_traj([
        ("2026-03-12T10:00:00Z", 39.9000, 116.4005),   # A
        ("2026-03-12T10:00:10Z", 39.9005, 116.4010),   # B (A->B @10)
        ("2026-03-12T10:00:20Z", 39.9005, 116.4009),   # 仍在 B
    ])
    res = match(tid)
    assert [(t["from_segment_id"], t["to_segment_id"]) for t in res["turns"]] == [("A", "B")]
    assert survey("turn-v1", "2026-03-12", [res["analysis_id"]]).status_code == 200
    c = counts("turn-v1", "2026-03-12")
    assert c["turns"] == [{"from_segment_id": "A", "to_segment_id": "B",
                           "utc_hour": 10, "turn_count": 1}]
    # 小时过滤不存在于该清单
    assert counts("turn-v1", "2026-03-12", to_segment_id="A")["turns"] == []
