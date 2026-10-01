import asyncio

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image
from astrbot.core.agent.message import TextPart

from .zhixue_core import (
    zhixue_manager,
    load_accounts,
    find_username_by_id,
    format_exams_table,
    format_marks_table,
)

# ---------------------------------------------------------------------------
# HTML templates — black/white minimalist pixel style
# Fixed-width body (480px) to match viewport and eliminate blank space
# ---------------------------------------------------------------------------

EXAMS_TMPL = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Courier New',Consolas,'Microsoft YaHei',monospace; background:#fff; min-height:100vh; padding:20px; color:#000; }
.container { max-width:480px; margin:0 auto; }
</style></head>
<body><div class="container">
  <div style="font-size:18px; font-weight:bold; border-bottom:2px solid #000; padding-bottom:8px; margin-bottom:12px; letter-spacing:2px;">
    EXAM LIST
  </div>
  {% for exam in exams %}
  <div style="padding:6px 0; border-bottom:1px dashed #ccc; font-size:14px;">
    <span style="font-weight:bold; margin-right:6px;">{{ "%02d"|format(loop.index) }}.</span>
    <span>{{ exam.name }}</span>
    {% if exam.is_final %}<span style="margin-left:6px; font-size:11px; color:#fff; background:#000; padding:1px 4px;">FINAL</span>{% endif %}
  </div>
  {% endfor %}
  <div style="margin-top:12px; font-size:12px; color:#888; border-top:1px solid #000; padding-top:8px;">
    &gt; /zx marks &lt;no&gt;
  </div>
