"""指标发布归档 / 下线记录 / 数据服务生效边界测试。

覆盖：归档前引用面核对、下线记录登记、在途调用结案、归档后新调用拒绝、
半截明细不交付、已结案响应历史依据可查，以及归档不影响其他指标。
"""
from __future__ import annotations

import pytest

from app.services.data_service import DataServiceService
from app.services.metric_archive import MetricArchiveService
from app.services.query_builder import QueryBuilderService
from app.store import store


def _seed_metric(name: str) -> int:
    metric_id = store.next_id("metric_definition")
    store.rows("metric_definition").append({
        "id": metric_id, "status": "已发布", "pending": False, "abnormal": False,
        "指标名称": name, "计算口径": f"{name}口径", "统计粒度": "日",
        "数据源": "dw", "业务域": "测试",
    })
    store.rows("caliber_version").append({
        "id": store.next_id("caliber_version"), "所属指标": name, "版本号": "v1.0",
        "口径描述": f"{name}口径", "变更类型": "初始创建",
        "生效时间": "2026-11-01", "status": "已生效",
        "pending": False, "abnormal": False,
    })
    return metric_id


def _seed_template(name: str, metric_name: str) -> int:
    template_id = store.next_id("query_builder")
    store.rows("query_builder").append({
        "id": template_id, "模板名称": name, "关联指标": metric_name,
        "维度组合": "日", "过滤条件": "", "缓存策略": "",
        "status": "已发布", "pending": False, "abnormal": False,
    })
    return template_id


def _seed_service(name: str, metric_name: str) -> int:
    api_id = store.next_id("data_service")
    store.rows("data_service").append({
        "id": api_id, "接口名称": name, "关联指标或模型": metric_name,
        "请求方式": "GET", "限流配额": "100/min", "发布状态": "已发布",
        "status": "已发布", "pending": False, "abnormal": False,
    })
    return api_id


@pytest.fixture()
def archive_scene():
    store.reset()
    metric_id = _seed_metric("归档指标A")
    template_1 = _seed_template("归档模板1", "归档指标A")
    template_2 = _seed_template("归档模板2", "归档指标A")
    api_id = _seed_service("归档服务A", "归档指标A")

    other_metric_id = _seed_metric("在用指标B")
    other_template = _seed_template("在用模板B", "在用指标B")
    other_api = _seed_service("在用服务B", "在用指标B")

    return {
        "metric_id": metric_id, "templates": [template_1, template_2],
        "api_id": api_id, "other_metric_id": other_metric_id,
        "other_template": other_template, "other_api": other_api,
    }


def test_references_and_archive_record(archive_scene):
    svc = MetricArchiveService()
    refs = svc.find_references("归档指标A")
    assert len(refs["query_templates"]) == 2
    assert refs["data_services"][0]["id"] == archive_scene["api_id"]

    record, msg = svc.archive_metric(archive_scene["metric_id"], reason="口径下线")
    assert record is not None, msg
    assert record["status"] == "已归档"
    assert record["在途调用数"] == 0
    assert record["生效边界"] >= 1

    metric = store.find("metric_definition", archive_scene["metric_id"])
    assert metric["status"] == "已停用"

    # 同一个指标不能重复归档
    again, msg = svc.archive_metric(archive_scene["metric_id"])
    assert again is None and "已归档" in msg


def test_archive_requires_published_metric():
    store.reset()
    metric_id = store.next_id("metric_definition")
    store.rows("metric_definition").append({
        "id": metric_id, "status": "草稿", "pending": True, "abnormal": False,
        "指标名称": "草稿指标", "计算口径": "x", "统计粒度": "日",
        "数据源": "dw", "业务域": "测试",
    })
    record, msg = MetricArchiveService().archive_metric(metric_id)
    assert record is None and "尚未发布" in msg


def test_inflight_call_before_second_template_cannot_close(archive_scene):
    """服务已取得第一份模板结果、还在等第二份时指标下线，本次调用无法结案。"""
    t1, t2 = archive_scene["templates"]
    result = DataServiceService().call_service(
        archive_scene["api_id"], operator="在途客户端",
        template_sequence=[t1, t2], halt_before_template=t2,
    )

    assert result["code"] == "metric_offline"
    assert result["call_status"] == "无法结案"
    assert result["completed_templates"] == [t1]
    assert result["abandoned_templates"] == [t2]
    # 第一份的口径和明细保留，但不作为数据交付
    assert str(t1) in result["calibers"]
    assert result["retained_details"][0]["template_id"] == t1
    assert result["retained_row_count"] > 0
    assert result["delivered_rows"] == 0
    assert "query_results" not in result

    # 已结束的调用记录继续可查，历史依据完整
    calls = store.service_calls(archive_scene["api_id"])
    assert len(calls) == 1
    call = calls[0]
    assert call["状态"] == "无法结案"
    assert call["已完成模板"] == [t1]
    assert call["待执行模板"] == []
    assert call["已放弃模板"] == [t2]
    assert call["明细"][0]["row_count"] == result["retained_row_count"]
    assert call["结案时间"]

    # 不能借 resume 继续取已下线指标
    resumed = DataServiceService().call_service(
        archive_scene["api_id"], resume_call_id=call["id"])
    assert resumed["code"] == "metric_offline"
    assert resumed["delivered_rows"] == 0


