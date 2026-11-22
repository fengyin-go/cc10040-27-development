"""查询构建器领域服务：构建和缓存多维组合查询模板以加速分析。

消费口径版本的指标定义，生成查询模板供数据服务调用。
跨模块数据流: caliber_version -> query_builder -> data_service -> cache_layer -> query_journal
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class QueryBuilderService(BaseModuleService):
    MODULE = "query_builder"
    ENTITY = "查询模板"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["模板名称", "关联指标", "维度组合", "过滤条件", "缓存策略"]
    REQUIRED_FIELDS = ["模板名称", "关联指标", "维度组合"]
    STATUS_ORDER = ["草稿", "已发布", "已过期"]
    ACTION_RULES = {"发布模板": "已发布", "过期模板": "已过期"}
    NEGATIVE_ACTIONS = []

    def execute_query(self, entry_id: int, operator: str = "系统",
                      use_cache: bool = True) -> dict[str, Any] | None:
        """执行查询模板并返回结果与日志。

        跨模块数据流: query_builder -> caliber_version(口径验证) -> cache_layer(缓存) -> query_journal(日志)
        """
        template = store.find(self.MODULE, entry_id)
        if template is None:
            return {"error": f"查询模板 {entry_id} 不存在"}
        if template["status"] != "已发布":
            return {"error": f"查询模板 {entry_id} 未发布"}

        metric_name = template.get("关联指标", "")
        if not metric_name:
            return {"error": "查询模板未关联指标"}

        # 下线边界检查：指标归档后，任何新的取数请求都不允许再执行。
        # 这一步放在缓存之前，归档时已失效过缓存，避免从旧缓存里取到已下线指标。
        if store.is_metric_offline(metric_name):
            record = store.metric_archive(metric_name) or {}
            return {
                "error": f"指标 {metric_name} 已归档（下线记录 #{record.get('id')}），不再接受计算",
                "code": "metric_offline",
                "metric": metric_name,
                "archive_id": record.get("id"),
            }

        # 检查指标口径版本
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)
        if active_caliber is None:
            return {"error": f"指标 {metric_name} 没有已生效的口径版本"}

        cache_key = f"query:{metric_name}:{template.get('维度组合','')}:{template.get('过滤条件','')}"

        # 尝试读取缓存
        if use_cache:
            cached = store.cache_get(cache_key)
            if cached is not None:
                t_start = time.time()
                store.log_query(
                    metric_id=template.get("id", 0),
                    operator=operator,
                    sql_summary=f"缓存命中: {template.get('模板名称')}",
                    row_count=len(cached) if isinstance(cached, list) else 1,
                    duration_ms=(time.time() - t_start) * 1000,
                    status="正常",
                )
                return {"source": "cache", "template": template.get("模板名称"),
                        "caliber": active_caliber.get("版本号"), "rows": cached}

        # 模拟查询执行 - 基于指标数据生成结果
        t_start = time.time()
        metric_data = store.find_by_field("metric_definition", "指标名称", metric_name)
        dimensions = template.get("维度组合", "").split(",") if template.get("维度组合") else []

        result_rows = []
        for idx, m in enumerate(metric_data):
            row = {"序号": idx + 1, "指标": metric_name,
                   "口径版本": active_caliber.get("版本号", ""),
                   "计算口径": m.get("计算口径", ""),
                   "统计粒度": m.get("统计粒度", "")}
            for dim in dimensions:
                row[dim.strip()] = f"维度值_{idx + 1}"
            result_rows.append(row)

        duration_ms = (time.time() - t_start) * 1000
        status = "慢查询" if duration_ms > 100 else "正常"

        # 写入查询日志
        store.log_query(
            metric_id=template.get("id", 0),
            operator=operator,
            sql_summary=f"查询模板: {template.get('模板名称')}, 维度: {template.get('维度组合')}",
            row_count=len(result_rows),
            duration_ms=duration_ms,
            status=status,
        )

        # 写入缓存
        cache_ttl = 300  # 默认 5 分钟
        cache_policy = template.get("缓存策略", "")
        if "长期" in str(cache_policy):
            cache_ttl = 3600
        elif "短期" in str(cache_policy):
            cache_ttl = 60
        store.cache_set(cache_key, result_rows, cache_ttl)

        return {"source": "computed", "template": template.get("模板名称"),
                "caliber": active_caliber.get("版本号"), "duration_ms": duration_ms,
                "cached_for_seconds": cache_ttl, "rows": result_rows}

    def invalidate_query_cache(self, metric_name: str) -> int:
        """当口径版本变更时，失效指标相关的所有查询缓存。

        跨模块数据流: caliber_version -> query_builder -> cache_layer
        """
        return store.cache_invalidate_by_prefix(f"query:{metric_name}:")