</div></body></html>'''

MARKS_TMPL = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Courier New',Consolas,'Microsoft YaHei',monospace; background:#fff; min-height:100vh; padding:20px; color:#000; }
.container { max-width:480px; margin:0 auto; }
</style></head>
<body><div class="container">
  <div style="font-size:16px; font-weight:bold; border-bottom:2px solid #000; padding-bottom:8px; margin-bottom:4px; letter-spacing:1px;">
    {{ student_name }}
  </div>
  <div style="font-size:12px; color:#666; margin-bottom:14px;">
    {{ exam_name }}
  </div>
  <table style="width:100%; border-collapse:collapse; font-size:14px;">
    <thead>
      <tr style="border-bottom:2px solid #000;">
        <th style="text-align:left; padding:6px 4px; font-weight:bold;">SUBJECT</th>
        <th style="text-align:right; padding:6px 4px; font-weight:bold;">SCORE</th>
        {% if has_rank %}<th style="text-align:right; padding:6px 4px; font-weight:bold;">RANK</th>{% endif %}
      </tr>
    </thead>
    <tbody>
      {% for s in subjects %}
      <tr style="border-bottom:1px dashed #ddd;">
        <td style="padding:6px 4px;">{{ s.subject }}</td>
        <td style="padding:6px 4px; text-align:right; font-weight:bold;">{{ "%.1f"|format(s.score) if s.score is not none else "-" }}</td>
        {% if has_rank %}<td style="padding:6px 4px; text-align:right; color:#666;">{{ s.class_rank if s.class_rank else "-" }}</td>{% endif %}
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% if total_info and total_info.score is not none %}
  <div style="margin-top:12px; padding:8px; border:2px solid #000; display:flex; justify-content:space-between; align-items:center;">
    <span style="font-size:14px; font-weight:bold;">{{ total_info.name or 'TOTAL' }}</span>
    <span style="font-size:18px; font-weight:bold;">{{ "%.1f"|format(total_info.score) }}</span>
  </div>
  {% endif %}
</div></body></html>'''


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
        self._watch_task = asyncio.create_task(self._watch_loop())
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

                config_users = self.config.get("users", [])
                if not config_users:
                    continue

                watch_groups = self.config.get("watch_groups", [])
                # Build per-username group map: username -> [umo, ...]
                groups_by_user: dict[str, list[str]] = {}
                for g in watch_groups:
                    uname = g.get("username", "")
                    umo = g.get("umo", "")
                    if uname and umo:
                        groups_by_user.setdefault(uname, []).append(umo)

                accounts = load_accounts(config_users)

                for username, data in accounts.items():
                    bound_ids = data.get("bound_ids", [])
                    if not bound_ids:
                        continue
                    try:
                        first_id = bound_ids[0].get("user_id", "")
                        new_items = await zhixue_manager.check_new_scores(first_id, config_users)
                        if new_items:
                            for item in new_items:
                                change_type = item.get("change_type", "new_exam")
                                if change_type == "new_exam":
                                    header = f"[新考试] {item['exam']}"
                                    msg_text = format_marks_table(
                                        username, item["exam"], item["subjects"]
                                    )
                                else:
                                    header = f"[成绩更新] {item['exam']}"
                                    lines = [f"【{username}】 {item['exam']}"]
                                    for s in item["subjects"]:
                                        subj = s.get("subject", "?")
                                        score = s.get("score")
                                        score_str = f"{score:.1f}" if score is not None else "-"
                                        lines.append(f"  {subj}: {score_str}")
                                    msg_text = "\n".join(lines)
                                msg_text = f"{header}\n{msg_text}"
                                # Push to bound IDs (private chat)
                                for binding in bound_ids:
                                    uid = binding.get("user_id", "")
                                    pid = binding.get("platform_id", "")
                                    if uid and pid:
                                        umo = f"{pid}:FriendMessage:{uid}"
                                        chain = MessageChain().message(msg_text)
                                        try:
                                            await self.context.send_message(umo, chain)
                                        except Exception as e:
                                            logger.warning(f"发送通知到 {umo} 失败: {e}")
                                # Push to this account's watch groups
                                for group_umo in groups_by_user.get(username, []):
                                    chain = MessageChain().message(msg_text)
                                    try:
                                        await self.context.send_message(group_umo, chain)
                                    except Exception as e:
                                        logger.warning(f"发送通知到 {group_umo} 失败: {e}")
                            logger.info(f"[监听] {username}: 发现 {len(new_items)} 条新成绩")
                        else:
                            logger.info(f"[监听] {username}: 无变化")
                    except Exception as e:
                        logger.warning(f"[监听] {username} 失败: {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[监听] 循环异常: {e}")
                await asyncio.sleep(10)

    # -----------------------------------------------------------------------
    # command group: /zx
    # -----------------------------------------------------------------------

    @filter.command_group("zx")
    def zx(self):
        pass

    @zx.command("help")
    async def zx_help(self, event: AstrMessageEvent):
        '''查看帮助信息'''
        help_text = (
            "智学网成绩查询\n"
            "  /zx bind <用户名>    - 绑定智学网账号\n"
            "  /zx exams            - 查看考试列表\n"
            "  /zx marks [序号/名称] - 查看成绩\n"
            "  /zx sheet <学科> [考试] - 查看答题卡(带批改标注长图)\n"
            "  /zx watch on [间隔]|off - 开启/停止成绩监听\n"
            "  /zx status           - 查看状态\n"
            "  /zx unbind           - 解绑账号\n"
            "\n发送 /zx marks 1 查询最新考试，/zx marks 3 查询第3新的考试"
        )
        yield event.plain_result(help_text)

    @zx.command("bind")
    async def zx_bind(self, event: AstrMessageEvent, username: str):
        '''绑定智学网账号'''
        config_users = self.config.get("users", [])
        umo = getattr(event, "unified_msg_origin", "") or ""
        platform_id = umo.split(":")[0] if umo else "unknown"
        ok = zhixue_manager.bind_user(event.get_sender_id(), username, config_users, platform_id)
        if ok:
            yield event.plain_result(f"已绑定账号: {username}\n可使用 /zx exams 查看考试")
        else:
            valid = [u.get("username", "?") for u in config_users]
            yield event.plain_result(
                f"账号 {username} 不在配置列表中\n"
                f"请管理员在插件配置中添加该账号\n"
                f"当前可用账号: {', '.join(valid) if valid else '无'}"
            )

    @zx.command("unbind")
    async def zx_unbind(self, event: AstrMessageEvent):
        '''解绑当前账号'''
        config_users = self.config.get("users", [])
        zhixue_manager.unbind_user(event.get_sender_id(), config_users)
        yield event.plain_result("已解绑智学网账号")

    async def _render_image(self, template: str, data: dict) -> str:
        """Render HTML template to image with proper viewport settings."""
        return await self.html_render(
            template,
            data,
            options={
                "quality": 95,
                "device_scale_factor_level": "ultra",
                "viewport_width": 520,
                "type": "png",
            },
        )

    @zx.command("exams")
    async def zx_exams(self, event: AstrMessageEvent):
        '''查看考试列表'''
        yield event.plain_result("正在查询考试列表...")
        try:
            config_users = self.config.get("users", [])
            exams = await zhixue_manager.get_exams(event.get_sender_id(), config_users)
            try:
                data = {"exams": [{"name": e.get("name", "?"), "is_final": e.get("is_final", False)} for e in exams]}
                url = await self._render_image(EXAMS_TMPL, data)
                yield event.image_result(url)
            except Exception as t2i_err:
                logger.warning(f"t2i渲染失败，降级纯文本: {t2i_err}")
                yield event.plain_result(format_exams_table(exams))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @zx.command("marks")
    async def zx_marks(self, event: AstrMessageEvent, exam_param: str = None):
        '''查询成绩，可指定考试名或序号'''
        yield event.plain_result("正在查询成绩...")
        try:
            config_users = self.config.get("users", [])
            user_name, exam_info, subjects, total_info = await zhixue_manager.get_marks(
                event.get_sender_id(), config_users, exam_param
            )
            ename = exam_param or exam_info.get("name", "最新")
            try:
                has_rank = any(s.get("class_rank") for s in subjects)
                data = {
                    "student_name": user_name,
                    "exam_name": ename,
                    "subjects": subjects,
                    "total_info": total_info,
                    "has_rank": has_rank,
                }
                url = await self._render_image(MARKS_TMPL, data)
                yield event.image_result(url)
            except Exception as t2i_err:
                logger.warning(f"t2i渲染失败，降级纯文本: {t2i_err}")
                yield event.plain_result(format_marks_table(user_name, ename, subjects, total_info))
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询失败: {e}")

    @zx.command("sheet")
    async def zx_sheet(self, event: AstrMessageEvent, subject_name: str, exam_param: str = None):
        '''查看答题卡(批改标注长图)'''
        yield event.plain_result(f"正在查询「{subject_name}」答题卡并渲染批改标注...")
        try:
            config_users = self.config.get("users", [])
            sheet_name, temp_paths = await zhixue_manager.get_sheet(
                event.get_sender_id(), config_users, subject_name, exam_param
            )
            try:
                for path in temp_paths:
                    yield event.image_result(path)
            finally:
                import os
                for path in temp_paths:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询答题卡失败: {e}")

    @zx.command("watch")
    async def zx_watch(self, event: AstrMessageEvent, action: str = "on", interval: int = 5):
        '''开启/停止成绩监听'''
        if action == "off":
            self.config["watch_enabled"] = False
            self.config.save_config()
            await self._stop_watch()
            yield event.plain_result("成绩监听已停止")
            return

        if interval < 1:
            yield event.plain_result("监听间隔不能小于1分钟")
            return

        self.config["watch_enabled"] = True
        self.config["watch_interval"] = interval
        self.config.save_config()
        self._start_watch()

        config_users = self.config.get("users", [])
        accounts = load_accounts(config_users)
        watch_groups = self.config.get("watch_groups", [])

        # Build per-account group summary
        groups_by_user: dict[str, list[str]] = {}
        for g in watch_groups:
            uname = g.get("username", "")
            gname = g.get("group_name", g.get("umo", "?"))
            if uname:
                groups_by_user.setdefault(uname, []).append(gname)

        lines = [f"成绩监听已开启 (间隔: {interval} 分钟)", ""]
        for username in accounts:
            bound = accounts[username].get("bound_ids", [])
            ids_str = ", ".join(b.get("user_id", "?") for b in bound)
            groups = groups_by_user.get(username, [])
            group_str = ", ".join(groups) if groups else "(无)"
            lines.append(f"  {username}")
            lines.append(f"    绑定: {ids_str}")
            lines.append(f"    群组: {group_str}")

        if not accounts:
            lines.append("  (无绑定账号)")

        lines.append("")
        lines.append("发送 /zx watch off 停止")
        yield event.plain_result("\n".join(lines))

    @zx.command("status")
    async def zx_status(self, event: AstrMessageEvent):
        '''查看当前状态'''
        is_running = self._watch_task and not self._watch_task.done()
        config_users = self.config.get("users", [])
        accounts = load_accounts(config_users)
        valid_usernames = {u.get("username") for u in config_users}
        watch_groups = self.config.get("watch_groups", [])

        # Build per-account group map
        groups_by_user: dict[str, list[str]] = {}
        for g in watch_groups:
            uname = g.get("username", "")
            gname = g.get("group_name", g.get("umo", "?"))
            if uname:
                groups_by_user.setdefault(uname, []).append(gname)

        lines = ["[智学网状态]", ""]
        lines.append(f"  监听: {'运行中' if is_running else '停用'}")
        if is_running:
            lines.append(f"  间隔: {self.config.get('watch_interval', 5)} 分钟")
        lines.append(f"  配置账号: {len(config_users)} 个")
        lines.append(f"  已绑定: {len(accounts)} 个账号")

        for username, data in accounts.items():
            valid = "[OK]" if username in valid_usernames else "[INVALID]"
            bound = data.get("bound_ids", [])
            ids_str = ", ".join(b.get("user_id", "?") for b in bound)
            groups = groups_by_user.get(username, [])
            group_str = ", ".join(groups) if groups else "(无)"
            lines.append(f"    {username} {valid}")
            lines.append(f"      绑定: {ids_str}")
            lines.append(f"      群组: {group_str}")
        yield event.plain_result("\n".join(lines))

    # -----------------------------------------------------------------------
    # LLM tools (function calling)
    # -----------------------------------------------------------------------

    @filter.on_llm_request()
    async def inject_user_info(self, event: AstrMessageEvent, req):
        """Inject user context for LLM tool calling."""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else ""

        info = f"<user_context>\n发送者: {sender_name}(ID:{sender_id})\n"

        for comp in getattr(event.message_obj, "message", []):
            if isinstance(comp, At):
                info += f"被@的用户: ID:{str(comp.qq)}\n"

        info += "</user_context>"
        req.extra_user_content_parts.append(TextPart(text=info))

    @filter.llm_tool(name="zhixue_list_exams")
    async def llm_list_exams(self, event: AstrMessageEvent, user_id: str) -> MessageEventResult:
        '''查询学生的考试列表。在查询成绩前必须先调用此工具获取准确的考试名称。

        Args:
            user_id(string): 要查询的用户ID。从上下文 user_context 获取：查自己用发送者ID，查别人用被@用户的ID
        '''
        try:
            config_users = self.config.get("users", [])
            accounts = load_accounts(config_users)
            zx_username = find_username_by_id(accounts, user_id)
            if not zx_username:
                return f"用户 {user_id} 未绑定智学网账号，请先使用 /zx bind 绑定"
            exams = await zhixue_manager.get_exams(user_id, config_users)
            return format_exams_table(exams)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"查询失败: {e}"

    @filter.llm_tool(name="zhixue_query_score")
    async def llm_query_score(self, event: AstrMessageEvent, user_id: str, exam_name: str = "") -> MessageEventResult:
        '''查询学生某次考试的成绩。不传 exam_name 则查询最新考试，传 exam_name 则查询指定考试。

        Args:
            user_id(string): 要查询的用户ID。从上下文 user_context 获取：查自己用发送者ID，查别人用被@用户的ID
            exam_name(string): 考试名称关键词，支持模糊匹配。不传则查询最新考试
        '''
        try:
            config_users = self.config.get("users", [])
            accounts = load_accounts(config_users)
            zx_username = find_username_by_id(accounts, user_id)
            if not zx_username:
                return f"用户 {user_id} 未绑定智学网账号，请先使用 /zx bind 绑定"
            exam_param = exam_name if exam_name else None
            user_name, exam_info, subjects, total_info = await zhixue_manager.get_marks(
                user_id, config_users, exam_param
            )
            ename = exam_name or exam_info.get("name", "最新考试")
            return format_marks_table(user_name, ename, subjects, total_info)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"查询失败: {e}"

    @filter.llm_tool(name="zhixue_query_sheet")
    async def llm_query_sheet(self, event: AstrMessageEvent, user_id: str, subject_name: str, exam_name: str = "") -> MessageEventResult:
        '''查询学生某次考试某学科的答题卡图片。必须指定学科名称，考试名可选。

        Args:
            user_id(string): 要查询的用户ID。从上下文 user_context 获取：查自己用发送者ID，查别人用被@用户的ID
            subject_name(string): 学科名称，如"数学"、"语文"等，支持模糊匹配
            exam_name(string): 考试名称关键词，可选，不传则查询最新考试
        '''
        try:
            config_users = self.config.get("users", [])
            accounts = load_accounts(config_users)
            zx_username = find_username_by_id(accounts, user_id)
            if not zx_username:
                yield event.plain_result(f"用户 {user_id} 未绑定智学网账号，请先使用 /zx bind 绑定")
                return
            exam_param = exam_name if exam_name else None
            sheet_name, temp_paths = await zhixue_manager.get_sheet(
                user_id, config_users, subject_name, exam_param
            )
            import os
            try:
                for path in temp_paths:
                    yield event.image_result(path)
                yield event.plain_result(
                    f"已发送「{sheet_name}」答题卡图片，共 {len(temp_paths)} 页。"
                )
            finally:
                for path in temp_paths:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        except ValueError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"查询答题卡失败: {e}")
