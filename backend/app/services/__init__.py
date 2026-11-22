"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService, service_for
from app.services.metric_definition import MetricDefinitionService
from app.services.metric_archive import MetricArchiveService
from app.services.dimension_model import DimensionModelService
from app.services.query_builder import QueryBuilderService
from app.services.data_service import DataServiceService
from app.services.caliber_version import CaliberVersionService
from app.services.query_journal import QueryJournalService
from app.services.drill_down import DrillDownService
from app.services.report_template import ReportTemplateService
from app.services.experiment_group import ExperimentGroupService
from app.services.data_dashboard import DataDashboardService
from app.services.user_cohort import UserCohortService
from app.services.anomaly_detector import AnomalyDetectorService
from app.services.data_export_service import DataExportServiceService
from app.services.segment_compare import SegmentCompareService
from app.services.pivot_table import PivotTableService
from app.services.data_subscription import DataSubscriptionService
from app.services.cache_layer import CacheLayerService
from app.services.trace_system import TraceSystemService
from app.services.recalc_engine import RecalcEngineService
from app.services.field_lineage import FieldLineageService

SERVICES: dict[str, BaseModuleService] = {
    "metric_definition": MetricDefinitionService(),
    "metric_archive": MetricArchiveService(),
    "dimension_model": DimensionModelService(),
    "query_builder": QueryBuilderService(),
    "data_service": DataServiceService(),
    "caliber_version": CaliberVersionService(),
    "query_journal": QueryJournalService(),
    "drill_down": DrillDownService(),
    "report_template": ReportTemplateService(),
    "experiment_group": ExperimentGroupService(),
    "data_dashboard": DataDashboardService(),
    "user_cohort": UserCohortService(),
    "anomaly_detector": AnomalyDetectorService(),
    "data_export_service": DataExportServiceService(),
    "segment_compare": SegmentCompareService(),
    "pivot_table": PivotTableService(),
    "data_subscription": DataSubscriptionService(),
    "cache_layer": CacheLayerService(),
    "trace_system": TraceSystemService(),
    "recalc_engine": RecalcEngineService(),
    "field_lineage": FieldLineageService(),
}


__all__ = ["SERVICES", "service_for", "BaseModuleService"]
