"""数据解析模板业务规则：状态流转、字段校验与筛选口径都收在这里。

模板本体（parser 表）始终镜像“当前生效版本”，版本档案（parser_version 表）
保存每个不可变版本；模板版本选择点统一在 app.versions.registry，
本服务只做列表筛选和动作转发，不直接决定任务读哪一版。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.versions import registry

MODULE = "parser"
REQUIRED_FIELDS = ["模板编号", "输入格式", "字段映射"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class ParserService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        # 详情里同时给出当前生效版本与完整版本档案，避免“两套结果”看不清来源。
        template_code = str(entry.get("模板编号", ""))
        detail = dict(entry)
        detail["生效版本号"] = registry.effective_version(template_code)
        detail["版本档案"] = registry.list_versions(template_code)
        return detail

    def get_template_by_code(self, template_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("模板编号") == template_code:
                return row
        return None

    def list_versions(self, template_code: str) -> list[dict[str, Any]]:
        return registry.list_versions(template_code)

    def get_version(self, template_code: str, version: int) -> dict[str, Any] | None:
        return registry.get_version(template_code, version)

    def create_draft(
        self, template_code: str, values: dict[str, Any],
        rename_records: list[dict[str, str]] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        return registry.create_draft(template_code, values, rename_records=rename_records)

    def switch_version(
        self, template_code: str, version: int, *, operator: str = "系统",
    ) -> tuple[dict[str, Any] | None, str, bool]:
        return registry.switch_version(template_code, version, operator=operator)

    def list_todos(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        return registry.list_todos(include_closed=include_closed)

    def dismiss_todo(self, todo_id: int) -> tuple[dict[str, Any] | None, str]:
        return registry.dismiss_todo(todo_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"解析模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据解析模板可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"解析模板已{action}"
