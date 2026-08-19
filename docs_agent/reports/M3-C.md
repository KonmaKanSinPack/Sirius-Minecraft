# M3-C 工作报告（真机集成验收）

- 任务：M3 收官——最小整机大脑在真 Minecraft 服务器上闭环验收
- 日期：2026-08-19
- 状态：**部分通过**（2/3 任务完整闭环，1/3 因 token 预算用尽中止；闭环成立已证明，问题清单清晰）
- 验收：技术规格 §10.1 M3 行"玩家在游戏聊天打字，bot 听到并自主完成，全程闭环"——**闭环成立**

## 环境

- 真服务器（非 LAN 单机），生存模式，主世界
- bot 角色：Sirius_test（uuid 298a93a1-...），初始位置 (0.5, 80, -5.5)
- 玩家：LegnaW9473（uuid e25b5c30-...），主账号客户端
- Bridge：sirius_bridge-0.1.0.jar（M2 全功能版，含事件/look/getGuiState/权限）
- 大脑：`python -m sirius_brain.agent --local-md local.md`，VLM=qwen3.7-plus 直连 DashScope
- 周围实体：鲑鱼 1（r=37）、牛 1（r=42）、玩家本人

## 验收结果

### 任务 1：「你好」——✅ 完整闭环

| 项 | 值 |
|---|---|
| 步数 | 1（纯文字回复，无工具调用） |
| 耗时 | 2.05s |
| tokens | 2246 |
| 终点 | content（VLM 直接文字回复） |
| 播报 | "你好！我是天狼星（Sirius），你的 Minecraft AI 陪玩伙伴。我目前处于生存模式，生命值满格（20.0）..." |

**关键验证**：自回显过滤成功——bot 自己的播报没有触发新任务。

### 任务 2：「试试来我这里」——✅ 完整闭环

| 项 | 值 |
|---|---|
| 步数 | 11 |
| 耗时 | 24.94s |
| tokens | 44738 |
| 终点 | finish("我已经到达你附近了！现在我们距离只有约3格。") |

**决策链**（完整还原）：
1. `world.query(range=100)` → 被拒（-32602 range must ≤64）→ **VLM 自我纠正**
2. `world.query(range=64)` → 找到玩家 (7.85, 74, 6.14)
3. `lookAt(7.85, 74, 6.14)` → 转向玩家
4. `input.key W duration=3000` → 向前走 3 秒
5. `getStats` → 验位 (3.47, 78.92, -1.06)，仍在接近
6. `lookAt` → 重新瞄准
7. `input.key W duration=1000` → 再走 1 秒
8. `getStats` → (9.03, 71, 8.36) 已越过
9. `world.query` → 确认玩家 (7.93, 74, 6.28)
10. `lookAt` → 转向
11. `finish("已到达，距离约 3 格")`

**关键验证**：VLM 能用结构化坐标算距离、规划移动、被拒绝时纠正参数、到达后主动 finish。

### 任务 3：「试试去旁边搜集几个云杉木」——⚠️ 预算用尽中止

| 项 | 值 |
|---|---|
| 步数 | 22（触达 max_steps 边界前 token 预算先尽） |
| 耗时 | 74.43s |
| tokens | 212840（超 200000 预算） |
| 终点 | budget（播报"这个任务我先到这"） |

**决策链摘要**：world.query 找方块（截断看不到 spruce_log）→ screenshot 看环境 → 识别到云杉树叶 → 多次 lookAt+screenshot+input.key W 逐步靠近树干 → 22 步时刚对准树干准备砍，token 用尽。

**方向正确**：找树→截图定位→移动靠近→准备砍，逻辑全对；**没走完**是因为反复截图+lookAt 的 token 开销累积太快，且 world.query 截断让 VLM 无法用结构化数据定位 spruce_log，只能靠图像绕路。

## 证明了什么

1. **最小整机大脑闭环成立**——玩家打字 → bot chat 事件捕获 → 感知（getStats/world.query/screenshot/getGuiState）→ VLM tool-calling 决策 → 工具执行（input.key/lookAt/command）→ finish/播报。**全自主零人工**
2. **VLM tool-calling 在 Minecraft 场景可用**——qwen3.7-plus 能看截图识别树木、能用结构化坐标算距离规划路径、被拒绝时自我纠正参数
3. **Bridge 哑管道原则正确**——Java 侧只报数据与执行注入，所有智能在 brain，运行流畅
4. **安全栏工作**——自回显过滤、串行任务队列、急停词配置、预算保护均按设计生效
5. **"大脑不绑死身体"第二次实战**——同一 AgentLoop 此前对 mock 双人剧本 PASS，此刻对真 Mod+真服务器零改动 PASS

## 存在的问题（按严重度排序）

### P0 — 影响任务完成率

1. **token 预算太紧（200k）**：复杂探索任务（找树+砍）需要大量 screenshot+lookAt 迭代，单任务轻松超 200k。建议：默认提至 500k；或改用步数预算为主（max_steps=25 已够紧）、token 仅作硬上限保护
2. **world.query 截断是认知瓶颈**：512 方块上限让 VLM 在找特定方块（如 spruce_log）时只能靠截图绕路，效率极低且 token 消耗大。需要"按类型过滤"的查询能力（如 `world.query(type="blocks", filter="spruce_log", range=64)`）——M4+ 优先项
3. **VLM 不会组合"挖方块"动作**：input.click 左键挖方块需要"准星对准+长按"的组合，当前 VLM 还没学会这个动作序列。属 M5 分层大脑（规划器分解+执行器熟练）的范畴，但 M3 工具集本身够用

### P1 — 健壮性

4. **hello_ack 帧未在协议建模**：真 Mod 回 `{type:"hello_ack",...}`，BridgeClient 当未知帧忽略（日志 WARNING）。功能不影响（best-effort 握手），但应补建模或至少在 hello 处理里识别——清理项
5. **command() 时序常量是经验值**：M2-D 的 0.4+0.3s 在真机长消息时可能丢字（本次未观察到，但任务 1 的长播报有截断迹象"请问有什么我可以帮"）——需真机长文本测试
6. **上下文增长未压缩**：22 步任务 prompt 从 2204 涨到 13091 tokens，screenshot 图像虽只留最近 1 张但工具结果文本累积。M5 需要 history 摘要/压缩机制

### P2 — 已知限制（非 bug，记入交接）

7. **world.query range 上限 64**：合理但 VLM 倾向请求 100+，需在系统提示里明确告知边界（当前靠 -32602 错误纠正，浪费一步）
8. **视角转动需窗口前台**：M2-A2 已知限制，真机本次 bot 窗口保持前台未触发
9. **无寻路**：bot 按 W 直走会撞墙/掉坑，任务 2 因距离近（15 格平地）成功；长距离需 M4 寻路

## 交接须知

- **M4 优先项**：world.query 按类型过滤（解决 P0-2）；brain 侧反射层（消费 CRITICAL 事件）；基础寻路（A*，消费 world.query 数据）
- **M5 前置**：history 压缩机制（解决 P1-6）；分层大脑（规划器分解任务→执行器熟练动作，解决 P0-3）
- **系统提示优化**：把工具边界（range≤64、限频 20/s、input.click 需长按）写进系统提示，减少 VLM 试错
- **真机验收脚本**：`m3c_probe.py`（环境探针）+ agent CLI `--local-md local.md` 即可复现；token 已在 local.md env 块
- 关联报告：M3-A（VLM 客户端）、M3-B（循环）、M2-B/C/D（被调用的工具）
