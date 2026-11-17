import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c, username="admin", password="123456"):
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _slice(c, h, sid):
    return next(s for s in c.get("/api/slices", headers=h).json()["items"] if s["id"] == sid)


def _wm_count(c, h, slice_no):
    return sum(1 for w in c.get("/api/watermarks", headers=h).json()["items"]
               if w["source_slice"] == slice_no)


def test_pipeline():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "日切放行"
    h = _login(c)
    assert len(c.get("/api/slices", headers=h).json()["items"]) >= 1
    # partial cannot release
    bad = c.post("/api/slices/1/release", headers=h, json={"consumer": "x"})
    assert bad.status_code == 409
    # register complete and release
    ok = c.post("/api/slices", headers=h, json={"slice_no": "SL-T-1", "biz_date": "2026-11-04",
                                                "partition_key": "mid", "row_count": 10, "expected_count": 10})
    assert ok.status_code == 200
    sid = ok.json()["id"]
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 200
    assert any(w["source_slice"] == "SL-T-1" for w in c.get("/api/watermarks", headers=h).json()["items"])


def test_caliber_is_pinned_for_the_day():
    c = TestClient(app)
    h = _login(c)
    p = c.get("/api/policy", headers=h).json()["policy"]
    assert p["rule"] == "甲" and p["locked"] is True
    # 当天换口径必须被拒并记事件，库里的口径不变
    r = c.post("/api/policy", headers=h, json={"rule": "乙", "note": "试换"})
    assert r.status_code == 409
    assert c.get("/api/policy", headers=h).json()["policy"]["rule"] == "甲"
    events = c.get("/api/policy", headers=h).json()["events"]
    assert any(e["status"] == "期望口径偏了" and e["attempted_rule"] == "乙" for e in events)


def test_raising_expectation_voids_old_complete_and_blocks_release():
    c = TestClient(app)
    h = _login(c)
    # 种子切片 2：1000/1000 完整、已放行
    s = _slice(c, h, 2)
    assert s["completeness"] == "完整" and s["gate"] == "已放行"

    before_wm = _wm_count(c, h, "SL-2026-11-01-west")
    r = c.post("/api/slices/2/expectation", headers=h,
               json={"expected_count": 1200, "expected_rev": 1, "reason": "上游补口径"})
    assert r.status_code == 200, r.text
    assert r.json()["expected_rev"] == 2 and r.json()["recut_required"] is True

    # 期望变更必须真实写入重裁状态，而不是只改提示
    s = _slice(c, h, 2)
    assert s["expected_count"] == 1200
    assert s["expected_rev"] == 2
    assert s["evaluation_rev"] == 1
    assert s["recut_required"] is True
    assert s["completeness"] == "待重裁"
    assert s["gate"] == "待重裁"

    # 旧完整标记不得直接放行，也不得推水位
    rel = c.post("/api/slices/2/release", headers=h, json={"consumer": "ads_order"})
    assert rel.status_code == 409
    assert "重裁" in rel.json()["detail"]
    assert _wm_count(c, h, "SL-2026-11-01-west") == before_wm
    releases = c.get("/api/releases", headers=h).json()["items"]
    assert releases[0]["status"] == "拒绝" and releases[0]["expected_rev"] == 2

    # 按新期望重裁：980/1200 仍然不完整，还是不能放行
    ev = c.post("/api/slices/2/evaluate", headers=h)
    assert ev.status_code == 200
    assert ev.json()["completeness"] == "部分完整"
    assert c.post("/api/slices/2/release", headers=h, json={"consumer": "ads_order"}).status_code == 409
    assert _wm_count(c, h, "SL-2026-11-01-west") == before_wm


def test_recut_after_expectation_change_allows_release():
    c = TestClient(app)
    h = _login(c)
    s = c.post("/api/slices", headers=h, json={"slice_no": "SL-T-2", "biz_date": "2026-11-04",
                                               "partition_key": "mid", "row_count": 1000,
                                               "expected_count": 1000}).json()
    sid = s["id"]
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"}).status_code == 200

    r = c.post(f"/api/slices/{sid}/expectation", headers=h,
               json={"expected_count": 1200, "expected_rev": 1})
    assert r.status_code == 200
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_y"}).status_code == 409

    # 迟到数据补齐后重裁，才恢复“可放行”
    arr = c.post(f"/api/slices/{sid}/arrival", headers=h, json={"row_count": 1200})
    assert arr.status_code == 200 and arr.json()["recut_required"] is True
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_y"}).status_code == 409

    ev = c.post(f"/api/slices/{sid}/evaluate", headers=h)
    assert ev.status_code == 200 and ev.json()["completeness"] == "完整"
    s = _slice(c, h, sid)
    assert s["gate"] == "可放行" and s["evaluation_rev"] == s["expected_rev"] == 2
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_y"})
    assert rel.status_code == 200
    wm = c.get("/api/watermarks", headers=h).json()["items"]
    assert wm[0]["source_slice"] == "SL-T-2" and wm[0]["expected_rev"] == 2


