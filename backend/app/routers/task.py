"""采集任务接口：维护采集任务，覆盖提交执行、暂停任务、恢复任务等动作。

任务解析与诊断接口显式回传生效版本号：输入格式、字段映射、类型转换、默认值、
校验正则、输出格式全部按任务钉住的版本快照读取。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.task import TaskService
from app.versions import registry

router = APIRouter(prefix="/api/task", tags=["采集任务"])

service = TaskService()

LIST_FIELDS = ["任务编号", "任务名称", "关联源端", "采集频率", "增量字段", "起始水位", "批次大小", "执行窗口", "解析模板编号", "模板版本"]
STATUSES = ["草稿", "待执行", "执行中", "已完成"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、待执行、执行中、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤采集任务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    # 工作台需要展示每个任务实际钉住的版本与模板当前生效版本的差异。
    for item in items:
        template_code = item.get("解析模板编号")
        if template_code:
            effective = registry.effective_version(str(template_code))
            item["当前模板生效版本"] = effective
            item["版本落后"] = effective is not None and effective != item.get("模板版本")
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/version-todos")
def task_version_todos() -> dict[str, Any]:
    """任务工作台的版本失败待办入口：切换失败/并发冲突在此可见。"""
    items = registry.list_todos()
    return {"total": len(items), "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集任务 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/diagnostics")
def get_diagnostics(entry_id: int) -> dict[str, Any]:
    """任务的历史诊断：按诊断当时的版本号原样返回，不随模板切换改写。"""
    items, message = service.list_diagnostics(entry_id)
    if items is None:
        raise HTTPException(status_code=404, detail=message)
    return {"任务ID": entry_id, "total": len(items), "items": items}


@router.post("/{entry_id}/parse", response_model=ActionResult)
def parse_task_input(entry_id: int, payload: EntryPayload) -> ActionResult:
    """按任务钉住的模板版本解析输入，结果与诊断都带生效版本号。"""
    data = payload.values.get("数据")
    if not isinstance(data, dict):
        return ActionResult(ok=False, message="缺少待解析数据：values.数据 必须是对象")
    result, message = service.parse_input(entry_id, data)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=bool(result.get("通过")), message="解析完成，诊断已按生效版本回写", entry=result)


@router.post("/{entry_id}/bind-parser", response_model=ActionResult)
def bind_parser(entry_id: int, payload: EntryPayload) -> ActionResult:
    """绑定/改绑解析模板，钉住当前生效版本。"""
    template_code = str(payload.values.get("解析模板编号") or "").strip()
    if not template_code:
        return ActionResult(ok=False, message="缺少参数：解析模板编号")
    entry, message = service.bind_template(entry_id, template_code)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采集任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="采集任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条采集任务执行提交执行、暂停任务、恢复任务；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "task", "total": total, "items": items}
