"""采集任务业务规则：状态流转、字段校验与筛选口径都收在这里。

解析相关的版本选择不在本模块决定：任务创建/启动时把当时模板的生效版本号
钉在「模板版本」字段上，之后解析一律走 registry.task_snapshot() 取该版本
的不可变快照，模板升级不会让旧任务误用新默认值或新校验正则。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.versions import list_diagnostics, parse_snapshot, registry

MODULE = "task"
REQUIRED_FIELDS = ["任务编号", "任务名称", "关联源端"]
STATUS_ORDER = ["草稿", "待执行", "执行中", "已完成"]
ACTION_RULES = {"提交执行": "待执行", "暂停任务": "执行中", "恢复任务": "已完成"}
NEGATIVE_ACTIONS = []


class TaskService:
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
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        detail = dict(entry)
        template_code = entry.get("解析模板编号")
        if template_code:
            detail["当前模板生效版本"] = registry.effective_version(str(template_code))
            detail["版本落后"] = (
                detail["当前模板生效版本"] is not None
                and detail["当前模板生效版本"] != entry.get("模板版本")
            )
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 新任务绑定模板时钉住“当前生效版本”，这是任务后续唯一读取的版本。
        template_code = str(values.get("解析模板编号") or "").strip()
        if template_code:
            effective = registry.effective_version(template_code)
            if effective is None:
                return None, [f"解析模板 {template_code} 没有可用版本，无法创建任务"]
            entry["解析模板编号"] = template_code
            entry["模板版本"] = effective
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def bind_template(self, entry_id: int, template_code: str) -> tuple[dict[str, Any] | None, str]:
        """给任务绑定解析模板并钉住生效版本；已钉版本的任务改绑需要显式调用。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集任务 {entry_id} 不存在或已归档"
        effective = registry.effective_version(template_code)
        if effective is None:
            return None, f"解析模板 {template_code} 没有可用版本"
        entry["解析模板编号"] = template_code
        entry["模板版本"] = effective
        return entry, f"任务已绑定 {template_code} 并钉住版本 {effective}"

    def parse_input(self, entry_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """任务读取输入并解析：规则全部来自任务钉住版本的快照。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集任务 {entry_id} 不存在或已归档"
        snapshot, error = registry.task_snapshot(entry)
        if snapshot is None:
            return None, error or "解析快照缺失"
        result = parse_snapshot(snapshot, payload, task_code=str(entry.get("任务编号", "")))
        return result, ""

    def list_diagnostics(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集任务 {entry_id} 不存在或已归档"
        return list_diagnostics(task_code=str(entry.get("任务编号", ""))), ""

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集任务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"采集任务已{action}"