def test_recut_catalog_detail_and_gate_agree():
    c = TestClient(app)
    h = _login(c)
    assert c.post("/api/slices/2/expectation", headers=h,
                  json={"expected_count": 1500, "expected_rev": 1}).status_code == 200

    listed = _slice(c, h, 2)
    catalog = c.get("/api/recut", headers=h).json()
    in_catalog = next(x for x in catalog["items"] if x["id"] == 2)
    detail = c.get("/api/slices/2/expectation", headers=h).json()["slice"]

    for view in (listed, in_catalog, detail):
        assert view["gate"] == "待重裁"
        assert view["completeness"] == "待重裁"
        assert view["recut_required"] is True
        assert view["expected_rev"] == 2
        assert "期望v2" in view["note"]
    # 重裁目录必须给出重裁写入记录（不是只改提示）
    assert any(e["slice_no"] == "SL-2026-11-01-west" and e["completeness"] == "待重裁"
               and e["source"] == "期望变更" for e in catalog["recent"])

    # 重裁后三处同步恢复一致
    assert c.post("/api/slices/2/evaluate", headers=h).status_code == 200
    listed = _slice(c, h, 2)
    catalog = c.get("/api/recut", headers=h).json()
    detail = c.get("/api/slices/2/expectation", headers=h).json()["slice"]
    assert not any(x["id"] == 2 for x in catalog["items"])
    for view in (listed, detail):
        assert view["recut_required"] is False
        assert view["evaluation_rev"] == view["expected_rev"]
    assert listed["gate"] == "未完整"


