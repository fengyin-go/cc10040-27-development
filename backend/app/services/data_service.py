"""数据服务领域服务：将指标和模型发布为对外数据服务API。

跨模块数据流: query_builder -> data_service -> cache_layer 以及
              data_service 调用 query_builder.execute_query 获取数据
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService


def published_templates(metric_name: str) -> list[dict[str, Any]]:
    return [tmpl for tmpl in store.find_by_field("query_builder", "关联指标", metric_name)
            if tmpl.get("status") == "已发布"]


class DataServiceService(BaseModuleService):
    MODULE = "data_service"
    ENTITY = "服务接口"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["接口名称", "关联指标或模型", "请求方式", "限流配额", "发布状态"]
    REQUIRED_FIELDS = ["接口名称", "关联指标或模型", "请求方式"]
    STATUS_ORDER = ["开发中", "已发布", "已下线"]
    ACTION_RULES = {"发布接口": "已发布", "下线接口": "已下线"}
    NEGATIVE_ACTIONS = ["下线接口"]

    def call_service(self, entry_id: int, operator: str = "客户端",
                     template_sequence: list[int] | None = None,
                     halt_before_template: int | None = None,
                     resume_call_id: int | None = None,
                     hold_open: bool = False) -> dict[str, Any]:
        """调用数据服务API接口（外部入口，整体在归档锁内执行）。"""
        with store.lock:
            return self._call_service_locked(
                entry_id, operator=operator, template_sequence=template_sequence,
                halt_before_template=halt_before_template,
                resume_call_id=resume_call_id, hold_open=hold_open)

    def _call_service_locked(self, entry_id: int, operator: str = "客户端",
                             template_sequence: list[int] | None = None,
                             halt_before_template: int | None = None,
                             resume_call_id: int | None = None,
                             hold_open: bool = False) -> dict[str, Any]:
        """调用数据服务API接口。

        跨模块数据流: data_service -> query_builder -> cache_layer -> query_journal
        首先验证接口和关联查询模板，然后通过查询构建器获取数据。

        生效边界语义::
            每次调用在发起时记下当时的边界。多份模板按顺序执行；每份模板执行前
            重新核对指标下线边界。若指标在第二份模板执行前被归档：

            * 已取得第一份模板结果 -> 本次调用无法结案，保留第一份的口径和明细，
              第二份标记为「已放弃」，不向调用方交付半截明细；
            * 一份都没跑完就被归档 -> 直接以「已拒绝」结案，不交付任何明细。

        归档后发起的新调用在第一步就被拒绝，不会进入任何模板。
        """
        api = store.find(self.MODULE, entry_id)
        if api is None:
            return {"error": f"服务接口 {entry_id} 不存在"}
        if api["status"] != "已发布":
            return {"error": f"服务接口 {entry_id} 未发布"}

        target = str(api.get("关联指标或模型", ""))
        if not target:
            return {"error": "服务接口未关联指标或模型"}

        record = store.metric_archive(target)

        # ── 恢复在途调用：先结算原调用，绝不新开一条 ──
        if resume_call_id is not None:
            call = self._resume_call(resume_call_id)
            if call is None:
                return {"error": f"在途调用 {resume_call_id} 不存在"}
            if call.get("状态") == "已完成":
                return {"error": f"调用 {resume_call_id} 已完成，不能重复执行"}
            if call.get("状态") == "已拒绝":
                return {
                    "error": f"调用 {resume_call_id} 已拒绝（指标已归档），不能继续取数",
                    "code": "metric_offline", "call_id": call["id"],
                    "call_status": "已拒绝", "delivered_rows": 0,
                }
            if call.get("状态") == "无法结案":
                return {
                    "error": "本次调用无法结案：指标已归档，剩余模板已放弃，不能继续取数",
                    "code": "metric_offline",
                    "call_id": call["id"],
                    "call_status": "无法结案",
                    "completed_templates": [item["template_id"]
                                            for item in call.get("明细", [])],
                    "abandoned_templates": list(call.get("已放弃模板", [])),
                    "retained_row_count": sum(item["row_count"]
                                              for item in call.get("明细", [])),
                    "delivered_rows": 0,
                }
            # 归档记录可能在两次请求之间写入，但调用状态尚未推进时，这里兜底结算。
            if record is not None and call.get("待执行模板"):
                return self._settle_truncated(
                    call, api, operator, record, call["待执行模板"][0])
            ordered = [tmpl for tmpl in published_templates(target)
                       if int(tmpl.get("id", 0)) in set(call.get("待执行模板", []))]
        else:
            # ── 归档后发起的新调用：拒绝取数，连模板都不再枚举 ──
            if record is not None:
                return self._reject_new_call(api, operator, target, record)
            try:
                ordered = self._select_templates(target, template_sequence)
            except ValueError as exc:
                return {"error": str(exc)}

        if not ordered:
            if resume_call_id is not None:
                store.finish_service_call(
                    call, "失败", "在途调用已无可用模板，无法继续执行")
                return {"error": "在途调用已无可用模板", "call_id": call["id"],
                        "call_status": "失败", "delivered_rows": 0}
            return {"error": "没有可用的已发布查询模板"}

        from app.services.query_builder import QueryBuilderService
        builder = QueryBuilderService()

        call = (self._resume_call(resume_call_id) if resume_call_id
                else store.open_service_call(api, operator,
                                             [int(t.get("id", 0)) for t in ordered]))

        pending_ids = list(call.get("待执行模板", []))

        for tmpl in ordered:
            template_id = int(tmpl.get("id", 0))
            if template_id not in pending_ids:
                continue

            # 测试/演示钩子：在指定模板执行前模拟一次归档，用于核对在途请求的数据。
            if halt_before_template is not None and template_id == halt_before_template:
                from app.services.metric_archive import MetricArchiveService
                MetricArchiveService().archive_metric(
                    store.find_by_field("metric_definition", "指标名称", target)[0]["id"],
                    reason=f"调用 {call['id']} 执行第二份模板前下线",
                    operator=operator,
                )

            # 每份模板执行前都按「此刻」重新核对边界。
            offline = store.metric_archive(target)
            if offline is not None:
                return self._settle_truncated(call, api, operator, offline,
                                              template_id)

            query_result = builder.execute_query(template_id, operator=operator)
            if "rows" not in query_result:
                # 单份模板失败（非下线原因）：按失败结案，但保留已取得的部分。
                store.finish_service_call(call, "失败", str(query_result.get("error", "")))
                store.log_query(
                    metric_id=int(api.get("id", 0)), operator=operator,
                    sql_summary=f"调用 #{call['id']} 模板 {template_id} 失败",
                    row_count=0, duration_ms=0.0, status="异常终止",
                )
                return {
                    "error": query_result.get("error", "查询模板执行失败"),
                    "code": "template_failed",
                    "call_id": call["id"],
                    "call_status": call["状态"],
                }

            call["已完成模板"].append(template_id)
            pending_ids.remove(template_id)
            call["待执行模板"] = list(pending_ids)
            call["口径"][str(template_id)] = query_result.get("caliber")
            call["明细"].append({
                "template_id": template_id,
                "template": query_result.get("template"),
                "caliber": query_result.get("caliber"),
                "source": query_result.get("source"),
                "row_count": len(query_result.get("rows", [])),
                "rows": query_result.get("rows", []),
            })

            # 执行完这份模板后暂停：把调用保持在途，供另一个请求发起归档。
            if hold_open and pending_ids:
                return {
                    "api": api.get("接口名称"),
                    "call_id": call["id"],
                    "call_status": "计算中",
                    "boundary": call.get("发起边界"),
                    "completed_templates": list(call["已完成模板"]),
                    "pending_templates": list(pending_ids),
                    "retained_row_count": sum(item["row_count"]
                                              for item in call["明细"]),
                    "delivered_rows": 0,
                    "note": "调用已保持在途，尚未结案；可用 resume_call_id 继续执行",
                }

        store.finish_service_call(call, "已完成")
        return {
            "api": api.get("接口名称"),
            "request_method": api.get("请求方式"),
            "call_id": call["id"],
            "call_status": "已完成",
            "boundary": call.get("发起边界"),
            "completed_templates": list(call["已完成模板"]),
            "result_count": sum(item["row_count"] for item in call["明细"]),
            "delivered_rows": sum(item["row_count"] for item in call["明细"]),
            "query_results": [
                {"template_id": item["template_id"],
                 "template": item["template"],
                 "source": item["source"],
                 "caliber": item["caliber"],
                 "rows": item["rows"]}
                for item in call["明细"]
            ],
        }

    def _select_templates(self, target: str,
                          template_sequence: list[int] | None) -> list[dict[str, Any]]:
        published = published_templates(target)
        if not template_sequence:
            return published
        by_id = {int(tmpl.get("id", 0)): tmpl for tmpl in published}
        ordered = [by_id[tid] for tid in template_sequence if tid in by_id]
        missing = [tid for tid in template_sequence if tid not in by_id]
        if missing or not ordered:
            raise ValueError(f"模板 {missing or template_sequence} 不是 {target} 的已发布模板")
        return ordered

    def _reject_new_call(self, api: dict[str, Any], operator: str,
                         target: str, record: dict[str, Any]) -> dict[str, Any]:
        """归档后才进门的调用：登记一条「已拒绝」调用，不交付任何数据。"""
        rejected = store.open_service_call(api, operator, [])
        store.finish_service_call(
            rejected, "已拒绝",
            f"指标 {target} 已归档（下线记录 #{record.get('id')}），发起边界 "
            f"{rejected.get('发起边界')} 不早于下线边界 {store.metric_offline_boundary(target)}",
        )
        store.log_query(
            metric_id=int(api.get("id", 0)), operator=operator,
            sql_summary=f"拒绝取数: 指标 {target} 已归档",
            row_count=0, duration_ms=0.0, status="异常终止",
        )
        return {
            "error": f"关联指标 {target} 已归档（下线记录 #{record.get('id')}），不再接受新计算",
            "code": "metric_offline",
            "api": api.get("接口名称"),
            "metric": target,
            "archive_id": record.get("id"),
            "call_id": rejected["id"],
            "call_status": "已拒绝",
            "delivered_rows": 0,
        }

    def _resume_call(self, call_id: int) -> dict[str, Any] | None:
        for call in store.service_calls():
            if call.get("id") == call_id:
                return call
        return None

    def _settle_truncated(self, call: dict[str, Any], api: dict[str, Any],
                          operator: str, record: dict[str, Any],
                          blocked_template: int) -> dict[str, Any]:
        """指标在调用途中被归档：按是否已取到数据决定能否结案。

        * 已有结果 -> ``无法结案``，保留已取得的口径与明细；
        * 尚无结果 -> ``已拒绝``，不交付任何明细。
        """
        granted = [item for item in call["明细"]]
        # 未执行的模板（含当前被拦下的这份）按原顺序标记为已放弃。
        truncated = list(call.get("待执行模板", []))
        if blocked_template not in truncated:
            truncated.insert(0, blocked_template)
        call["待执行模板"] = []
        call["已放弃模板"] = list(truncated)

        if granted:
            store.finish_service_call(
                call, "无法结案",
                f"指标 {call.get('关联指标')} 在模板 {blocked_template} 执行前归档"
                f"（下线记录 #{record.get('id')}），已取到 {len(granted)} 份模板结果；"
                f"剩余 {len(truncated)} 份模板已放弃",
            )
            status = "无法结案"
        else:
            store.finish_service_call(
                call, "已拒绝",
                f"指标 {call.get('关联指标')} 已归档（下线记录 #{record.get('id')}），未取到任何数据",
            )
            status = "已拒绝"

        store.log_query(
            metric_id=int(api.get("id", 0)), operator=operator,
            sql_summary=f"调用 #{call['id']} 因指标归档中断（{status}）",
            row_count=0,
            duration_ms=0.0, status="异常终止",
        )
        return {
            "error": f"关联指标 {call.get('关联指标')} 已归档，本次调用{status}",
            "code": "metric_offline",
            "api": api.get("接口名称"),
            "call_id": call["id"],
            "call_status": status,
            "archive_id": record.get("id"),
            "completed_templates": [item["template_id"] for item in granted],
            "abandoned_templates": truncated,
            "calibers": {str(item["template_id"]): item["caliber"] for item in granted},
            "retained_details": [
                {"template_id": item["template_id"], "caliber": item["caliber"],
                 "row_count": item["row_count"], "rows": item["rows"]}
                for item in granted
            ],
            # 只留档、不交付：半截计算的明细不能出现在对外响应的数据字段里。
            "retained_row_count": sum(item["row_count"] for item in granted),
            "delivered_rows": 0,
            "note": ("已取得的部分仅留档供审计，不向调用方交付半截明细；"
                     "请按完整口径重新发起或改用其他指标"),
        }

    def advance_calls_for_archive(self, metric_name: str,
                                  record: dict[str, Any]) -> list[dict[str, Any]]:
        """数据服务按指标下线记录推进在途调用状态。

        还没有取到任何模板结果的在途调用直接以「已拒绝」结案；
        已经取到部分结果的保留为「无法结案」，把未执行的模板标记为「已放弃」。
        已经结案的调用保持不变，历史依据继续可查。
        """
        advanced: list[dict[str, Any]] = []
        for call in store.open_calls_for_metric(metric_name):
            pending = list(call.get("待执行模板", []))
            call["待执行模板"] = []
            call["已放弃模板"] = pending
            if call.get("明细"):
                store.finish_service_call(
                    call, "无法结案",
                    f"指标 {metric_name} 已归档（下线记录 #{record.get('id')}），"
                    f"剩余 {len(pending)} 份模板未执行",
                )
            else:
                store.finish_service_call(
                    call, "已拒绝",
                    f"指标 {metric_name} 已归档（下线记录 #{record.get('id')}），未取到任何数据",
                )
            advanced.append(call)
        return advanced

    def list_calls(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        """查询数据服务调用记录，含已结案与在途请求的历史依据。"""
        return store.service_calls(entry_id)

    def get_service_health(self, entry_id: int) -> dict[str, Any]:
        """获取服务接口健康状态。

        检查关联的指标口径版本和数据服务可用性。
        """
        api = store.find(self.MODULE, entry_id)
        if api is None:
            return {"error": f"服务接口 {entry_id} 不存在"}

        target = api.get("关联指标或模型", "")

        # 检查关联指标的口径版本是否有效
        calibers = store.find_by_field("caliber_version", "所属指标", target)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)

        # 检查关联查询模板
        query_templates = store.find_by_field("query_builder", "关联指标", target)
        published = [t for t in query_templates if t.get("status") == "已发布"]

        return {
            "api_id": entry_id,
            "api_name": api.get("接口名称"),
            "status": api.get("status"),
            "target_metric": target,
            "active_caliber": active_caliber.get("版本号") if active_caliber else None,
            "published_query_templates": len(published),
            "health": "healthy" if active_caliber and published else "degraded",
        }