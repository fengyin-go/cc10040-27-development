"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, cross_module

# 固定前缀的跨模块路由必须先注册：资源路由带 /api/{module}/{entry_id} 通配，
# 先注册会把 /api/cross/full-chain-snapshot 之类的固定路径抢走（entry_id 解析 422）。
ROUTERS = [cross_module, resource]