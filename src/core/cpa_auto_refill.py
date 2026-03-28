"""
CPA 账号自动补充服务
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from ..config.settings import get_settings
from ..database.session import get_db
from ..database import crud
from .upload.cpa_upload import get_cpa_accounts_info
from ..web.task_manager import task_manager

logger = logging.getLogger(__name__)


class CPAAutoRefillService:
    """CPA 账号自动补充服务"""

    def __init__(self):
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self._last_check_time: Optional[datetime] = None
        self._last_refill_time: Optional[datetime] = None
        self._current_tasks: dict = {}  # 存储当前运行的注册任务 {task_id: {service_name, count, started_at}}

    async def start(self):
        """启动自动补充服务"""
        if self.running:
            logger.warning("CPA 自动补充服务已在运行")
            return

        self.running = True
        self.task = asyncio.create_task(self._run_loop())
        logger.info("CPA 自动补充服务已启动")

    async def stop(self):
        """停止自动补充服务"""
        if not self.running:
            return

        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("CPA 自动补充服务已停止")

    async def _run_loop(self):
        """主循环"""
        while self.running:
            try:
                settings = get_settings()

                # 执行检查
                await self._check_and_refill()

                # 等待下次检查
                interval = settings.cpa_auto_refill_check_interval
                logger.debug(f"等待 {interval} 秒后进行下次检查")
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CPA 自动补充服务异常: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_and_refill(self):
        """检查并补充账号"""
        try:
            settings = get_settings()
            self._last_check_time = datetime.now()

            # 获取要监控的 CPA 服务列表
            with get_db() as db:
                service_ids_str = settings.cpa_auto_refill_service_ids.strip()

                if service_ids_str:
                    # 解析服务 ID 列表
                    try:
                        service_ids = [int(sid.strip()) for sid in service_ids_str.split(',') if sid.strip()]
                        services = [crud.get_cpa_service_by_id(db, sid) for sid in service_ids]
                        services = [s for s in services if s and s.enabled]
                    except ValueError:
                        logger.error(f"无效的服务 ID 列表: {service_ids_str}")
                        return
                else:
                    # 空字符串表示所有启用的服务
                    services = crud.get_cpa_services(db, enabled=True)

                if not services:
                    logger.warning("未找到可用的 CPA 服务，跳过自动补充")
                    return

                # 检查每个服务
                for service in services:
                    await self._check_service(service, settings)

        except Exception as e:
            logger.error(f"检查并补充账号时出错: {e}", exc_info=True)

    async def _check_service(self, service, settings):
        """检查单个服务并补充"""
        try:
            logger.debug(f"[{service.name}] 开始检查服务，阈值={settings.cpa_auto_refill_threshold}, 目标={settings.cpa_auto_refill_target}")

            # 检查是否已有该服务的运行中任务
            running_tasks = [
                task for task in self._current_tasks.values()
                if task["service_id"] == service.id and task["status"] == "running"
            ]

            if running_tasks:
                logger.info(f"[{service.name}] 已有 {len(running_tasks)} 个运行中的补充任务，跳过本次检查")
                return

            # 获取 CPA 账号信息
            success, result = get_cpa_accounts_info(service.api_url, service.api_token)
            if not success:
                logger.error(f"[{service.name}] 获取账号信息失败: {result.get('error', '未知错误')}")
                return

            # 统计有效账号数
            by_status = result.get("by_status", {})
            active_count = by_status.get("active", 0)
            total_count = result.get("total", 0)
            accounts = result.get("accounts", [])

            logger.info(f"[{service.name}] CPA 账号状态: 总数={total_count}, 有效={active_count}")

            # 自动删除无效账号
            if settings.cpa_auto_delete_invalid:
                invalid_accounts = [
                    acc for acc in accounts
                    if acc.get("status") not in ["active", "pending"]
                ]
                if invalid_accounts:
                    logger.info(f"[{service.name}] 发现 {len(invalid_accounts)} 个无效账号，开始删除...")
                    from .upload.cpa_upload import delete_invalid_cpa_accounts
                    success_count, failed_count = delete_invalid_cpa_accounts(
                        service.api_url,
                        service.api_token,
                        invalid_accounts
                    )
                    logger.info(f"[{service.name}] 删除无效账号完成: 成功 {success_count}, 失败 {failed_count}")

            # 检查是否需要补充
            threshold = settings.cpa_auto_refill_threshold
            logger.info(f"[{service.name}] 检查补充条件: 有效账号={active_count}, 阈值={threshold}")
            if active_count >= threshold:
                logger.info(f"[{service.name}] 有效账号数 ({active_count}) >= 阈值 ({threshold})，无需补充")
                return

            # 计算需要注册的数量
            target = settings.cpa_auto_refill_target
            need_count = max(1, target - active_count)

            logger.info(f"[{service.name}] 有效账号数 ({active_count}) < 阈值 ({threshold})，需要补充 {need_count} 个账号")

            # 触发批量注册
            await self._trigger_registration(need_count, service.id, service.name)

        except Exception as e:
            logger.error(f"[{service.name}] 检查服务时出错: {e}", exc_info=True)

    async def _trigger_registration(self, count: int, cpa_service_id: int, service_name: str):
        """触发批量注册"""
        try:
            settings = get_settings()

            # 获取邮箱服务
            with get_db() as db:
                email_service = None
                email_service_type = "tempmail"  # 默认使用 tempmail
                email_service_id = None

                # 如果配置了指定的邮箱服务 ID
                if settings.cpa_auto_refill_email_service_id > 0:
                    email_service = crud.get_email_service_by_id(db, settings.cpa_auto_refill_email_service_id)
                    if email_service and email_service.enabled:
                        email_service_type = email_service.service_type
                        email_service_id = email_service.id
                        logger.info(f"[{service_name}] 使用配置的邮箱服务: {email_service_type}")
                    else:
                        logger.warning(f"[{service_name}] 配置的邮箱服务 ID {settings.cpa_auto_refill_email_service_id} 不存在或未启用")

                # 如果没有配置或配置的服务不可用，尝试使用优先级最高的
                if not email_service:
                    email_services = crud.get_email_services(db, enabled=True)
                    if email_services:
                        # 按优先级排序
                        email_services.sort(key=lambda x: x.priority or 999)
                        email_service = email_services[0]
                        email_service_type = email_service.service_type
                        email_service_id = email_service.id
                        logger.info(f"[{service_name}] 使用优先级最高的邮箱服务: {email_service_type}")
                    else:
                        # 没有配置任何邮箱服务，使用默认的 tempmail
                        logger.info(f"[{service_name}] 没有配置邮箱服务，使用默认的 tempmail.lol")

                # 创建任务记录
                task_uuids = []
                for _ in range(count):
                    task_uuid = str(__import__('uuid').uuid4())
                    task = crud.create_registration_task(
                        db,
                        task_uuid=task_uuid,
                        proxy=None
                    )
                    task_uuids.append(task_uuid)

            # 创建批量注册任务 ID
            task_id = f"auto_refill_{service_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            logger.info(f"[{service_name}] 触发自动补充注册任务: task_id={task_id}, count={count}, email_service={email_service_type}")

            # 记录任务信息
            self._current_tasks[task_id] = {
                "service_name": service_name,
                "service_id": cpa_service_id,
                "count": count,
                "started_at": datetime.now(),
                "status": "running"
            }

            # 在后台启动注册任务
            asyncio.create_task(
                self._run_registration_task(
                    task_id,
                    task_uuids,
                    email_service_type,
                    email_service_id,
                    cpa_service_id
                )
            )

            self._last_refill_time = datetime.now()

        except Exception as e:
            logger.error(f"[{service_name}] 触发注册任务失败: {e}", exc_info=True)

    async def _run_registration_task(
        self,
        task_id: str,
        task_uuids: list,
        email_service_type: str,
        email_service_id: int,
        cpa_service_id: int
    ):
        """运行注册任务并追踪状态"""
        try:
            from ..web.routes.registration import run_batch_registration, batch_tasks

            # 执行批量注册
            await run_batch_registration(
                batch_id=task_id,
                task_uuids=task_uuids,
                email_service_type=email_service_type,
                proxy=None,
                email_service_config=None,
                email_service_id=email_service_id,
                interval_min=5,
                interval_max=15,
                concurrency=1,
                mode="pipeline",
                auto_upload_cpa=True,
                cpa_service_ids=[cpa_service_id],
                auto_upload_sub2api=False,
                sub2api_service_ids=[],
                auto_upload_tm=False,
                tm_service_ids=[],
            )

            # 任务完成，从 batch_tasks 获取最终状态
            if task_id in self._current_tasks:
                batch_info = batch_tasks.get(task_id, {})
                self._current_tasks[task_id]["status"] = "completed"
                self._current_tasks[task_id]["completed_at"] = datetime.now()
                self._current_tasks[task_id]["success_count"] = batch_info.get("success", 0)
                self._current_tasks[task_id]["failed_count"] = batch_info.get("failed", 0)
                logger.info(f"自动补充任务 {task_id} 完成: 成功 {batch_info.get('success', 0)}, 失败 {batch_info.get('failed', 0)}")
        except Exception as e:
            logger.error(f"注册任务 {task_id} 执行失败: {e}", exc_info=True)
            if task_id in self._current_tasks:
                self._current_tasks[task_id]["status"] = "failed"
                self._current_tasks[task_id]["error"] = str(e)
                self._current_tasks[task_id]["completed_at"] = datetime.now()

    def get_status(self) -> dict:
        """获取服务状态"""
        from ..web.routes.registration import batch_tasks

        # 清理已完成超过1小时的任务
        now = datetime.now()
        tasks_to_remove = []
        for task_id, task_info in self._current_tasks.items():
            if task_info["status"] in ["completed", "failed"]:
                completed_at = task_info.get("completed_at") or task_info.get("started_at")
                if completed_at and (now - completed_at).total_seconds() > 3600:
                    tasks_to_remove.append(task_id)

        for task_id in tasks_to_remove:
            del self._current_tasks[task_id]

        # 格式化任务信息
        current_tasks = []
        for task_id, task_info in self._current_tasks.items():
            task_data = {
                "task_id": task_id,
                "service_name": task_info["service_name"],
                "service_id": task_info["service_id"],
                "count": task_info["count"],
                "status": task_info["status"],
                "started_at": task_info["started_at"].isoformat(),
            }

            # 如果任务正在运行，从 batch_tasks 获取实时进度
            if task_info["status"] == "running" and task_id in batch_tasks:
                batch_info = batch_tasks[task_id]
                task_data["progress"] = {
                    "total": batch_info.get("total", 0),
                    "completed": batch_info.get("completed", 0),
                    "success": batch_info.get("success", 0),
                    "failed": batch_info.get("failed", 0),
                    "current_index": batch_info.get("current_index", 0),
                }

            if "completed_at" in task_info:
                task_data["completed_at"] = task_info["completed_at"].isoformat()
            if "success_count" in task_info:
                task_data["success_count"] = task_info["success_count"]
            if "failed_count" in task_info:
                task_data["failed_count"] = task_info["failed_count"]
            if "error" in task_info:
                task_data["error"] = task_info["error"]
            current_tasks.append(task_data)

        return {
            "running": self.running,
            "last_check_time": self._last_check_time.isoformat() if self._last_check_time else None,
            "last_refill_time": self._last_refill_time.isoformat() if self._last_refill_time else None,
            "current_tasks": current_tasks,
        }


# 全局实例
_auto_refill_service: Optional[CPAAutoRefillService] = None


def get_auto_refill_service() -> CPAAutoRefillService:
    """获取自动补充服务实例"""
    global _auto_refill_service
    if _auto_refill_service is None:
        _auto_refill_service = CPAAutoRefillService()
    return _auto_refill_service
