import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig

from zhixue_core import (
    zhixue_manager,
    load_bindings,
    format_exams_table,
    format_marks_table,
)


class ZhiXuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._watch_task: asyncio.Task | None = None

        if config.get("watch_enabled", False):
            self._start_watch()

    async def terminate(self):
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

    def _start_watch(self):
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = asyncio.ensure_future(self._watch_loop())
        logger.info("成绩监听已启动")

    async def _stop_watch(self):
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        logger.info("成绩监听已停止")

    async def _watch_loop(self):
        while True:
            try:
                interval = self.config.get("watch_interval", 5)
                await asyncio.sleep(interval * 60)
                if not self.config.get("watch_enabled", False):
                    continue

                group_umo = self.config.get("watch_group_umo", "")
                config_users = self.config.get("users", [])
                if not config_users:
                    continue

                bindings = load_bindings()
                for qq_id, username in bindings.items():
                    try:
                        new_items = await zhixue_manager.check_new_scores(qq_id, config_users)
                        if new_items:
                            for item in new_items:
                                msg_text = format_marks_table(
                                    username, item["exam"], item["subjects"]
                                )
                                msg_text = f"📢 新成绩通知！\n{msg_text}"
                                if group_umo:
                                    from astrbot.api.message_components import Plain
                                    from astrbot.api.event import MessageChain
                                    chain = MessageChain().message(msg_text)
                                    try:
                                        await self.context.send_message(group_umo, chain)
                                    except Exception:
                                        await self.context.send_message(
                                            group_umo, [Plain(msg_text)]
                                        )
                    except Exception as e:
                        logger.warning(f"监听用户 {qq_id}({username}) 失败: {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"监听循环异常: {e}")
                await asyncio.sleep(10)

    def _get_user_password(self, username: str) -> str | None:
        for u in self.config.get("users", []):
            if u.get("username") == username:
                return u.get("password")
        return None

    @filter.command_group("zx")
    def zx(self):
        pass

    @zx.command("help")
    async def zx_help(self, event: AstrMessageEvent):
        help_text = (
            "智学网成绩查询\n"
            "  /zx bind <用户名>  - 绑定智学网账号\n"
            "  /zx exams          - 查看考试列表\n"
            "  /zx marks [考试名] - 查看成绩\n"
            "  /zx watch on [间隔]|off - 开启/停止成绩监听\n"
            "  /zx status         - 查看状态\n"
            "  /zx unbind         - 解绑账号"
        )
        yield event.plain_result(help_text)

    @zx.command("bind")
    async def zx_bind(self, event: AstrMessageEvent, username: str):
        config_users = self.config.get("users", [])
        ok = zhixue_manager.bind_user(event.get_sender_id(), username, config_users)
        if ok:
            yield event.plain_result(f"✅ 已绑定账号: {username}\n可使用 /zx exams 查看考试")
        else:
            valid = [u.get("username", "?") for u in config_users]
            yield event.plain_result(
                f"账号 {username} 不在配置列表中\n"
                f"请管理员在插件配置中添加该账号\n"
                f"当前可用账号: {', '.join(valid) if valid else '无'}"
            )

    @zx.command("unbind")
    async def zx_unbind(self, event: AstrMessageEvent):
        zhixue_manager.unbind_user(event.get_sender_id())
        yield event.plain_result("已解绑智学网账号")

    @zx.command("exams")
    async def zx_exams(self, event: AstrMessageEvent):
        yield event.plain_result("⏳ 正在查询考试列表...")
        try:
            config_users = self.config.get("users", [])
            exams = await zhixue_manager.get_exams(event.get_sender_id(), config_users)
            yield event.plain_result(format_exams_table(exams))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @zx.command("marks")
    async def zx_marks(self, event: AstrMessageEvent, exam_name: str = None):
        yield event.plain_result("⏳ 正在查询成绩...")
        try:
            config_users = self.config.get("users", [])
            user_name, exam_info, subjects = await zhixue_manager.get_marks(
                event.get_sender_id(), config_users, exam_name
            )
            ename = exam_name or exam_info.get("name", "最新")
            yield event.plain_result(format_marks_table(user_name, ename, subjects))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @zx.command("watch")
    async def zx_watch(self, event: AstrMessageEvent, action: str = "on", interval: int = 5):
        if action == "off":
            self.config["watch_enabled"] = False
            self.config.save_config()
            await self._stop_watch()
            yield event.plain_result("✅ 成绩监听已停止")
            return

        if interval < 1:
            yield event.plain_result("监听间隔不能小于1分钟")
            return

        group_umo = event.unified_msg_origin if hasattr(event, "unified_msg_origin") else ""
        self.config["watch_enabled"] = True
        self.config["watch_interval"] = interval
        if group_umo:
            self.config["watch_group_umo"] = group_umo
        self.config.save_config()
        self._start_watch()

        bindings = load_bindings()
        user_list = ", ".join(bindings.values()) if bindings else "(无绑定用户)"
        yield event.plain_result(
            f"✅ 成绩监听已开启\n"
            f"  间隔: {interval} 分钟\n"
            f"  监听用户: {user_list}\n"
            f"  发送 /zx watch off 停止"
        )

    @zx.command("status")
    async def zx_status(self, event: AstrMessageEvent):
        is_running = self._watch_task and not self._watch_task.done()
        bindings = load_bindings()
        config_users = self.config.get("users", [])
        valid_usernames = {u.get("username") for u in config_users}

        lines = ["【智学网状态】", ""]
        lines.append(f"  监听: {'运行中' if is_running else '停用'}")
        if is_running:
            lines.append(f"  间隔: {self.config.get('watch_interval', 5)} 分钟")
        lines.append(f"  配置账号: {len(config_users)} 个")
        lines.append(f"  已绑定: {len(bindings)} 人")

        for qq_id, username in bindings.items():
            valid = "✅" if username in valid_usernames else "⚠️已失效"
            lines.append(f"    - {username} {valid}")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="zhixue_get_exams")
    async def llm_get_exams(self, event: AstrMessageEvent) -> MessageEventResult:
        user_id = event.get_sender_id()
        try:
            config_users = self.config.get("users", [])
            exams = await zhixue_manager.get_exams(user_id, config_users)
            yield event.plain_result(format_exams_table(exams))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @filter.llm_tool(name="zhixue_get_marks")
    async def llm_get_marks(self, event: AstrMessageEvent, exam_name: str) -> MessageEventResult:
        user_id = event.get_sender_id()
        try:
            config_users = self.config.get("users", [])
            user_name, exam_info, subjects = await zhixue_manager.get_marks(
                user_id, config_users, exam_name
            )
            yield event.plain_result(format_marks_table(user_name, exam_name, subjects))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @filter.llm_tool(name="zhixue_get_latest_marks")
    async def llm_get_latest(self, event: AstrMessageEvent) -> MessageEventResult:
        user_id = event.get_sender_id()
        try:
            config_users = self.config.get("users", [])
            user_name, exam_info, subjects = await zhixue_manager.get_marks(
                user_id, config_users
            )
            ename = exam_info.get("name", "最新考试")
            yield event.plain_result(format_marks_table(user_name, ename, subjects))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")