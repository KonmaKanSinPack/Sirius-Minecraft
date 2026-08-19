# M3-B 工作报告

- 任务：最小大脑循环（M3 会师核心）——工具注册表 + AgentLoop + CLI：玩家在游戏聊天打字 → bot 感知 → VLM 决策 → 工具执行 → 游戏内回话 的自主闭环
- 日期：2026-08-19
- 状态：完成（mock 双人全流程剧本 PASS；真机验收 = M3-C，操作指引见下）
- 验收：pytest 全绿 **263/263**（基线 244 + 新增 19，零破坏）；全部测试离线（fake VLM 剧本回放 + mock server 真实回环 127.0.0.1），无真实 key/真实联网进测试

## 交付物

| 文件 | 说明 |
|---|---|
| `sirius-brain/sirius_brain/agent/tools.py` | 工具注册表：`schema/tools/*.json` 读参数 schema → OpenAI function-calling 工具表；M3 白名单 11 工具（9 bridge + command/finish）；`ToolRegistry.register(ToolSpec)` 扩展口；handler 统一 `async (client, args) -> ToolOutcome`，screenshot 特殊（文本 `[图像已附]` + 图像 bytes 放 `ToolOutcome.image`） |
| `sirius-brain/sirius_brain/agent/loop.py` | `AgentLoop`（chat 入口/自回显/急停/任务循环/播报/上下文管理/结构化日志）+ `SelfEchoFilter` + `match_self_uuid`（纯函数）+ `LoopClient`（command 拦截包装）+ `TaskRun`/`ToolExec` 记录 |
| `sirius-brain/sirius_brain/agent/__main__.py` | CLI：`python -m sirius_brain.agent --local-md local.md [--url/--token/--max-steps/-v]` → 装配+就绪打印+常驻，Ctrl+C 优雅退出 |
| `sirius-brain/sirius_brain/agent/__init__.py` | 导出扩充（AgentLoop/ToolRegistry/TaskRun/SelfEchoFilter/…） |
| `sirius-brain/sirius_brain/agent/config.py` | **最小扩展（唯一既有文件改动，见偏离说明 1）**：`LoopConfig` 增 `max_total_tokens: int = 200_000`（token 预算，简报预留字段） |
| `sirius-brain/tests/test_agent_loop.py` | 19 项测试（见"验证方式"） |
| `sirius-brain/tests/fixtures/two_player_scene.json` | 双人 mock 剧本（回归资产）：bot Sirius_Bot + 玩家 Alex 的工具响应编排 |
| `sirius-brain/README.md` | 包结构树更新 + "Agent 循环（M3-B）"一节 |

## 关键决策与理由

