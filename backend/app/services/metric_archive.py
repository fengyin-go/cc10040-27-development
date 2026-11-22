"""指标归档领域服务：把「停用指标」升级成有下线流程的指标发布归档。

停用只改指标自身的状态，外部数据服务仍会继续取数。本服务补上下线闭环：

1. 查询指标被哪些查询模板 / 报告模板引用（发布前的影响面核对）；
2. 登记一条指标下线记录，并推进一个独占的生效边界；
3. 通知数据服务：在途调用按记录推进调用状态，归档后发起的新调用拒绝取数。

跨模块数据流: metric_definition -> metric_archive -> query_builder / data_service
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService, metric_of_report_template, service_for
from app.store import store


class MetricArchiveService(BaseModuleService):
    MODULE = "metric_archive"
    ENTITY = "指标下线记录"
    OPERATION_TERM = "归档"
    LIST_FIELDS = [
        "关联指标",
        "下线原因",
        "生效边界",
        "影响模板数",
        "在途调用数",
        "登记人",
        "登记时间",
    ]
    REQUIRED_FIELDS = ["关联指标"]
    STATUS_ORDER = ["待生效", "已归档"]
    # 归档记录只允许登记和推进到「已归档」，不提供回退动作。
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """下线登记不允许绕过归档流程直接插入记录。"""
        return None, ["指标下线记录需通过「发布指标归档」发起，不能直接登记"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return None, f"指标下线记录不支持动作「{action}」，请通过发布指标归档或数据服务调用推进状态"

    # ── 引用面核对 ──

    def find_references(self, metric_name: str) -> dict[str, Any]:
        """列出指标被哪些模板和服务引用，供归档前核对影响面。"""
        query_templates = store.find_by_field("query_builder", "关联指标", metric_name)
        report_templates = [
            row for row in store.rows("report_template")
            if metric_name in metric_of_report_template(row)
        ]
        services = [
            row for row in store.rows("data_service")
            if metric_name in str(row.get("关联指标或模型", ""))
        ]
        # 只有仍在计算中的模板才构成「下半段被截断」的风险。
        active_query_templates = [
            row for row in query_templates if row.get("status") == "已发布"
        ]
        return {
            "metric": metric_name,
            "query_templates": [
                {"id": row.get("id"), "name": row.get("模板名称"),
                 "status": row.get("status")}
                for row in query_templates
            ],
            "report_templates": [
                {"id": row.get("id"), "name": row.get("模板名称"),
                 "status": row.get("status")}
                for row in report_templates
            ],
            "data_services": [
                {"id": row.get("id"), "name": row.get("接口名称"),
                 "status": row.get("status")}
                for row in services
            ],
            "active_query_template_ids": [int(row.get("id", 0))
                                          for row in active_query_templates],
            "total_references": len(query_templates) + len(report_templates) + len(services),
        }

    # ── 发布指标归档 ──

    def archive_metric(self, metric_id: int, reason: str = "",
                       operator: str = "运营") -> tuple[dict[str, Any] | None, str]:
        """登记指标下线记录并把指标切换到已归档。

        生效边界在归档锁内推进：边界之前的调用继续按边界取数，
        边界之后发起的调用一律拒绝。
        """
        with store.lock:
            metric = store.find("metric_definition", metric_id)
            if metric is None:
                return None, f"指标 {metric_id} 不存在"
            if metric.get("status") == "草稿":
                return None, f"指标 {metric_id} 尚未发布，无需归档"
            if store.metric_archive(str(metric.get("指标名称", ""))) is not None:
                return None, f"指标 {metric_id} 已归档，不能重复登记下线"

            metric_name = str(metric.get("指标名称", ""))
            references = self.find_references(metric_name)
            boundary = store.next_boundary()

            record = {
                "id": store.next_id(self.MODULE),
                "关联指标": metric_name,
                "下线原因": reason or "运营发起指标下线",
                "生效边界": boundary,
                "影响模板数": (len(references["query_templates"])
                               + len(references["report_templates"])),
                "在途调用数": 0,
                "登记人": operator,
                "登记时间": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "已归档",
                "pending": False,
                "abnormal": True,
            }
            store.rows(self.MODULE).append(record)

            # 指标状态切到已停用，作为对外的可见状态；真正的取数边界在归档记录里。
            metric["status"] = "已停用"
            metric["pending"] = False
            metric["abnormal"] = True

            store.set_archive_meta(int(record["id"]), {
                "metric_id": metric_id,
                "boundary": boundary,
                "query_template_ids": references["active_query_template_ids"],
                "all_query_template_ids": [t["id"] for t in references["query_templates"]],
                "report_template_ids": [t["id"] for t in references["report_templates"]],
                "service_ids": [t["id"] for t in references["data_services"]],
            })

            # 清理该指标的查询缓存，避免归档后仍从缓存取到旧口径明细。
            invalidated = store.cache_invalidate_by_prefix(f"query:{metric_name}:")

            # 通知数据服务按下线记录推进调用状态。
            data_service = service_for("data_service")
            advanced = []
            if data_service is not None:
                advanced = data_service.advance_calls_for_archive(metric_name, record)

            record["在途调用数"] = len(advanced)

            message = (
                f"指标 {metric_name} 已归档，生效边界={boundary}，"
                f"影响模板 {record['影响模板数']} 个，"
                f"推进在途调用 {len(advanced)} 个，失效缓存 {invalidated} 条"
            )
            return record, message

    # ── 查询 ──

    def get_archive(self, metric_name: str) -> dict[str, Any] | None:
        record = store.metric_archive(metric_name)
        if record is None:
            return None
        meta = store.archive_meta(int(record.get("id", 0)))
        return {**record, "meta": meta}

    def list_archives(self) -> list[dict[str, Any]]:
        return list(store.rows(self.MODULE))