def test_new_calls_after_archive_are_rejected(archive_scene):
    t1, _ = archive_scene["templates"]
    DataServiceService().call_service(archive_scene["api_id"],
                                      template_sequence=[t1])
    MetricArchiveService().archive_metric(archive_scene["metric_id"])

    result = DataServiceService().call_service(archive_scene["api_id"],
                                               operator="归档后客户端")
    assert result["code"] == "metric_offline"
    assert result["call_status"] == "已拒绝"
    assert result["delivered_rows"] == 0

    # 查询构建器同样拒绝已下线指标，且不会命中旧缓存
    query = QueryBuilderService().execute_query(t1)
    assert query["code"] == "metric_offline"

    # 拒绝也要留下调用记录
    calls = store.service_calls(archive_scene["api_id"])
    assert calls[-1]["状态"] == "已拒绝"


def test_archive_with_no_result_rejects_inflight_call(archive_scene):
    t1, t2 = archive_scene["templates"]
    # 模拟一个已发起、尚未执行任何模板的在途调用
    api = store.find("data_service", archive_scene["api_id"])
    call = store.open_service_call(api, "在途客户端", [t1, t2])

    record, _ = MetricArchiveService().archive_metric(archive_scene["metric_id"])
    assert record["在途调用数"] == 1
    assert call["状态"] == "已拒绝"
    assert call["明细"] == []


def test_completed_call_keeps_evidence_and_other_metrics_available(archive_scene):
    t1, _ = archive_scene["templates"]
    svc = DataServiceService()
    completed = svc.call_service(archive_scene["api_id"], template_sequence=[t1])
    assert completed["call_status"] == "已完成"
    assert completed["delivered_rows"] > 0
    completed_id = completed["call_id"]

    MetricArchiveService().archive_metric(archive_scene["metric_id"])

    # 已完成的响应不因归档被改写
    calls = {c["id"]: c for c in store.service_calls(archive_scene["api_id"])}
    assert calls[completed_id]["状态"] == "已完成"
    assert calls[completed_id]["明细"]

    # 其他指标不受影响
    other = svc.call_service(archive_scene["other_api"],
                             template_sequence=[archive_scene["other_template"]])
    assert other["call_status"] == "已完成"
    assert other["delivered_rows"] > 0
    assert "error" not in other


def test_archive_is_serialized_against_inflight_calls(archive_scene):
    """归档推进边界与在途调用结算在同一把锁内，不会出现两边各结一半。"""
    from concurrent.futures import ThreadPoolExecutor

    t1, t2 = archive_scene["templates"]
    # 预热缓存，排除首次计算噪声
    QueryBuilderService().execute_query(t1)

    def do_archive():
        return MetricArchiveService().archive_metric(archive_scene["metric_id"])

    def do_call():
        return DataServiceService().call_service(
            archive_scene["api_id"], template_sequence=[t1, t2], hold_open=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive_future = pool.submit(do_archive)
        call_future = pool.submit(do_call)
        record, _ = archive_future.result()
        started = call_future.result()

    assert record is not None
    # 不论调用是归档前还是归档后进入，都不能成功结案拿到完整两份模板
    assert started.get("call_status") != "已完成"
    assert started.get("delivered_rows", 0) == 0

    calls = store.service_calls(archive_scene["api_id"])
    assert all(c["状态"] in ("已拒绝", "无法结案") for c in calls)
    # 落库的模板数不超过一份
    assert sum(len(c["明细"]) for c in calls) <= 1


def test_boundary_ordering(archive_scene):
    """生效边界单调递增，后归档的指标边界更大。"""
    first, _ = MetricArchiveService().archive_metric(archive_scene["metric_id"])
    second, _ = MetricArchiveService().archive_metric(
        archive_scene["other_metric_id"])
    assert first["生效边界"] < second["生效边界"]
    assert store.metric_offline_boundary("归档指标A") == first["生效边界"]
    assert store.metric_offline_boundary("在用指标B") == second["生效边界"]


def test_hold_open_then_archive_then_resume(archive_scene):
    """跨请求在途：先保持调用在途，另一请求归档，再恢复执行第二份模板。"""
    t1, t2 = archive_scene["templates"]
    svc = DataServiceService()
    started = svc.call_service(archive_scene["api_id"], template_sequence=[t1, t2],
                               hold_open=True, operator="客户端A")
    assert started["call_status"] == "计算中"
    assert started["completed_templates"] == [t1]
    assert started["pending_templates"] == [t2]
    assert started["delivered_rows"] == 0

    MetricArchiveService().archive_metric(archive_scene["metric_id"])

    resumed = svc.call_service(archive_scene["api_id"],
                               resume_call_id=started["call_id"])
    assert resumed["code"] == "metric_offline"
    assert resumed["call_status"] == "无法结案"
    assert resumed["delivered_rows"] == 0
    # 不能通过恢复新开一条调用记录
    assert len(store.service_calls(archive_scene["api_id"])) == 1

    # 历史依据仍可查
    call = store.service_calls(archive_scene["api_id"])[0]
    assert call["状态"] == "无法结案"
    assert call["明细"][0]["template_id"] == t1


def test_archived_metric_unavailable_in_report(archive_scene):
    report_id = store.next_id("report_template")
    store.rows("report_template").append({
        "id": report_id, "模板名称": "指标下线报告",
        "包含指标集": "归档指标A,在用指标B", "报告周期": "每日",
        "输出格式": "HTML", "分发渠道": "邮件",
        "status": "已发布", "pending": False, "abnormal": False,
    })
    MetricArchiveService().archive_metric(archive_scene["metric_id"])

    from app.services.report_template import ReportTemplateService
    report = ReportTemplateService().generate_report(report_id)
    sections = {s["metric"]: s for s in report["sections"]}
    assert sections["归档指标A"]["available"] is False
    assert sections["归档指标A"]["archive_id"]
    assert sections["在用指标B"]["available"] is True
    assert report["unavailable_metrics"] == 1