1. **工具表从冻结 schema 产物读、描述不照抄**：参数 JSON Schema 读 `schema/tools/*.json`（与 Java 侧校验同源），保证 VLM 看到的参数契约 = bridge 校验的契约；但 schema 的 `description` 是"getStats()。spec §8.2。"这类内部记号，对模型无用——换成面向模型的操作指引（`TOOL_HINTS`，含坐标基准坑、键码等 M2 踩过的坑都写进提示）。客户端侧再用同一 `TOOL_PARAMS` pydantic 模型预校验：白名单外/参数错的调用本地拒绝（回错误文本给模型自救），不浪费 bridge 往返。
2. **白名单语义在注册表不在 schema**：`look`/`events.subscribe` 在 schema 产物里但不在 M3 白名单 → `UnknownToolError`（有测试钉死）。M3 攻击面收敛：工具表里根本没有攻击类入口。
3. **自回显双重过滤的精确语义**：sender == 自身 uuid → 必是回显；sender 是已知他人 → 必不是（玩家复读同文本也是新指令，不误杀）；sender 缺省/NIL → 抑制窗（command() 发送时登记 (文本, 时间戳)，默认 5s，时钟可注入）。自身 uuid 识别：getStats 无 uuid，用 **getStats.position ↔ world.query(entities) 最近实体（容差 2 格）** 匹配——两次 RPC 间自身小范围移动无碍；识别失败不阻塞启动（抑制窗兜底，就绪信息如实注明）。
4. **急停不打断在途 VLM 调用**：检查点设在 (a) 每次 VLM 调用前 (b) 每个 tool_call 执行前。在途的 HTTP 调用允许完成（其观测不浪费），下一个检查点断出。急停回话"好的，停下了"由 chat handler **立即**后台发出（不等任务退出）；任务侧静默终止不重复播报。急停同时丢弃"停止序号之前已排队"的任务（`_stop_seq` 单调计数），停止之后的新指令正常执行。
5. **command 全局串行锁（LoopClient）**：急停回话 / finish 播报 / command 工具都走 `LoopClient.command`——先登记抑制窗再发送，且经循环级 `asyncio.Lock` 串行，防止两条 T→text→ENTER 序列交错把话发串。所有播报失败只记日志不杀循环。
6. **finish 与纯文本回复双终点**：`finish(result)` 是显式终点；模型直接给纯文本（不调工具）也视作结束（OpenAI finish_reason=stop 的惯例语义），result=content 同样走 command 播报。两者 `end_reason` 区分（finish/content），测试分别覆盖。
7. **上下文预算三件套**：消息历史按任务隔离（每任务新开）；单条工具结果 >4000 字符截断（头 55%/尾 45%，结果总长恒 ≤4000，截断记号带省略字符数）；截图图像仅保留最近 1 张——新截图到来时先 `_prune_old_images`（把历史 user 消息里的 image_url 段裁成纯文本）再追加新图消息。VLM 侧 token 预算按每步 `usage.total_tokens` 累计，超 `max_total_tokens` 即止（与 max_steps 同走"进度摘要"播报）。
8. **同步 VLM 经 `asyncio.to_thread` 进循环**（M3-A 交接配方照用）：零新依赖；急停检查点天然覆盖线程边界（to_thread 返回处即检查点 a）。
9. **测试的 wire 真值断言**：不只断言循环内部记录（`TaskRun.tool_calls`），用 `RecordingMock(MockBridgeServer)` 记录全部入站 request 帧，断言 finish 播报/急停回话真的以 `input.text` 帧上了线——证明走了 `client.command()` 全链路（T→text→ENTER），而非绕过。

## 实现要点 / API 笔记

### AgentLoop 公开面

```python
from sirius_brain.agent import AgentLoop, AgentConfig, QwenVLM
from sirius_brain.bridge import BridgeClient

loop = AgentLoop(client, vlm, config, persona="", *,
                 registry=None,          # 缺省 default_registry()（M3 白名单 11 个）
                 self_echo_window=5.0,   # 自回显抑制窗（秒）
                 command_settle=0.5,     # command() 收尾等待（测试传 0.02 提速）
                 log_level=None)         # logging 级别（None=不动）

loop.install()                   # 注册 chat handler（幂等；建议 connect 前）
await loop.identify_self()       # 手动自识别（run() 会自动做，幂等只试一次）
await loop.run()                 # 常驻：订阅 chat → 串行任务队列（cancel/stop 退出）
run = await loop.run_task("指令") # 单任务直驱（测试/调试，不经 chat）
await loop.shutdown()            # 回收后台任务、注销 handler（不关连接）

loop.last_run / loop.runs        # TaskRun 留档（end_reason/result/tool_calls/tokens/elapsed）
loop.self_uuid / loop.echo       # 识别结果 / SelfEchoFilter（可注入时钟单测）
loop.request_stop()              # 急停（chat handler 内部也调它）
```

`TaskRun.end_reason` 取值：`finish` / `content`（纯文本回复）/ `stop` / `max_steps` / `budget` / `error`。

### 急停时序（测试钉死的行为）

