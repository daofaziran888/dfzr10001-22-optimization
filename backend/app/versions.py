"""解析模板版本注册中心。

调用链上的“模板版本选择点”集中在本模块，其它地方不许直接读 parser 表：

* parser 表只保存“当前生效版本”的镜像，给模板列表/详情使用；
* parser_version 表是不可变版本档案，任务启动时把生效版本号钉在自己身上，
  之后读取输入格式、字段映射、类型转换、默认值、校验正则、输出格式都只走
  get_snapshot()，模板再怎么改都不影响旧任务；
* 切换走 switch_version()：进程级互斥锁保证并发切换只有一个版本能生效，
  校验失败自动回滚到上一可用版本，并生成版本失败待办；
* parse_snapshot() 是唯一的解析执行入口，诊断结果按当时的版本号回写，
  历史诊断与旧字段解释保持原样，不随后续版本切换改写。

字段重命名兼容：每个版本用“字段改名记录”保存 源字段旧名->新名 的别名链，
解析旧任务的输入时，别名链会把旧输入里的旧源字段自动映射到新源字段再读取，
诊断里同时记下实际命中的源字段名，保证既有映射不中断。
"""
from __future__ import annotations

import copy
import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "parser"
VERSION_MODULE = "parser_version"
TODO_MODULE = "version_todo"
DIAG_MODULE = "parse_diagnostic"
TASK_MODULE = "task"

# 版本档案里构成“解析规则”的字段；读取与回写都必须带着版本号。
SNAPSHOT_FIELDS = [
    "输入格式", "字段映射", "类型转换", "默认值", "校验正则", "输出格式",
]
VERSION_STATUS = ["草稿", "待校验", "已启用", "已停用", "切换失败"]

# 进程级互斥：同一时刻只允许一个模板版本切换在途。
_switch_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _version_rows(template_code: str) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(VERSION_MODULE)
        if row.get("模板编号") == template_code
    ]


def _find_version(template_code: str, version: int) -> dict[str, Any] | None:
    for row in _version_rows(template_code):
        if int(row.get("版本号", 0)) == version:
            return row
    return None


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