def test_concurrent_same_slice_expectation_change_is_exclusive():
    c = TestClient(app)
    h1 = _login(c)
    tok2 = c.post("/api/login", json={"username": "viewer", "password": "view123"}).json()["token"]
    h2 = {"Authorization": f"Bearer {tok2}"}

    barrier = threading.Barrier(2)
    results = []

    def submit(headers):
        client = TestClient(app)
        barrier.wait()
        r = client.post("/api/slices/2/expectation", headers=headers,
                        json={"expected_count": 1300, "expected_rev": 1})
        results.append(r.status_code)

    t1 = threading.Thread(target=submit, args=(h1,))
    t2 = threading.Thread(target=submit, args=(h2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409]
    logs = c.get("/api/expectations", headers=h1).json()["items"]
    assert any(x["status"] == "已生效" for x in logs)
    assert any(x["status"] == "期望争用" for x in logs)
    # 后手不得静默成功，也不得半更新切片字段
    s = _slice(c, h1, 2)
    assert s["expected_rev"] == 2
    assert s["expected_count"] == 1300


def test_stale_rev_is_rejected_without_mutation():
    c = TestClient(app)
    h = _login(c)
    assert c.post("/api/slices/2/expectation", headers=h,
                  json={"expected_count": 1200, "expected_rev": 1}).status_code == 200
    bad = c.post("/api/slices/2/expectation", headers=h,
                 json={"expected_count": 1400, "expected_rev": 1})
    assert bad.status_code == 409 and "期望争用" in bad.json()["detail"]
    s = _slice(c, h, 2)
    assert s["expected_count"] == 1200 and s["expected_rev"] == 2
    logs = c.get("/api/expectations", headers=h).json()["items"]
    assert logs[0]["status"] == "期望争用" and logs[0]["new_expected"] == 1400


def test_deviation_from_caliber_statement_is_rejected_and_logged():
    c = TestClient(app)
    h = _login(c)
    r = c.post("/api/slices/2/expectation", headers=h,
               json={"expected_count": 1200, "expected_rev": 1, "rule": "乙"})
    assert r.status_code == 409 and "期望口径偏了" in r.json()["detail"]
    s = _slice(c, h, 2)
    assert s["expected_count"] == 1000 and s["expected_rev"] == 1
    logs = c.get("/api/expectations", headers=h).json()["items"]
    assert logs[0]["status"] == "期望口径偏了"
    assert logs[0]["rule"] == "乙"
    assert "甲规则" in logs[0]["statement"]


def test_expectation_change_is_recorded_with_rule_and_statement():
    c = TestClient(app)
    h = _login(c)
    assert c.post("/api/slices/2/expectation", headers=h,
                  json={"expected_count": 2000, "expected_rev": 1,
                        "reason": "口径上调"}).status_code == 200
    log = c.get("/api/expectations", headers=h).json()["items"][0]
    assert log["status"] == "已生效" and log["rule"] == "甲"
    assert log["old_expected"] == 1000 and log["new_expected"] == 2000
    assert "旧完整标记" in log["statement"]
    assert log["operator"] == "admin" and "口径上调" in log["note"]


def test_arrival_resets_release_gate():
    c = TestClient(app)
    h = _login(c)
    sid = c.post("/api/slices", headers=h, json={"slice_no": "SL-T-3", "biz_date": "2026-11-05",
                                                 "partition_key": "mid", "row_count": 10,
                                                 "expected_count": 10}).json()["id"]
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"}).status_code == 200
    # 迟到重灌导致实收变化：旧结论作废，必须重裁
    assert c.post(f"/api/slices/{sid}/arrival", headers=h, json={"row_count": 8}).status_code == 200
    s = _slice(c, h, sid)
    assert s["recut_required"] is True and s["gate"] == "待重裁"
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_z"}).status_code == 409


def test_health_reports_pinned_caliber():
    c = TestClient(app)
    body = c.get("/api/health").json()
    assert body["caliber"] == "甲"
    assert body["db"].endswith("t.db")


def test_missing_expected_rev_is_contention_not_success():
    c = TestClient(app)
    h = _login(c)
    r = c.post("/api/slices/2/expectation", headers=h, json={"expected_count": 1200})
    assert r.status_code == 409 and "期望争用" in r.json()["detail"]
    s = _slice(c, h, 2)
    assert s["expected_count"] == 1000 and s["expected_rev"] == 1
    log = c.get("/api/expectations", headers=h).json()["items"][0]
    assert log["status"] == "期望争用"


def test_statement_mismatch_is_rejected_as_caliber_deviation():
    c = TestClient(app)
    h = _login(c)
    pinned = c.get("/api/policy", headers=h).json()["policy"]["statement"]
    # 与钉死说明逐字一致：允许
    ok = c.post("/api/slices/2/expectation", headers=h,
                json={"expected_count": 1200, "expected_rev": 1, "statement": pinned})
    assert ok.status_code == 200
    # 混用另一种写法：拒绝并记口径偏了
    bad = c.post("/api/slices/2/expectation", headers=h,
                 json={"expected_count": 1400, "expected_rev": 2,
                       "statement": "乙规则：保留行数快照只读对照"})
    assert bad.status_code == 409 and "期望口径偏了" in bad.json()["detail"]
    s = _slice(c, h, 2)
    assert s["expected_count"] == 1200 and s["expected_rev"] == 2
    logs = c.get("/api/expectations", headers=h).json()["items"]
    assert logs[0]["status"] == "期望口径偏了" and "乙规则" in logs[0]["statement"]


def test_watermarks_below_expectation_are_surfaced_not_trusted():
    c = TestClient(app)
    h = _login(c)
    detail = c.get("/api/slices/2/expectation", headers=h).json()
    assert detail["watermarks_below_expectation"] == []
    assert c.post("/api/slices/2/expectation", headers=h,
                  json={"expected_count": 1800, "expected_rev": 1}).status_code == 200
    detail = c.get("/api/slices/2/expectation", headers=h).json()
    stale = detail["watermarks_below_expectation"]
    assert len(stale) == 1 and stale[0]["expected_rev"] == 1
    catalog = c.get("/api/recut", headers=h).json()
    assert any(w["source_slice"] == "SL-2026-11-01-west" for w in catalog["watermarks_below_expectation"])
    # 旧水位不得被当成新期望下的放行依据
    assert c.post("/api/slices/2/release", headers=h, json={"consumer": "ads_order"}).status_code == 409


def test_stale_watermark_clears_once_released_at_new_expectation():
    c = TestClient(app)
    h = _login(c)
    assert c.post("/api/slices/2/expectation", headers=h,
                  json={"expected_count": 1100, "expected_rev": 1}).status_code == 200
    assert len(c.get("/api/slices/2/expectation", headers=h).json()["watermarks_below_expectation"]) == 1
    # 补齐并按新期望重裁通过后，新水位带着 v2 覆盖旧水位，对照项消失
    assert c.post("/api/slices/2/arrival", headers=h, json={"row_count": 1100}).status_code == 200
    assert c.post("/api/slices/2/evaluate", headers=h).status_code == 200
    assert c.post("/api/slices/2/release", headers=h, json={"consumer": "ads_order"}).status_code == 200
    detail = c.get("/api/slices/2/expectation", headers=h).json()
    assert detail["watermarks_below_expectation"] == []
    assert detail["slice"]["expected_rev"] == 2
    assert c.get("/api/recut", headers=h).json()["watermarks_below_expectation"] == []
