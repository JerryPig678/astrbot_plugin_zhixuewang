import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp

from zhixue import (
    zhixue_manager,
    load_users,
    load_watch_config,
    save_watch_config,
    format_exams_table,
    format_marks_table,
)


class ZhiXuePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._watch_task: asyncio.Task | None = None

        cfg = load_watch_config()
        if cfg.get("enabled"):
            self._start_watch(cfg.get("interval", 300))

    async def terminate(self):
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

    def _start_watch(self, interval: int):
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = asyncio.ensure_future(self._watch_loop(interval))
        logger.info(f"成绩监听已启动，间隔 {interval}s")

    async def _stop_watch(self):
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        logger.info("成绩监听已停止")

    async def _watch_loop(self, interval: int):
        while True:
            try:
                await asyncio.sleep(interval)
                cfg = load_watch_config()
                if not cfg.get("enabled"):
                    continue
                group_umo = cfg.get("group_umo", "")
                if not group_umo:
                    continue
                users = load_users()
                if not users:
                    continue

                for qq_id in users:
                    try:
                        new_items = await zhixue_manager.check_new_scores(qq_id)
                        if new_items:
                            for item in new_items:
                                msg = format_marks_table(
                                    users[qq_id].get("username", qq_id),
                                    item["exam"],
                                    item["subjects"],
                                )
                                msg = f"📢 新成绩通知！\n{msg}"
                                try:
                                    chain = MessageChain().message(msg)
                                    await self.context.send_message(group_umo, chain)
                                except Exception:
                                    chain = [Comp.Plain(msg)]
                                    await self.context.send_message(group_umo, chain)
                    except Exception as e:
                        logger.warning(f"监听用户 {qq_id} 失败: {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"监听循环异常: {e}")
                await asyncio.sleep(10)

    @filter.command_group("zx")
    def zx(self):
        pass

    @zx.command("help")
    async def zx_help(self, event: AstrMessageEvent):
        help_text = (
            "智学网成绩查询\n"
            "  /zx bind <用户名> <密码>  - 绑定智学网账号\n"
            "  /zx exams                 - 查看考试列表\n"
            "  /zx marks [考试名]        - 查看成绩\n"
            "  /zx watch [间隔分钟]      - 开启成绩监听（群聊管理员）\n"
            "  /zx unwatch               - 停止监听\n"
            "  /zx status                - 查看监听状态\n"
            "  /zx users                 - 查看已绑定用户（管理员）\n"
            "  /zx unbind                - 解绑账号"
        )
        yield event.plain_result(help_text)

    @zx.command("bind")
    async def zx_bind(self, event: AstrMessageEvent, username: str, password: str):
        user_id = event.get_sender_id()
        zhixue_manager.register_user(user_id, username, password)
        yield event.plain_result(f"✅ 已绑定账号: {username}\n可使用 /zx exams 查看考试，/zx marks 查看成绩")

    @zx.command("unbind")
    async def zx_unbind(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        zhixue_manager.remove_user(user_id)
        yield event.plain_result("已解绑智学网账号")

    @zx.command("exams")
    async def zx_exams(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        yield event.plain_result("⏳ 正在查询考试列表...")
        try:
            exams = await zhixue_manager.get_exams(user_id)
            yield event.plain_result(format_exams_table(exams))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {type(e).__name__}: {e}")

    @zx.command("marks")
    async def zx_marks(self, event: AstrMessageEvent, exam_name: str = None):
        user_id = event.get_sender_id()
        yield event.plain_result("⏳ 正在查询成绩...")
        try:
            user_name, exam_info, subjects = await zhixue_manager.get_marks(user_id, exam_name)
            ename = exam_name or exam_info.get("name", "最新")
            yield event.plain_result(format_marks_table(user_name, ename, subjects))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {type(e).__name__}: {e}")

    @zx.command("watch")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def zx_watch(self, event: AstrMessageEvent, interval: int = 5):
        if interval < 1:
            yield event.plain_result("监听间隔不能小于1分钟")
            return
        cfg = {
            "enabled": True,
            "interval": interval * 60,
            "group_umo": event.unified_msg_origin,
        }
        save_watch_config(cfg)
        self._start_watch(interval * 60)
        users = load_users()
        user_list = ", ".join([u.get("username", qid) for qid, u in users.items()]) if users else "(无绑定用户)"
        yield event.plain_result(
            f"✅ 成绩监听已开启\n"
            f"  间隔: {interval} 分钟\n"
            f"  监听用户: {user_list}\n"
            f"  有新成绩时会在此群通知\n"
            f"  发送 /zx unwatch 停止监听"
        )

    @zx.command("unwatch")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def zx_unwatch(self, event: AstrMessageEvent):
        cfg = load_watch_config()
        cfg["enabled"] = False
        save_watch_config(cfg)
        await self._stop_watch()
        yield event.plain_result("✅ 成绩监听已停止")

    @zx.command("status")
    async def zx_status(self, event: AstrMessageEvent):
        cfg = load_watch_config()
        users = load_users()
        is_running = cfg.get("enabled", False) and self._watch_task and not self._watch_task.done()

        lines = ["【智学网监听状态】", ""]
        lines.append(f"  监听: {'运行中' if is_running else '停用'}")
        if is_running:
            lines.append(f"  间隔: {cfg['interval'] // 60} 分钟")
        lines.append(f"  绑定用户: {len(users)} 人")
        for qid, u in users.items():
            lines.append(f"    - {u['username']}")
        yield event.plain_result("\n".join(lines))

    @zx.command("users")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def zx_users(self, event: AstrMessageEvent):
        users = load_users()
        if not users:
            yield event.plain_result("暂无绑定用户")
            return
        lines = ["【已绑定用户】", ""]
        for qid, u in users.items():
            lines.append(f"  {u['username']} (QQ: {qid})")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="zhixue_get_exams")
    async def llm_get_exams(self, event: AstrMessageEvent) -> MessageEventResult:
        '''获取智学网当前用户的考试列表，返回所有考试的名称。

        Returns:
            考试列表的文本
        '''
        user_id = event.get_sender_id()
        try:
            exams = await zhixue_manager.get_exams(user_id)
            yield event.plain_result(format_exams_table(exams))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @filter.llm_tool(name="zhixue_get_marks")
    async def llm_get_marks(self, event: AstrMessageEvent, exam_name: str) -> MessageEventResult:
        '''获取智学网指定考试的成绩详情，包括各科目分数和总分。

        Args:
            exam_name(string): 考试名称（从zhixue_get_exams返回的列表中选择）

        Returns:
            包含各科成绩和总分的文本
        '''
        user_id = event.get_sender_id()
        try:
            user_name, exam_info, subjects = await zhixue_manager.get_marks(user_id, exam_name)
            yield event.plain_result(format_marks_table(user_name, exam_name, subjects))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @filter.llm_tool(name="zhixue_get_latest_marks")
    async def llm_get_latest(self, event: AstrMessageEvent) -> MessageEventResult:
        '''获取智学网最新一次考试的成绩，包含各科分数和总分。

        Returns:
            包含各科最新成绩和总分的文本
        '''
        user_id = event.get_sender_id()
        try:
            user_name, exam_info, subjects = await zhixue_manager.get_latest_marks(user_id)
            ename = exam_info.get("name", "最新考试")
            yield event.plain_result(format_marks_table(user_name, ename, subjects))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")