class VersionRegistry:
    """模板版本选择点：版本档案查询、生效版本切换与任务快照读取。"""

    # ---- 查询 -------------------------------------------------------
    def list_versions(self, template_code: str) -> list[dict[str, Any]]:
        return sorted(_version_rows(template_code), key=lambda row: int(row["版本号"]))

    def get_version(self, template_code: str, version: int) -> dict[str, Any] | None:
        row = _find_version(template_code, version)
        return copy.deepcopy(row) if row is not None else None

    def effective_version(self, template_code: str) -> int | None:
        for row in _version_rows(template_code):
            if row.get("是否生效"):
                return int(row["版本号"])
        return None

    def get_snapshot(self, template_code: str, version: int) -> dict[str, Any] | None:
        """任务读取解析规则的唯一入口：按钉住的版本号取不可变快照。"""
        row = _find_version(template_code, version)
        if row is None:
            return None
        snapshot = {field: copy.deepcopy(row.get(field)) for field in SNAPSHOT_FIELDS}
        snapshot["模板编号"] = template_code
        snapshot["模板版本"] = int(row["版本号"])
        snapshot["字段改名记录"] = copy.deepcopy(row.get("字段改名记录", []))
        return snapshot

    def task_snapshot(self, task: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """按任务启动时钉住的版本号取快照；版本档案缺失时给出可读错误。"""
        template_code = str(task.get("解析模板编号") or "").strip()
        version = task.get("模板版本")
        if not template_code or version is None:
            return None, "任务未绑定解析模板或缺少生效版本号，无法确定解析口径"
        snapshot = self.get_snapshot(template_code, int(version))
        if snapshot is None:
            return None, f"模板 {template_code} 的版本 {version} 已不存在，解析口径缺失"
        return snapshot, None

    # ---- 版本起草 ---------------------------------------------------
    def create_draft(
        self,
        template_code: str,
        values: dict[str, Any],
        *,
        rename_records: list[dict[str, str]] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """基于提交内容起草新版本；不直接生效，必须经过切换校验。"""
        existing = _version_rows(template_code)
        if not existing:
            return None, f"解析模板 {template_code} 不存在，不能直接登记版本"
        next_version = max(int(row["版本号"]) for row in existing) + 1
        base = existing[-1]
        draft = {
            "id": _next_id(VERSION_MODULE),
            "模板编号": template_code,
            "版本号": next_version,
            "状态": "草稿",
            "是否生效": False,
            "pending": False,
            "字段改名记录": copy.deepcopy(rename_records or []),
            "失败原因": None,
            "创建时间": _now(),
        }
        for field in SNAPSHOT_FIELDS:
            draft[field] = copy.deepcopy(values[field]) if field in values else copy.deepcopy(base.get(field))
        store.rows(VERSION_MODULE).append(draft)
        return copy.deepcopy(draft), ""

    # ---- 切换与回滚 -------------------------------------------------
    def switch_version(
        self,
        template_code: str,
        target_version: int,
        *,
        operator: str = "系统",
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """把生效版本切到 target_version。

        返回 (版本记录, 说明, 是否拿到锁)。并发切换拿不到锁时不改任何数据；
        校验失败时目标版本标记“切换失败”，上一可用版本保持生效（回滚），
        并写入版本失败待办和审计记录。
        """
        target = _find_version(template_code, target_version)
        if target is None:
            return None, f"模板 {template_code} 不存在版本 {target_version}", True

        if not _switch_lock.acquire(blocking=False):
            self._add_todo(
                template_code, target_version, "并发冲突",
                f"版本 {target_version} 切换时已有其它切换在途，本次请求未生效，请重试",
            )
            self._write_audit(
                template_code, target_version, operator,
                "并发冲突：切换未执行，沿用上一可用版本",
            )
            return None, "已有模板版本切换在途，并发切换被拒绝，请稍后重试", False

        try:
            previous_version = self.effective_version(template_code)
            error = self._validate(target)
            if error:
                # 切换失败：目标版本落“切换失败”，上一可用版本原样保留即回滚。
                target["状态"] = "切换失败"
                target["pending"] = True
                target["失败原因"] = error
                rollback_to = previous_version
                rollback_note = (
                    f"，已回滚到版本 {rollback_to}" if rollback_to is not None else "，无历史可用版本"
                )
                message = f"版本 {target_version} 切换失败：{error}{rollback_note}"
                self._add_todo(template_code, target_version, "切换失败", message)
                self._write_audit(template_code, target_version, operator, message)
                return copy.deepcopy(target), message, True

            # 校验通过：同一模板内只允许一个生效版本。
            for row in _version_rows(template_code):
                if int(row["版本号"]) == target_version:
                    row["状态"] = "已启用"
                    row["是否生效"] = True
                    row["pending"] = False
                    row["失败原因"] = None
                elif row.get("是否生效"):
                    row["是否生效"] = False
                    row["状态"] = "已停用"
                    row["pending"] = False
            self._sync_template_row(template_code, target)
            self._write_audit(
                template_code, target_version, operator,
                f"生效版本切换为版本 {target_version}"
                + (f"，原版本 {previous_version} 已停用" if previous_version else ""),
            )
            return copy.deepcopy(target), f"模板 {template_code} 已切换到版本 {target_version}", True
        finally:
            _switch_lock.release()

    def _validate(self, version_row: dict[str, Any]) -> str | None:
        """切换前校验：结构完整 + 校验正则可编译。"""
        mapping = version_row.get("字段映射")
        if not isinstance(mapping, dict) or not mapping:
            return "字段映射必须是非空字典"
        for field in ("输入格式", "输出格式"):
            if not str(version_row.get(field) or "").strip():
                return f"{field}不能为空"
        patterns = version_row.get("校验正则") or {}
        if not isinstance(patterns, dict):
            return "校验正则必须是“源字段 -> 正则”的字典"
        for source_field, pattern in patterns.items():
            try:
                re.compile(str(pattern))
            except re.error as exc:
                return f"字段 {source_field} 的校验正则不是合法正则（{exc.msg}）"
        return None

    def _sync_template_row(self, template_code: str, version_row: dict[str, Any]) -> None:
        """把生效版本镜像回 parser 表，保证模板详情与实际生效版本一致。"""
        for row in store.rows(MODULE):
            if row.get("模板编号") == template_code:
                for field in SNAPSHOT_FIELDS:
                    row[field] = copy.deepcopy(version_row.get(field))
                row["模板版本"] = int(version_row["版本号"])
                row["status"] = "已启用"
                row["pending"] = False
                row["abnormal"] = False
                return

    def _add_todo(
        self, template_code: str, version: int, todo_type: str, note: str,
    ) -> dict[str, Any]:
        # 同一模板/版本/类型的未处理待办只保留一条，重复失败只更新说明。
        for row in store.rows(TODO_MODULE):
            if (
                not row.get("已处理")
                and row.get("模板编号") == template_code
                and int(row.get("版本号", 0)) == version
                and row.get("待办类型") == todo_type
            ):
                row["说明"] = note
                row["创建时间"] = _now()
                return row
        related_task = ""
        for task in store.rows(TASK_MODULE):
            if task.get("解析模板编号") == template_code:
                related_task = str(task.get("任务编号", ""))
                break
        todo = {
            "id": _next_id(TODO_MODULE),
            "模板编号": template_code,
            "版本号": version,
            "关联任务": related_task,
            "待办类型": todo_type,
            "说明": note,
            "已处理": False,
            "创建时间": _now(),
        }
        store.rows(TODO_MODULE).append(todo)
        return todo

    def _write_audit(
        self, template_code: str, version: int, operator: str, remark: str,
    ) -> None:
        audit_id = _next_id("audit")
        entry = {
            "id": audit_id,
            "status": "已记录",
            "pending": True,
            "abnormal": "失败" in remark or "冲突" in remark,
            "审计编号": f"AUDI-V{version:04d}-{audit_id:04d}",
            "操作类型": "模板版本切换",
            "操作对象": f"{template_code}#v{version}",
            "操作人": operator,
            "操作时间": _now(),
            "变更前": f"{template_code} 切换前口径",
            "变更后": remark,
            "审计备注": remark,
        }
        store.rows("audit").append(entry)

    # ---- 待办 -------------------------------------------------------
    def list_todos(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        rows = store.rows(TODO_MODULE)
        if not include_closed:
            rows = [row for row in rows if not row.get("已处理")]
        return copy.deepcopy(rows)

    def dismiss_todo(self, todo_id: int) -> tuple[dict[str, Any] | None, str]:
        for row in store.rows(TODO_MODULE):
            if int(row.get("id", 0)) == todo_id:
                row["已处理"] = True
                return copy.deepcopy(row), "版本失败待办已关闭"
        return None, f"待办 {todo_id} 不存在"


registry = VersionRegistry()


# =====================================================================
# 解析引擎：任务读取与诊断回写的唯一执行点
# =====================================================================

def _source_aliases(snapshot: dict[str, Any]) -> dict[str, str]:
    """汇总当前版本及历史改名记录，得到 源字段（含旧名）-> 现源字段 的别名表。"""
    aliases: dict[str, str] = {}
    for record in snapshot.get("字段改名记录", []):
        old_name = record.get("源字段")
        new_name = record.get("新源字段")
        if old_name and new_name:
            aliases[old_name] = new_name
            # 传递兼容：更早的旧名也指向最新源字段。
            for earlier, target in list(aliases.items()):
                if target == old_name:
                    aliases[earlier] = new_name
    return aliases


def _resolve_source(payload: dict[str, Any], source_field: str, aliases: dict[str, str]) -> tuple[Any, str]:
    """按 现名 -> 历史旧名 的顺序取输入值，返回 (值, 实际命中的源字段名)。"""
    if source_field in payload:
        return payload[source_field], source_field
    for old_name, new_name in aliases.items():
        if new_name == source_field and old_name in payload:
            return payload[old_name], old_name
    return None, source_field


def _convert(value: Any, target_type: str | None) -> Any:
    if value is None or not target_type:
        return value
    kind = str(target_type).lower()
    try:
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "str":
            return str(value)
        if kind == "bool":
            return str(value).lower() in {"1", "true", "yes", "y", "是"}
    except (TypeError, ValueError):
        return value
    return value


def parse_snapshot(
    snapshot: dict[str, Any],
    payload: dict[str, Any],
    *,
    task_code: str = "",
) -> dict[str, Any]:
    """按指定版本快照解析一份输入，并把诊断按该版本号回写。

    诊断一旦写入即不可变：后续模板切换不会重算历史诊断，旧字段解释原样保留。
    """
    version = int(snapshot["模板版本"])
    template_code = str(snapshot["模板编号"])
    mapping: dict[str, Any] = snapshot.get("字段映射") or {}
    defaults: dict[str, Any] = snapshot.get("默认值") or {}
    conversions: dict[str, Any] = snapshot.get("类型转换") or {}
    patterns: dict[str, Any] = snapshot.get("校验正则") or {}
    aliases = _source_aliases(snapshot)

    output: dict[str, Any] = {}
    field_notes: list[dict[str, Any]] = []
    errors: list[str] = []

    for source_field, target_field in mapping.items():
        raw_value, hit_field = _resolve_source(payload, source_field, aliases)
        used_alias = hit_field != source_field
        missing = raw_value is None or raw_value == ""

        # 默认值：只按钉住版本的默认值填充，新版本默认值不影响旧任务。
        if missing and target_field in defaults:
            raw_value = defaults[target_field]
            missing = False
            applied_default = True
        else:
            applied_default = False

        # 校验正则：只执行钉住版本里登记的正则，新增正则不会回溯到旧任务。
        pattern = patterns.get(source_field)
        regex_ok = True
        if pattern is not None and raw_value is not None and raw_value != "":
            regex_ok = re.fullmatch(str(pattern), str(raw_value)) is not None
            if not regex_ok:
                errors.append(
                    f"字段 {target_field}（源字段 {hit_field}）的值 {raw_value!r} 未匹配版本 {version} 的校验正则 {pattern}"
                )

        converted = _convert(raw_value, conversions.get(target_field))
        output[target_field] = converted

        # 旧字段解释：记录当时实际命中的源字段（含旧名），历史诊断保持原样。
        field_notes.append({
            "目标字段": target_field,
            "版本内源字段": source_field,
            "实际命中源字段": hit_field,
            "使用历史别名": used_alias,
            "缺失并套用默认值": applied_default,
            "默认值": defaults.get(target_field),
            "类型转换": conversions.get(target_field),
            "校验正则": pattern,
            "正则通过": regex_ok,
            "原始值": None if missing and not applied_default else raw_value,
            "输出值": converted,
        })

    diagnostic = {
        "id": _next_id(DIAG_MODULE),
        "任务编号": task_code,
        "模板编号": template_code,
        "模板版本": version,
        "输入格式": snapshot.get("输入格式"),
        "输出格式": snapshot.get("输出格式"),
        "通过": not errors,
        "错误": errors,
        "字段解释": field_notes,
        "诊断时间": _now(),
    }
    store.rows(DIAG_MODULE).append(diagnostic)
    return {
        "模板编号": template_code,
        "模板版本": version,
        "输出格式": snapshot.get("输出格式"),
        "数据": output,
        "诊断编号": diagnostic["id"],
        "通过": not errors,
        "错误": errors,
    }


def list_diagnostics(
    *, task_code: str | None = None, template_code: str | None = None,
) -> list[dict[str, Any]]:
    rows = store.rows(DIAG_MODULE)
    if task_code:
        rows = [row for row in rows if row.get("任务编号") == task_code]
    if template_code:
        rows = [row for row in rows if row.get("模板编号") == template_code]
    return copy.deepcopy(rows)


def get_diagnostic(diagnostic_id: int) -> dict[str, Any] | None:
    for row in store.rows(DIAG_MODULE):
        if int(row.get("id", 0)) == diagnostic_id:
            return copy.deepcopy(row)
    return None
