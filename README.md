# Todo Reminder

<p align="center">
  <img src="logo.png" alt="Todo Reminder" width="160">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v0.1.0-blue" alt="version">
  <img src="https://img.shields.io/badge/AstrBot-plugin-7c4dff" alt="AstrBot plugin">
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab" alt="python">
</p>

AstrBot 私聊本地待办与提醒插件。支持待办清单、独立提醒、待办绑定提醒、重复提醒、工作日/节假日过滤，以及通过 LLM 自然语言创建和管理待办提醒。

## 目录

- [功能特性](#功能特性)
- [安装](#安装)
- [快速开始](#快速开始)
- [使用范围](#使用范围)
- [参数规则](#参数规则)
- [命令总览](#命令总览)
- [命令详解](#命令详解)
- [自然语言和 LLM 工具](#自然语言和-llm-工具)
- [配置项](#配置项)
- [数据文件](#数据文件)
- [常见问题](#常见问题)
- [兼容性](#兼容性)
- [开发验证](#开发验证)
- [本地上传打包](#本地上传打包)
- [反馈与贡献](#反馈与贡献)
- [许可证](#许可证)

## 功能特性

- 私聊专用：每个私聊会话独立保存待办和提醒。
- 待办管理：添加、查看、修改、完成、取消完成、删除、清空。
- 提醒管理：创建独立提醒，或在创建待办时绑定提醒。
- 重复提醒：支持一次性、每天、每周、每月、每年。
- 工作日/节假日：支持仅工作日触发或仅法定节假日触发。
- 自然语言：可通过 AstrBot 的 LLM 工具理解“明天 9 点提醒我开会”等表达。
- 到点文案：可用当前 LLM 生成自然语言提醒内容，无模型时自动回退固定文本。
- 数据可靠性：数据本地 JSON 保存，原子写入；损坏数据会自动备份；异常写入会尽量回滚内存状态。
- 离线补发：机器人离线期间错过的一次性提醒会在下次启动后补发，发送成功后自动清理。

## 安装

任选一种方式：

**方式一：AstrBot 插件市场 / WebUI 上传**

在 AstrBot WebUI 的插件市场搜索安装，或在「插件管理」中上传打包好的 zip（见[本地上传打包](#本地上传打包)）。

**方式二：git clone 到插件目录**

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/interpy/astrbot_plugin_todo_reminder.git
```

依赖会根据 `requirements.txt` 自动安装；如需手动安装：

```bash
pip install -r requirements.txt
```

依赖说明：

- `apscheduler`：注册和触发提醒任务。
- `aiohttp`：获取节假日数据。
- `tzdata`：为缺少系统时区数据库的环境提供 IANA 时区数据。

安装后在 AstrBot 中重载插件即可，使用 `/todo help` 验证是否生效。

## 快速开始

```text
/todo add 写周报
/todo add 写周报 18:00 daily workday
/todo remind 喝水 10:00 daily
/todo reminders
/todo ls all
/todo done 1
```

自然语言示例：

```text
明天上午9点提醒我开会
添加一个待办：写周报，周五下午6点提醒我
提醒我每天 10 点喝水
```

## 使用范围

插件默认仅支持私聊。群聊中触发 `/todo` 时：

- `reply_in_group=false`：静默忽略。
- `reply_in_group=true`：回复“本插件仅支持私聊，请私聊我使用待办和提醒。”

## 参数规则

### 序号和选择器

需要 `<序号>` 的命令一般支持以下选择方式：

- 列表中的数字序号，例如 `1`。
- 条目完整 id，或不少于 8 个字符且唯一的 id 前缀。
- 内容关键词；只有匹配到唯一条目时才会生效，多个条目同时匹配会视为未找到。

建议先用 `/todo ls` 或 `/todo reminders` 查看序号后再操作。

### 内容长度

- 待办内容和提醒内容最多 500 个字符。
- 待办备注最多 1000 个字符。

### 时间格式

命令参数中的时间支持：

```text
HH:MM
HHMM
YYYY-MM-DD HH:MM
YYYY/MM/DD HH:MM
YYYY-MM-DD-HH:MM
MM-DD-HH:MM
YYYYMMDDHHMM
MMDDHHMM
```

说明：

- `HH:MM` 和 `HHMM` 表示今天的时间；如果已经过了，会自动顺延到明天。
- `MM-DD-HH:MM` 和 `MMDDHHMM` 使用当前年份；如果日期已过，会顺延到下一年。
- 带年份的过去时间会被拒绝，避免误创建已经过期的提醒。
- 中文冒号 `：` 会自动按英文冒号处理。

### 重复类型

`repeat` 可选值：

```text
none,daily,weekly,monthly,yearly
```

含义：

- `none`：一次性提醒，默认值。
- `daily`：每天在指定小时和分钟提醒。
- `weekly`：每周在指定星期、小时和分钟提醒。
- `monthly`：每月在指定日期、小时和分钟提醒。
- `yearly`：每年在指定月日、小时和分钟提醒。

### 工作日和节假日

`holiday_type` 可选值：

```text
none,workday,holiday
```

含义：

- `none`：不限制日期类型，默认值。
- `workday`：仅工作日触发。
- `holiday`：仅法定节假日触发。

一次性提醒带 `workday` 或 `holiday` 时，如果目标日期不满足条件，会保持小时分钟不变，顺延到下一个满足条件的日期。重复提醒带 `workday` 或 `holiday` 时，会按重复规则注册，到点后再判断当天是否满足条件，不满足则跳过。

节假日数据来自 `https://timor.tech/api/holiday/year/{year}`，获取失败时使用周末兜底判断，并缓存失败状态以避免频繁请求。

## 命令总览

### 帮助

| 命令 | 作用 |
| --- | --- |
| `/todo help` | 查看插件内置帮助。 |

### 待办命令

| 命令 | 作用 |
| --- | --- |
| `/todo add <内容> [时间] [repeat] [workday\|holiday]` | 添加待办；如果提供时间，同时创建一个绑定到该待办的提醒。 |
| `/todo ls [open\|done\|all]` | 查看待办和提醒。默认只显示未完成待办，同时显示提醒列表。 |
| `/todo list [open\|done\|all]` | `/todo ls` 的别名。 |
| `/todo all` | 查看全部待办，相当于 `/todo ls all`。 |
| `/todo open` | 查看未完成待办，相当于 `/todo ls open`。 |
| `/todo completed` | 查看已完成待办，相当于 `/todo ls done`。 |
| `/todo done <序号>` | 将指定待办标记为已完成。 |
| `/todo undone <序号>` | 取消指定待办的已完成状态。 |
| `/todo edit <序号> <新内容>` | 修改指定待办内容。 |
| `/todo rm <序号>` | 删除指定待办；如果有绑定提醒，会同步删除这些提醒。 |
| `/todo del <序号>` | `/todo rm` 的别名。 |
| `/todo clear` | 删除当前私聊会话中的所有待办，并同步删除这些待办绑定的提醒。 |

### 提醒命令

| 命令 | 作用 |
| --- | --- |
| `/todo remind <内容> <时间> [repeat] [workday\|holiday]` | 创建独立提醒，不绑定待办。 |
| `/todo reminders` | 查看当前私聊会话中的提醒列表。 |
| `/todo rlist` | `/todo reminders` 的别名。 |
| `/todo rclear` | 删除所有提醒。 |
| `/todo rclear <提醒关键词>` | 删除提醒内容包含关键词的所有提醒。 |
| `/todo rclear-todo <待办关键词>` | 删除所有关联到匹配待办的提醒。 |
| `/todo rclear_todo <待办关键词>` | `/todo rclear-todo` 的别名。 |
| `/todo redt <序号> [新内容] [时间] [repeat] [workday\|holiday]` | 修改提醒内容、时间、重复规则或日期过滤。 |
| `/todo rtime <序号> <时间> [repeat] [workday\|holiday]` | 修改提醒时间、重复规则或日期过滤。 |
| `/todo rtext <序号> <新内容>` | 修改提醒内容。 |
| `/todo rrm <序号>` | 删除指定提醒。 |
| `/todo rrm all` | 删除所有提醒。 |

## 命令详解

### `/todo add`

添加待办。只写内容时仅创建待办：

```text
/todo add 写周报
```

提供时间时，会同时创建一个绑定到待办的提醒：

```text
/todo add 写周报 18:00
/todo add 写周报 18:00 daily workday
```

绑定提醒的提醒内容默认与待办内容相同。删除这个待办时，绑定提醒也会被删除。

### `/todo ls`、`/todo list`、`/todo all`、`/todo open`、`/todo completed`

查看待办和提醒。

```text
/todo ls
/todo ls all
/todo ls done
/todo completed
```

状态参数：

- `open`：未完成待办，默认值。
- `done`：已完成待办。
- `all`：全部待办。

输出会包含概览、待办列表和提醒列表。

### `/todo done` 和 `/todo undone`

修改待办完成状态：

```text
/todo done 1
/todo undone 1
```

`done` 只影响待办状态，不会删除待办或提醒。`undone` 会把已完成待办改回未完成。

### `/todo edit`

修改待办内容：

```text
/todo edit 1 写月报
```

如果待办有绑定提醒，提醒内容不会自动改名；需要改提醒内容时请使用 `/todo rtext`。

### `/todo rm`、`/todo del`、`/todo clear`

删除待办：

```text
/todo rm 1
/todo del 1
/todo clear
```

删除单个待办时，如果它有关联提醒，会同步删除关联提醒。`/todo clear` 会删除所有待办，并同步删除这些待办关联的提醒；独立提醒不会被 `/todo clear` 删除。

### `/todo remind`

创建独立提醒：

```text
/todo remind 喝水 10:00
/todo remind 喝水 10:00 daily
/todo remind 交房租 2026-07-01 09:00 monthly
/todo remind 上班打卡 09:00 daily workday
```

独立提醒不会绑定到待办。需要查看或删除请使用提醒命令。

### `/todo reminders` 和 `/todo rlist`

查看提醒列表：

```text
/todo reminders
/todo rlist
```

输出包含提醒序号、提醒内容、时间、重复规则，以及关联待办信息。

### `/todo rclear`

删除提醒。无参数时删除所有提醒：

```text
/todo rclear
```

带关键词时删除提醒内容包含关键词的提醒：

```text
/todo rclear 喝水
```

### `/todo rclear-todo` 和 `/todo rclear_todo`

按关联待办关键词删除提醒：

```text
/todo rclear-todo 写周报
/todo rclear_todo 写周报
```

该命令会先查找内容包含关键词的待办，再删除所有关联到这些待办的提醒。独立提醒不会受影响。

### `/todo redt`

通用提醒编辑命令，可同时修改内容、时间、重复规则和日期过滤：

```text
/todo redt 1 喝水 11:00 daily workday
```

参数都是位置参数。只想改时间或只想改内容时，更推荐使用 `/todo rtime` 或 `/todo rtext`，避免歧义。

### `/todo rtime`

修改提醒时间、重复规则或日期过滤：

```text
/todo rtime 1 15:30
/todo rtime 1 15:30 daily
/todo rtime 1 15:30 daily workday
```

修改时间或规则后，会重新注册调度任务。

### `/todo rtext`

修改提醒内容：

```text
/todo rtext 1 喝水休息一下
```

只改内容不会重新注册调度任务。

### `/todo rrm`

删除指定提醒：

```text
/todo rrm 1
/todo rrm all
```

`all`、`全部`、`所有`、`清空` 等选择器会删除所有提醒。

## 自然语言和 LLM 工具

插件注册了以下 LLM 工具，AstrBot 可以在自然语言对话中调用：

| 工具 | 作用 |
| --- | --- |
| `create_todo` | 创建待办，可选绑定提醒。 |
| `create_reminder` | 创建独立提醒。 |
| `list_todos_and_reminders` | 列出待办和提醒。 |
| `update_todo` | 修改待办内容或完成状态。 |
| `delete_todo` | 删除单个或全部待办。 |
| `delete_all_todos` | 删除全部待办。 |
| `update_reminder` | 修改提醒内容、时间、重复规则或日期过滤。 |
| `delete_reminder` | 删除单个或全部提醒。 |
| `delete_all_reminders` | 删除全部提醒。 |
| `delete_reminders_by_keyword` | 按提醒内容关键词删除提醒。 |
| `delete_reminders_by_todo_keyword` | 按关联待办关键词删除提醒。 |

出于安全考虑，LLM 工具执行批量删除时需要显式确认。删除所有待办、删除所有提醒、按关键词删除提醒、按待办关键词删除关联提醒时，工具会先返回将影响的数量；确认后再次调用并传 `confirm=yes` 才会真正删除。手动 `/todo clear`、`/todo rclear` 等命令保持原行为。

自然语言效果取决于 AstrBot 当前 LLM 的工具调用能力。建议表达中包含清晰的时间和动作，例如：

```text
明天 9 点提醒我开会
添加待办写周报，周五 18:00 提醒我
把第 1 个待办标记完成
删除包含喝水的提醒
```

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `max_items_per_user` | `50` | 每个私聊会话最大条目数。待办和提醒合计计算；`0` 表示不限制；负数会按默认值处理。 |
| `reply_in_group` | `false` | 群聊中使用 `/todo` 时是否提示私聊使用。 |
| `enable_llm_reminder` | `true` | 到点提醒时是否优先使用当前 LLM 生成提醒文案。 |
| `reminder_prompt` | 内置提示词 | LLM 生成提醒文案的提示词模板。支持 `{reminder_text}`、`{todo_text}`、`{current_time}`。 |
| `holiday_cache_days` | `30` | 法定节假日数据缓存天数。 |
| `timezone` | `Asia/Shanghai` | 解析提醒时间和注册调度任务使用的时区。Docker、UTC 服务器或海外服务器部署时建议显式设置。 |

## 数据文件

插件数据保存在 AstrBot 的插件数据目录下：

```text
todo_reminder_data.json
holiday_cache.json
```

说明：

- `todo_reminder_data.json` 保存待办和提醒。
- `holiday_cache.json` 保存节假日缓存和短期失败缓存。
- 如果数据文件损坏，插件会把原文件备份为 `todo_reminder_data.json.bak.<timestamp>`，并使用空数据继续启动。
- 加载时会过滤缺少必要字段或时间格式非法的提醒，避免坏数据阻塞插件启动。

## 常见问题

### 独立提醒到点时是什么格式？

独立提醒没有关联待办时，固定回退文案会使用单独格式：

```text
提醒：测试提醒
规则：一次性
```

绑定待办的提醒会保留关联信息：

```text
[提醒] 测试提醒
关联待办：测试提醒
```

### 为什么提示“重复类型错误”？

通常是因为把多条命令粘在一条消息里发送，导致下一条 `/todo` 被当成 `repeat` 参数。请一次只发送一条命令，例如：

```text
/todo remind 喝水 15:22
```

然后再单独发送：

```text
/todo reminders
```

### 为什么提醒没有在指定日期触发？

如果使用了 `workday` 或 `holiday`，插件会根据节假日判断过滤日期。一次性提醒会顺延到满足条件的日期；重复提醒会在触发时检查当天是否满足条件，不满足则跳过。

### 为什么 Docker 或服务器时间不对？

请检查 AstrBot 容器/服务器时区，并在插件配置里显式设置：

```text
timezone = Asia/Shanghai
```

## 兼容性

- Python 3.10 及以上。
- 依赖 `apscheduler>=3.10,<4`、`aiohttp>=3.9,<4`、`tzdata>=2024.1`。
- 仅支持私聊场景；不同消息平台的私聊判断由 AstrBot 统一提供。
- 节假日判断依赖外网接口 `timor.tech`；网络不可用时自动按周末兜底，不影响插件其他功能。

## 开发验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile main.py src/*.py tests/*.py
```

## 反馈与贡献

- 问题反馈与功能建议：[提交 Issue](https://github.com/interpy/astrbot_plugin_todo_reminder/issues)
- 欢迎提交 Pull Request；提交前请运行[开发验证](#开发验证)确保测试通过。

## 许可证

本项目以 [MIT](LICENSE) 许可证开源。