```
任务运行中（步 N 的 VLM 调用在途）
  ← chat "停下"（任意非自身玩家）
  → 立即：request_stop() + 后台 command("好的，停下了")（不等任务退出）
  → 在途 VLM 调用完成（观测不浪费）
  → 检查点 b（下一个 tool_call 执行前）断出：end_reason=stop，无播报
  → 早于本次停止序号的排队任务被丢弃；之后的新指令正常执行
```

### 系统提示（安全约束在提示层 + 工具表层双重收敛）

身份（persona 或默认"天狼星（Sirius）"）+ 工具使用说明（含槽位坐标 gui-scaled→窗口像素换算、GLFW 键码表）+ 安全约束（禁攻击/不丢重要物品/先观察再行动）+ 当前任务。初始观测（getStats+getGuiState 紧凑 JSON）附在首条 user 消息。

## 验证方式

- pytest **263/263**（基线 244 零破坏 + `tests/test_agent_loop.py` 19 项）：
  - 注册表 5：白名单与 OpenAI 形态（11 工具/参数 schema 形状）、参数读自冻结产物（world.query required / screenshot 枚举）、白名单外拒绝（look/fly/events.subscribe/input.scroll）、参数客户端校验（lookAt 缺参/command 缺 text）、扩展口（注册自定义工具进表并可执行）
  - 自回显/自识别 3：抑制窗（注入时钟：窗内同文本抑制/窗外放行/登记刷新）、uuid 与他人不误杀、match_self_uuid（命中/超出容差/不在游戏/空实体/结构异常）
  - chat 入口 1：系统行/空白/自身 uuid/窗内同文本无 sender 全不触发；正常指令入队；急停词置标志+后台回话；大小写不敏感英文急停
  - 单任务循环（真实 mock 回环）6：finish 播报走 command（wire 断言 input.text）、max_steps 用尽播报（steps=3+前缀）、token 预算用尽（2 步 2000>1500 即止）、纯文本回复终点、VLMError 终止+播报、screenshot 图像附消息+旧图裁剪（末轮恰 1 个 image_url 段+data-url 前缀）+截断边界（≤4000/头尾保留）
  - 双人全流程 1：Alex 指令"丢一块石头给我"→ getStats→lookAt→command 话术→finish 播报（工具序列/参数/wire 全断言）；自回显三连（自身 uuid/窗内同文本/系统行）不触发新任务；真玩家新指令照常进任务
  - 急停 1：任务冻结在第 3 次 VLM 调用在途（threading.Event 门）→"停下"→放行→在途完成、检查点断出（steps=3、无 finish、回话上 wire、之后无新 VLM 调用）
  - CLI 1：参数解析+配置装载（--url/--token/--max-steps 覆盖 env 块；无覆盖时 env 块 bridge 配置生效）
- CLI 对 mock 真跑冒烟：`python -m sirius_brain.mock --port 8799` + `python -m sirius_brain.agent --local-md <临时 local.md>` → 就绪信息（bridge/hello/uuid/工具表/预算/模型）打印正确、常驻等待（默认 mock 无 world.query 剧本时 uuid 如实报"未知，抑制窗兜底"）；临时文件跑完即删
- 中文文件严格 UTF-8 解码自检通过（本机 GBK 代码页纪律）；真实 key 只存在于 gitignored 的 local.md

## 给 M3-C 真机验收的操作指引

前置：真 Mod 已部署（M2 生命周期）、`local.md` 的 env 块有真 key、游戏内开好世界（建议 LAN 或单人）。

```sh
cd sirius-brain
.venv\Scripts\python.exe -m sirius_brain.agent --local-md ..\local.md
# 真机 token 若配置：--token <sirius_bridge.toml 的 token>
```

检查清单：

1. **就绪信息**：自身 uuid 应识别成功（真 Mod 的 world.query 会带回自己的玩家实体；若报"未知"也能跑，抑制窗兜底——但建议记录该情况）；hello=acked（配了 token 时）
2. **最小指令回路**：游戏内另一账号（或 F3 看 chat）发"看看你的血量"→ bot 应调 getStats/screenshot 等工具 → finish 播报出现在聊天框；日志每步有"步 N/工具/耗时"行
3. **自回显**：bot 播报后自己不再对自己的话起任务（观察日志无"新任务入队"跟着自己的播报文本）
4. **急停**：任务执行中发"停下"→ 聊天框立即出现"好的，停下了"，任务在下一个检查点终止（日志"原因=stop"）
5. **观测格式**：getGuiState/world.query 结果回填正常（过长截断记号出现与否都记录）；screenshot 后下一轮 VLM 请求带 data:image/jpeg base64（-v 日志可见请求体大小）
6. **预算**：`--max-steps 3` 跑一个长任务，确认 3 步后播报"这个任务我先到这：…"
7. **VLM 行为**：qwen3.7-plus 真实 tool-calling 质量（是否按白名单用工具/会不会输出幻觉工具名→本地拒绝文本→自我纠正）；这是 M3-C 最主要的观察点
8. 已知风险点：真 Mod 的 chat 事件里**自己发送的消息也会回来**（sender=自身 uuid，依赖自识别成功）；多人服务器上其他玩家聊天都会被当潜在指令（M3 白名单+安全约束兜底，M4 加指令过滤/白名单玩家）

## 偏离说明

1. **config.py 唯一改动**（约束允许的"最小处扩展"）：`LoopConfig` 新增 `max_total_tokens: int = 200_000` + 校验——简报明写"token 预算（LoopConfig 预留字段，默认宽松）"，M3-A 预留的类里没有此字段，纯新增默认值不影响任何既有构造/测试（244 基线全绿佐证）。其余 bridge/mock/protocol/vlm.py 零改动。
2. **schema description 不直接进工具表**：`TOOL_HINTS` 覆盖（理由见决策 1）；schema 文件仍是参数契约的唯一来源。
3. **"纯文本回复也算结束"**：简报只定义 finish 终点；实践中 VLM 常直接文本回复（尤其简单寒暄），硬要求 finish 会造成无谓 nudge 循环烧步数。实现为双终点（finish 显式 / content 隐式），语义与 OpenAI finish_reason=stop 一致，测试覆盖。
4. **急停回话时机**：简报"立刻终止当前任务…并回话"——实现为 chat handler 收到即回话（后台任务，~0.7s 时序），任务本体在下一检查点断出；两者并行，回话不被任务退出阻塞。
5. **测试规模**：要求 ≥12，实交 19（自回显/急停/预算的分支展开较细）。

## 交接须知

- 给 M3-C：上节操作指引；重点观察 VLM 真实 tool-calling 行为与自识别成功率
- 给 M4（反射层）：CRITICAL 危险事件（M2-B）可直接挂 `client.add_event_handler("*", …)` 在 AgentLoop 之外并行处理；急停机制（`request_stop`）可复用为"反射层打断当前任务"
- 给 M5（分层）：`ToolRegistry.register(ToolSpec)` 即扩展口（已有测试示例）；高层技能=注册新 handler 组合低层工具；`LoopConfig.min_interval` 已就绪（默认 0）
- 已知限制：
  - 任务队列纯串行，任务执行中后续指令只排队不插队（急停除外）；无任务优先级
  - 自识别只在启动时做一次；换世界/重生后 uuid 不变（uuid 稳定）但抑制窗兜底仍在
  - 每任务初始观测固定 getStats+getGuiState 两次 RPC（不做缓存，换任务世界可能已变）
  - 无 VLM 流式/无每任务 token 精确上限裁剪历史（只截断单条工具结果；整历史超长靠 max_steps/token 预算兜底——M4+ 上下文管理的事）
  - command() 的 0.4+0.3s 时序常量为 M2-D 经验值，真机丢字时先调它
- 关联报告：M3-A（QwenVLM/AgentConfig 接口本任务直接消费）、M2-D（command() 与权限分级）、M2-B（chat 事件结构与危险事件）、M2-C（getGuiState 坐标基准坑）、M1-D（BridgeClient 事件分发）
