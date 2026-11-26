# AI4PCB × KiCadRoutingTools 长效改进手册（按改进点结构化版）

> **仓库**：`https://github.com/chinayuren2022-2025/KiCadRoutingTools`（分支 `ai4pcb/main`，基于上游 `drandyhaas/KiCadRoutingTools @ 61565f3`）  
> **同步注册表**：[`playbook_registry.yaml`](./playbook_registry.yaml)（1:1 机器可读 SSOT）  
> **编排规范**：按 PCB 自动化链路（`S0 → S7`）逐一排布**每个改进点（`MOD-01` ~ `MOD-10`）**；每个点强制写清 **① 原本 vs 现在对照、② 为什么改进、③ 测试了吗、④ 怎么测试、⑤ 测试效果是什么、⑥ 代码锚点与开关/回退**。

---

## 全链路改进点总览表

| 编号 | 链路阶段 | 改进点名称 | 测试了吗 | 核心测试效果一句话 |
| :--- | :---: | :--- | :---: | :--- |
| **[MOD-01](#mod-01-s0--路线-b-boardyaml-确定性约束编译--禁正则误报差分对)** | `S0 约束编译` | `board.yaml` 确定性约束编译 & 禁正则差分误报 | ✅ 已测试 | 漏网/偏细电源与机壳网 **`3 → 0`**；`/V_GP & /V_GN` 伪差分告警 **`1 → 0`**；自动反推 `dy=40 mm` 对称矢量 |
| **[MOD-02](#mod-02-s1--分组意图前置预校验--原生-kicad-courtyard-早检)** | `S1 契约与早检` | 分组/意图预校验 & 原生 Courtyard 早检 DRC | ✅ 已测试 | 分组错误 `<1 s` 拦截；原生 KiCad 封装冲突早检 **`<2 s/seed`**（Exp 03 18/18 零冲突） |
| **[MOD-03](#mod-03-s2--增量硬对称--自定义热隔离约束与耦合对称移动)** | `S2 物理布局` | 增量硬对称 + 自定义热隔离 + 耦合对称移动 | ✅ 已测试 | 41 对 X/Y 误差 **`22.89 → 0.00 mm`**、朝向错 **`25 → 0`**、热阻-IC **`+48.8%`**；全链路 `DRC 0/0` |
| **[MOD-04](#mod-04-s2--宏观信号流解析初值--有界局部退火)** | `S2 宏观初值` | 宏观信号流初值（`--macro-flow`）+ 局部半径退火 | ⚠️ 已测（有边界） | 6-Seed 代价标准差 **`161.2 → 61.9 (-61.6%)`**；**边界**：全局首摆勿配过紧 `R=18 mm` |
| **[MOD-05](#mod-05-s3--自适应几何多样性并行择优--两级全量确认)** | `S3 并行择优` | `--diverse auto` + `--confirm close`（修 Harness 单选阉割） | ✅ 已测试 | 3 板 × 2 预算 **6/6 全胜或持平**；`k=4` Top-6 命中 **`3.0×`**；3/3 板触发全量反转 |
| **[MOD-06](#mod-06-s4--同网-smd-焊盘过孔避让--002-mm-路由间距裕量)** | `S4 布线规则` | `--same-net-pad-clearance 0.25–0.30` + `0.22 mm` 裕量 | ✅ 已测试 | 非 EP `via_in_smd` **`50 → 0`**；`<0.30 mm` 擦边 **`73 → 1 (-98.6%)`**；消除 9.5µm 网格舍入违例 |
| **[MOD-07](#mod-07-s5--确定性模拟收尾原语与-pass-2-手术刀-drc-自愈)** | `S5 模拟收尾` | `analog_finish.py`（外层热焊盘保护 + Pass-2 自愈 + 2L 防悬空孔） | ✅ 已测试 | B01 DFM 缺陷 **`5/5 → 0/5`**（`23.7 s`）；4L/2L 双板独立复跑均 **`DRC 0/0`**；2L 悬空孔 **`23 → 0`** |
| **[MOD-08](#mod-08-s6--布线失败拥堵热力图局部回灌精修与防退化门禁)** | `S6 闭环回灌` | `route_feedback.py` + `feedback-refine`（`keep-if-better`） | ⚠️ 已测（有边界） | L2 拥堵种子阻断 **`4 → 3`**；**边界**：对全量已 `0 blocking` 的板会拉长铜线，须仅在 `blocking > 0` 触发 |
| **[MOD-09](#mod-09-s7--windows-pythonwexe--conhostexe-窗口站挂死修复)** | `S7 平台稳定` | Windows 内部子进程统一启用 `CREATE_NO_WINDOW` | ✅ 已测试 | 彻底消除 2.7 MB / 0% CPU 挂死（10 min 超时 → **`3 s`**）；Trial 5B 12-Seed 布局从 `699 s → 184 s` |
| **[MOD-10](#mod-10-s7--待实施-heights-3d-glb-0-规范栅格缓存与跨进程文件锁)** | `S7 性能优化` | `heights.py` 0° 规范栅格缓存 + 冷缓存文件锁 | ⬜ 未实施（已定位） | 尚未实施；实测定位 18-Seed 3D 高度导出耗时 `~13 min`，实施后预计压缩至 **`<45 s`（5×–10×）** |

---

## 逐点详述（MOD-01 ~ MOD-10）

---

### MOD-01 `[S0]` · 路线 B `board.yaml` 确定性约束编译 & 禁正则误报差分对

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream KRT / 旧流程） | 现在（本 Fork `ai4pcb/main`） |
| :--- | :--- | :--- |
| 电源/机壳线宽提取 | 由 Agent 人工读 `board.yaml` 后手拼 `--power-nets` 参数 | `py_tools/compile_board_constraints.py` 自动编译 `board.yaml` + `.kicad_pcb`，确定性生成 `routing_policy.json` 与 `judge_chain.txt` |
| 差分对识别 | `judge_seeds.py` 无条件调用 `list_nets.find_differential_pairs()` 按网络名正则 `_P/_N` 猜测并持续告警 | 仅当 `board.yaml` 显式声明 `differential_pairs` 时启用差分对；否则生成 `forbid_regex_diff_pairs: true` 并由 `judge_seeds.py --no-diff-pair-warn` 抑制伪告警 |
| 热源与对称信息 | 无统一编译产物，靠人工口头描述或手写脚本 | 自动提取 `thermal_budgets` + 2512 功率电阻作为 `hot_refs`、EP/精密 IC 作为 `sensitive_refs`，并从锁定配对连接器自动反推 `dx/dy` 平移矢量 |
| 悬空单焊盘网络 | 经常被 `--nets` 通配带入路由链 | 自动过滤 `unconnected-*` 与参考地平面网络 |

#### 2. 为什么改进
- 在 Astra v1 基线中，LLM 手工转录 B01 规格时漏掉了 `/CHASSIS`（退化成 `0.20 mm` 细线，严重削弱 ESD 泄放通道）与下游保护轨 `/VCC_DUT`/`/VEE_DUT`（退化为 `0.40 mm`）。
- 上游正则把 B01 上的单端偏置/供电网络 `/V_GP` 与 `/V_GN` 误报为差分对，干扰 Agent 决策。

#### 3. 测试了吗
**✅ 已测试**（单元测试 + Exp 02/03/05A 真实 B01 全流程实战验证）。

#### 4. 怎么测试
- **单元测试**：运行 `python tests/test_ai4pcb_constraints_and_harness.py`（构造含 X/Y 配对锁定连接器、`/+15V`/`/-15V`/`/CHASSIS`/`/VCC_DUT`/`/VEE_DUT`、`/V_GP`/`/V_GN` 与 2512 电阻的合成板，断言编译结果、`dy=40.0 mm`、线宽与 `forbid_regex_diff_pairs=True`）。
- **实板集成测试**：对真实 `attoCOLTRIMS_CEP/2_design/B01_Balanced_Receiver/board.yaml` + `fixed.kicad_pcb` 执行编译，将生成的 `judge_chain.txt` 直接送入 Exp 02 / Exp 03 / Trial 5A 路由并用 KiCad 10 `analog_finish.py --audit-only` 复核每条电源/机壳网实际最小线宽。

#### 5. 测试效果是什么
- 单元测试 `test_compile_board_constraints`：**PASS**。
- B01 实测编译结果：
  - 电源/机壳遗漏数：**`3 条漏/偏细`（Astra v1） → `0 条遗漏`**，自动输出 `[('/+15V', 0.75), ('/-15V', 0.75), ('/CHASSIS', 0.80), ('/VCC_DUT', 0.65), ('/VEE_DUT', 0.65)]`；
  - Trial 5A 独立复跑实测最小线宽：`/CHASSIS = 0.80 mm`、`/+15V = 0.75 mm`、`/-15V = 0.75 mm`、`/VCC_DUT = 0.65 mm`、`/VEE_DUT = 0.65 mm`；
  - 自动从 `(J1/J3, J2/J4, J5/J6)` 推导精确平移矢量 `dx = 0.0 mm, dy = 40.0 mm`，自动识别 8 颗发热电阻 `R12–R19`；
  - `/V_GP & /V_GN` 伪差分对告警：**`1 → 0`**。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_tools/compile_board_constraints.py`](../py_tools/compile_board_constraints.py)、[`py_placer/judge_seeds.py`](../py_placer/judge_seeds.py)（`--no-diff-pair-warn`）。
- 回退：不传 `--board-yaml` 即退回手工 `chain.txt`。

---

### MOD-02 `[S1]` · 分组/意图前置预校验 & 原生 KiCad Courtyard 早检

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream / 旧 Harness） | 现在（本 Fork `ai4pcb_harness.py`） |
| :--- | :--- | :--- |
| 分组与意图校验 | Agent 常直接调用 `place_simple.py`，拼错位号或漏件在跑完退火后才报错 | `ai4pcb_harness.py place` 强制先执行 `py_tools/check_groups.py` 与 `py_tools/check_floorplan.py`，错误在 `<1 s` 内拦截 |
| 原生封装碰撞检查 | 仅依赖 KRT 内部包围盒估算；真正的 KiCad `courtyard_overlap` 要等到布线和覆铜 DRC 结束后才暴露 | `place` 完成后立即对入围种子跑**不覆铜**的 `kicad-cli pcb drc --format json`，仅抽取 `courtyard_overlap` 与板边违例 |

#### 2. 为什么改进
- 避免将 5–10 分钟的并行布线算力浪费在存在原生封装重叠或漏件分组的坏布局上。
- 实战中（Trial 5B）还暴露并修复了旧脚本中校验器路径误指 `py_placer/` 而非 `py_tools/` 的 bug。

#### 3. 测试了吗
**✅ 已测试**（Exp 03 全 18 颗种子 + Exp 05 Trial 5A/5B 端到端验证）。

#### 4. 怎么测试
- 在 Exp 03（`B0`/`B1`/`B2` 共 18 颗新布局）与 Trial 5A（6 颗入围种子）上，通过 `ai4pcb_harness.py place` 自动触发早检并记录单种子耗时与 `courtyard_overlaps` 计数；修复路径后使用 `python -m py_compile py_tools/ai4pcb_harness.py` 与真实沙箱校验验证。

#### 5. 测试效果是什么
- 单种子早检耗时：**`<2 s/seed`**（原本等到全量布线后需 `5–10 min`）。
- Exp 03 全部 18 颗种子（`B0` 6 颗 + `B1` 6 颗 + `B2` 6 颗）与 Trial 5A 全部 6 颗入围种子均在进入布线前确认 **`0` 个 `courtyard_overlap`**。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_tools/ai4pcb_harness.py`](../py_tools/ai4pcb_harness.py)（`cmd_place`, `_early_courtyard_check`）。
- 回退：`--skip-validate` / `--no-check-courtyard`。

---

### MOD-03 `[S2]` · 增量硬对称 + 自定义热隔离约束与耦合对称移动

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream `place_simple.py`） | 现在（本 Fork `ai4pcb/main`） |
| :--- | :--- | :--- |
| 双通道几何对称 | 无对称惩罚项；模拟退火各自独立移动 X/Y 通道器件 | `simple.py` 新增 O(1) 增量 `symmetry` 原语（平移 + 旋转误差）；`simple_anneal.py` 新增**耦合对称移动算子**（75% 概率同步平移/旋转配对器件，25% 单件微调合法化） |
| 发热件热隔离 | 仅有固定阈值 `apart`（`APART_MM`），无法按热功耗定制最小安全距离 | 新增增量 `apart_min` 原语，支持任意自定义 `min_mm`（如热阻-精密 IC `≥ 15 mm`、热阻-热阻 `≥ 6 mm`） |
| 依赖手工坐标框 | 精密模拟板往往被迫手写 12 个 `blocks` 盒子甚至 Python 摆件脚本强行对齐 | **零手写坐标、零区域盒（`blocks: []`）**，通过 `--constraints-json` 直接由退火自动收敛 |

#### 2. 为什么改进
- 精密双通道模拟前端（如 B01 的 X/Y 平衡接收通道）对寄生电容匹配和热梯度对称高度敏感。上游纯线长退火会导致 X/Y 通道完全失配（平均误差 `22.89 mm`，超过一半器件朝向相反），且 2512 功率电阻容易贴近运放/精密电阻网络。

#### 3. 测试了吗
**✅ 已测试**（增量代价一致性单测 + Exp 03 三臂 6-Seed 配对实验 + 全量布线 & KiCad 10 DRC 端到端验证 + Trial 5A 子代理实战）。

#### 4. 怎么测试
- **单元测试**：`python tests/test_ai4pcb_physics_and_feedback.py`——随机扰动位姿，断言 `simple.py` 的增量 `local()` 与全量重算 `total()` 在 `symmetry` + `apart_min` 同时开启时误差 `< 1e-5`。
- **配对 A/B 实验（Exp 03）**：同一张无手写框（`blocks: []`）的 B01 `fixed.kicad_pcb`，固定 `--anchored 6 --judge 4 --diverse farthest`：
  - 对照组 **`B0`**：不传 `--constraints-json`；
  - 实验组 **`B1`**：传入含 41 对 X/Y `symmetry_pairs` 与 `R12–R19` 热隔离约束的 `physics_constraints.json`；
  - 对比全部 6 颗种子的 41 对平移均值/最大误差、旋转失配数、最短热阻-IC/热阻-热阻距离、L2 与全量布通率及终审 KiCad 10 DRC。

#### 5. 测试效果是什么
- 增量一致性单测：`local()` 与 `total()` 误差 **`< 1e-5`**（**PASS**）。
- **Exp 03 配对实测（6 种子均值，零手写坐标）**：

| 指标 | 对照组 `B0`（上游基线） | 实验组 `B1`（本改进） | 变化 |
| :--- | :---: | :---: | :---: |
| 41 对 X/Y 平移均值误差 | `22.89 mm` | **`0.00 mm`** | **`-100%`**（6 颗种子全部 `0.00 mm`） |
| 41 对最大单对误差 | `74.31 mm` | **`0.00 mm`** | **`-100%`** |
| 41 对朝向失配数 | `25.0 / 41` | **`0.0 / 41`** | **`-100%`** |
| 最短热阻-精密 IC 距离 | `16.24 mm` | **`24.17 mm`** | **`+48.8%`** |
| 最短热阻-热阻距离 | `5.62 mm` | **`11.12 mm`** | **`+97.9%`** |
| L2 快速试布零阻断种子数 | `0 / 4` | **`2 / 4`** | 显著改善可布通性 |
| 全链路终审（`B1 s2` + `0.22` + `analog_finish` v3） | `B0 s0`: DRC `1/1`, `983 mm` | **`B1 s2`: DRC `0/0`, `1190.32 mm` (-47.4% vs 人绘), EP `4/4/4/4`, 板边孔 `30`, `via_in_smd=0`** | **全自动零缺陷签核** |

- **Trial 5A（GPT 子代理独立实战）**：Agent 自动配对 38 对全部达成 **`0.000 mm` / `0` 旋转错**，热阻-IC 最小距离 **`61.46 mm`**（余下 3 对辅助/退耦电容未手动列入配对表 → 暴露下一阶段可在 `compile_board_constraints.py` 自动从网表图同构推导全 41 对的演进方向）。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_placer/placement/simple.py`](../py_placer/placement/simple.py)、[`py_placer/placement/simple_anneal.py`](../py_placer/placement/simple_anneal.py)、[`py_placer/place_simple.py`](../py_placer/place_simple.py)。
- 启用 / 回退：`--constraints-json <file>` 开启；省略即 100% 回退上游行为。

---

### MOD-04 `[S2]` · 宏观信号流解析初值 & 有界局部退火

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream `place_simple.py`） | 现在（本 Fork `ai4pcb/main`） |
| :--- | :--- | :--- |
| 退火初始位姿 | 纯随机打散或按组团聚起步，受随机种子影响大 | 新增 `--macro-flow`：依据锁定连接器位置做低抖动二次信号流初值求解并预先对齐对称对中点 |
| 局部微调能力 | 每次调用只能全板重新打散重摆，破坏已通区域 | 新增 `--keep-poses --local-radius <mm> --movable-only <refs>`：保持当前位姿，锁定其余器件，仅在半径 `R` 内微调指定子集 |

#### 2. 为什么改进
- 在开启强物理约束（MOD-03）后，纯随机初始化的 6 颗种子（`Arm B1`）在非凸地形中落点差异极大，代理代价跨度高达 `484.8`（标准差 `161.2`）。同时 Stage 6 的局部布线回灌需要“只动热点、不动干净区”的有界局部退火原语。

#### 3. 测试了吗
**⚠️ 已测试（有明确适用边界）**（单元测试 + Exp 03 `B1` vs `B2` 6-Seed 配对对比 + Exp 04 局部回灌测试）。

#### 4. 怎么测试
- **Exp 03 三臂对比**：在 `B1` 完全相同输入与约束下，新增实验臂 **`B2`**（启用 `--macro-flow --local-radius 18.0`），对比 6 颗种子的代理代价均值/标准差/跨度、对称与热隔离指标、以及 L2 与全量布线结果。

#### 5. 测试效果是什么
- **正向收益**：
  - 6 种子代理代价均值：**`1490.0 → 1429.1`（`-4.1%`）**；
  - 代理代价标准差：**`161.2 → 61.9`（`-61.6%`）**，最大-最小跨度 **`484.8 → 189.6`（`-60.9%`）**；
  - 对称误差保持 **`0.00 mm`**（`0/41` 旋转错），最短热阻-IC 保持 **`22.61 mm`**，全量布线仍可收敛至 **`0 blocking`**（`s4`: `68 vias, 1153.42 mm`）。
- **负向边界（诚实记录）**：
  - 将 `--local-radius 18.0` 用于**全局首次布局**过紧，限制了 SA 解开中段线束交叉，导致 `B2` 在 L2 快速试布阶段阻断数升至 `4–7`（而 `B1` 为 `0–6`、有 2 颗 `0 blocking`）。
  - **固化工程规范**：全局首次布局若用 `--macro-flow`，**不加紧 `--local-radius`**（或 `R ≥ 35 mm`）；紧半径 `R = 6–12 mm` 专用于局部精修。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_placer/placement/simple_anneal.py`](../py_placer/placement/simple_anneal.py)、[`py_placer/place_simple.py`](../py_placer/place_simple.py)。
- 启用 / 回退：`--macro-flow` / `--keep-poses --local-radius <mm> --movable-only <refs>`；省略即关闭。

---

### MOD-05 `[S3]` · 自适应几何多样性并行择优 & 两级全量确认

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream KRT / 旧 `pcb_agent_harness.py`） | 现在（本 Fork `diversity.py` + `ai4pcb_harness.py`） |
| :--- | :--- | :--- |
| Harness 是否多布局试布 | 旧 `pcb_agent_harness.py` 硬编码 `chosen_seed = judged_list[0]`，完全阉割并行试布 | `ai4pcb_harness.py judge` 完整恢复多布局并行 `judge_seeds.py --confirm close`，并在接近带仅 1 颗时自动补跑 `--full`，绝不交付 `--quick 2` 板 |
| Top-K 选种策略 | 上游 `place_simple.py` 仅按最低代理代价截断 `final.cost[:k]`，易选出一堆同盆地雷同布局 | 新增 `placement/diversity.py`：按宏引脚加权位姿距离（自动剔除锁定/静止件），严守缺陷分层与 0 号最低代价槽；**`--diverse auto`** 按池内 90 分位宏 IC 间距 `p90_ic_mm` 自适应切换（`<10.5 mm` 用 `medoid@1.10`，`≥10.5 mm` 用 `farthest@1.20`） |

#### 2. 为什么改进
- 布局代理代价只是未布线时的弱代理，代理第 1 名几乎不是实际布线最优解（例如 `B01_Free` 代价第 1 名 `seed 16` 在 L2 阻断 4 条网，真正的全量冠军 `seed 4` 在代理代价中排第 10 名）。
- 在初版测试中我们还发现：单一 `farthest`（MaxMin）在 12 个紧框锁死的 `B01_Zoned`（`p90=8.41 mm`）上会选到小框边缘的高代价离群点，因此升级为 `auto` 自适应策略兼顾自由板与紧分区板。

#### 3. 测试了吗
**✅ 已测试**（4 项单元测试 + 上游 3 组 `judge_seeds` 回归测试 + Exp 01 三板全 18-Seed Oracle 真值配对实验 + Exp 05 双板实战）。

#### 4. 怎么测试
- **单元测试**：`python tests/test_ai4pcb_geometric_diversity.py`（验证同盆地防塌缩、0 号槽保底、缺陷层级隔离、锁定件自动剔除、`auto` 宽/紧分布自动切换）+ 上游 `test_1327_judge_seeds.py`、`test_1645_judge_clean_seeds.py`、`test_1383_judge_confirm.py`。
- **Exp 01 三板全量 Oracle 离线配对测试**：在 3 块特性迥异的板（`B01_Free` 4L 自由模拟板、`B01_Zoned` 4L 12-紧框板、`splitflap_driver` 2L 机电驱动板）上，先各自生成 18 颗合法布局并**对全部 18 颗种子跑完 L2 试布与 `--confirm close` 全量真值路由**，再在相同预算 `k=4` 与 `k=6` 下对比 `Baseline_Cost`、`Farthest@1.20`、`Medoid@1.10` 与 `Auto_Adaptive`。

#### 5. 测试效果是什么
- 全部 4 组单测 + 3 组上游回归测试：**100% PASS**。
- **Exp 01 三板 × 2 预算（6 组配对测试）实测结果**：

| 测试板（全 18 Seed 真值） | 池 p90 宏距 / `auto` 决策 | 预算 | Baseline Cost Top-K | **`--diverse auto`** | 结果 |
| :--- | :---: | :---: | :--- | :--- | :---: |
| **B01 Free** (4L) | `22.95 mm` → `farthest@1.20` | `k=4` | 最佳 `#7` (blk=3), Top-6 **0/4** | 最佳 **`#3` (blk=2)**, Top-6 **2/4** | **Auto 胜** |
| **B01 Free** (4L) | `22.95 mm` → `farthest@1.20` | `k=6` | 均阻 `3.67`, Top-6 **1/6**, **漏冠军 `s4`** | 均阻 **`2.83` (-22.9%)**, Top-6 **4/6**, **命中冠军 `s4` (52 vias)** | **Auto 完胜** |
| **B01 Zoned** (4L) | `8.41 mm` → `medoid@1.10` | `k=4` | 最佳 `#5` (blk=2), 均阻 `2.25`, Top-6 **1/4** | 最佳 **`#2` (blk=1)**, 均阻 **`1.75` (-22.2%)**, Top-6 **2/4** | **Auto 胜** |
| **B01 Zoned** (4L) | `8.41 mm` → `medoid@1.10` | `k=6` | 最佳 `#4` (`s15`, 57 vias), Top-6 **2/6** | 最佳 **`#2` (`s14`, 58 vias)**, Top-6 **2/6** | **持平（差 1 孔）** |
| **Splitflap** (2L) | `14.13 mm` → `farthest@1.20` | `k=4` | 最佳 `#4` (blk=2), Top-6 **1/4**, **漏冠军 `s3`** | 最佳 **`#3` (blk=1)**, Top-6 **2/4**, **命中冠军 `s3` (143 vias)** | **Auto 完胜** |
| **Splitflap** (2L) | `14.13 mm` → `farthest@1.20` | `k=6` | 最佳 `#3` (blk=1), 均阻 `2.83`, Top-6 **2/6** | 最佳 **`#2` (`s15`, blk=0!)**, 均阻 **`2.50` (-11.7%)**, Top-6 **3/6** | **Auto 完胜** |

- **汇总统计**：
  - `k=4` Top-6 Oracle 命中率：**`16.7% (2/12) → 50.0% (6/12)`（3.0×）**；
  - `k=6` Top-6 Oracle 命中率：**`27.8% (5/18) → 50.0% (9/18)`（1.8×）**；
  - **3/3 块板（100%）出现 `--confirm close` 全量排名反转**：`B01_Free` (`s14→s4`)、`B01_Zoned` (`s8→s15`)、`Splitflap` (`s9→s3`)。
- **Trial 5A/5B 实战**：Trial 5A 将 IC 均距从 `20.30 → 24.83 mm (+22.3%)`；Trial 5B（2L ThatMicPre）从 12 颗中直接命中唯一 `0 blocking` 冠军 `seed 6`。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_placer/placement/diversity.py`](../py_placer/placement/diversity.py)、[`py_placer/place_simple.py`](../py_placer/place_simple.py)、[`py_placer/judge_seeds.py`](../py_placer/judge_seeds.py)、[`py_tools/ai4pcb_harness.py`](../py_tools/ai4pcb_harness.py)。
- 启用 / 回退：`--diverse auto`（或 `farthest` / `medoid`）+ `--confirm close`；回退：`--diverse off`。

---

### MOD-06 `[S4]` · 同网 SMD 焊盘过孔避让 & `+0.02 mm` 路由间距裕量

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream `route.py` / Astra v1） | 现在（本 Fork 编译策略） |
| :--- | :--- | :--- |
| 同网 SMD 焊盘避让 | 默认 `SAME_NET_PAD_CLEARANCE = -1.0`（关闭），允许过孔直接打进同网 SMD 焊盘 | 编译链默认强制 `--same-net-pad-clearance 0.25–0.30`（B01 推荐 `0.30 mm`） |
| 路由最小间距裕量 | `--clearance 0.21` 仅比 KiCad `0.20 mm` fab 规则高 `0.01 mm` | 提升至 `default_clearance_mm = 0.22`（`+0.02 mm` 裕量），吸收 `0.1 mm` 网格斜线离散误差 |
| 电源/机壳线宽 | Astra v1 使用 `0.40 mm` 电源与 `0.20 mm` `/CHASSIS` | 统一提升至 `0.75–0.80 mm`（主电源/机壳）与 `0.65–0.75 mm`（DUT 保护轨） |

#### 2. 为什么改进
- 关闭同网焊盘避让导致 Astra v1 出现 `50` 处非 EP `via_in_smd` 与 `73` 处 `<0.30 mm` 擦边，SMT 回流焊极易锡膏流失虚焊。
- 在 Exp 03 首轮路由中发现，`--clearance 0.21` 在 `B0 s0` 与 `B2 s4` 上各触发了 1 条 `0.1905–0.1977 mm` 的亚 10µm 网格舍入 DRC 错，提升 `0.01 mm` 至 `0.22 mm` 即可根除。

#### 3. 测试了吗
**✅ 已测试**（Exp 02 固定布局四臂参数扫描 + Exp 03 `0.21` vs `0.22` 对比 + Exp 05 双板实战）。

#### 4. 怎么测试
- **Exp 02 固定布局控制变量扫描**：冻结同一张 B01 `thermal_placed.kicad_pcb`，只改变 `--same-net-pad-clearance`（`-1.0 / 0.15 / 0.25 / 0.30`）与电源/`/CHASSIS` 线宽，全部经过同一版 `analog_finish.py` 和 KiCad 10 DRC 统计铜长、过孔、非 EP `via_in_smd` 与 `<0.30 mm` 擦边数。
- **Exp 03 间距对比**：同一 `analog_finish` v3 流程下，对比 `0.21`（`B0 s0`, `B2 s4`）与 `0.22`（`B1 s2`）的 KiCad 10 `clearance` 错误数。

#### 5. 测试效果是什么
- **Exp 02 四臂实测对照**：

| 参数臂 | `same_net_pad_clearance` | 电源/`/CHASSIS` 宽 (mm) | DRC (错/断/警) | 铜长 (mm) | 总过孔 | 非 EP `via_in_smd` | `<0.30 mm` 擦边 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `A0`（Astra v1 基线） | `-1.0` | `0.40/0.40/0.20` | `1 / 0 / 4` | `1172.45` | `169` | **`50`** | **`73`** |
| `A1` | `0.15` | `0.80/0.70/0.80` | **`0 / 0 / 4`** | `1248.64` | `153` | **`0`** | `41` |
| `A2` | `0.25` | `0.80/0.75/0.80` | **`0 / 0 / 4`** | `1248.22` | `152` | **`0`** | `33` |
| **`A3`（推荐）** | **`0.30`** | **`0.80/0.75/0.80`** | **`0 / 0 / 4`** | **`1253.11`** | **`155` (-8.3%)** | **`0` (-100%)** | **`1` (-98.6%)** |

- **Exp 03 间距实测**：`0.21 mm` 组（`B0`/`B2`）各残余 `1` 条 `0.1905–0.1977 mm` 间距错；`0.22 mm` 组（`B1 s2`）**`0` 条间距错**。
- **Trial 5A/5B 实测**：双板非 EP `via_in_smd` 均为 **`0`**（Trial 5B `<0.30 mm` 擦边亦为 **`0`**）。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_tools/compile_board_constraints.py`](../py_tools/compile_board_constraints.py)（`default_same_net_pad_clearance_mm`, `default_clearance_mm`）。
- 启用 / 回退：通过 CLI `--same-net-pad-clearance` / `--clearance` 调节（高密度 0.4 mm BGA 可按需降至 `0.15`）。

---

### MOD-07 `[S5]` · 确定性模拟收尾原语与 Pass-2 手术刀 DRC 自愈

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Astra v1 临时 `pcbnew` 脚本） | 现在（本 Fork `py_tools/analog_finish.py`） |
| :--- | :--- | :--- |
| 外层热焊盘与 `starved_thermal` | 为消 2 处细间距 IC `starved_thermal` 告警，把全板外层覆铜改成 `ZONE_CONNECTION_THT_THERMAL`，毁掉全部 0603 热焊盘 | 强制外层保持 `ZONE_CONNECTION_THERMAL (1)` + 开启 `ISLAND_REMOVAL_MODE_ALWAYS` 去浮铜孤岛；EP 焊盘强制直连；Pass-2 手术刀修复：**`≥6` 脚细间距 IC 单引脚设 `SetLocalZoneConnection(FULL)`，2 脚被动件改为 `0.25 mm` 短引线+地孔并设 `ZONE_CONNECTION_NONE` 保持零立碑热隔离** |
| 孤立 SMD 地焊盘 | 仅按欧氏距离打地孔，被走线围困的孤立 SMD 地焊盘会残留 DRC 未连接 | Pass-2 读取 `drc_report['unconnected_items']`，对孤立 SMD 地焊盘自动补直线或 2 段 L 型安全短引线/地孔（最多 2 轮迭代） |
| 板边缝合地孔 | Astra v1 漏打（`0` 颗）；2 层单地铜板盲目打板边孔会产生 `via_dangling` 告警 | 仅当 `≥2` 层铜层含地覆铜时自动按 `15 mm` 间距打板边缝合孔（4L 打 30 颗；2L 单层地铜自动跳过，0 悬空孔告警） |
| KiCad 10 API 稳定性 | 反复踩 `PCB_VIA.GetWidth()` 缺图层断言卡死与方法名变更坑，耗去数十分钟 | 封装为确定性单命令工具（支持 `--audit-only` 零修改审计），规避全部 KiCad 10 SWIG 坑 |

#### 2. 为什么改进
- 收尾阶段是 LLM 最容易破坏 DFM 规范和消耗大量试错时间的重灾区（Astra v1 的 5/5 缺陷中有一半来自临时收尾脚本，总耗时 65 min）。收尾属于确定性几何与规则工程，应当完全代码化、门禁化。

#### 3. 测试了吗
**✅ 已测试**（Exp 02 A1–A3 + Exp 03 B0–B2 v1/v2/v3 迭代验证 + Exp 05 Trial 5A 4L 与 Trial 5B 2L 独立复核）。

#### 4. 怎么测试
- 使用 KiCad 10 内置 Python 对以下 4 类不同布局/层数板运行 `analog_finish.py` 并进行全新独立 `kicad-cli pcb drc --refill-zones` + `--audit-only` 复核：
  1. Exp 02 B01 `thermal_placed`（A1/A2/A3 三臂）；
  2. Exp 03 B01 无框新布局 `arm_B1_physics s2`（验证 Pass-2 孤立焊盘 + 被动件 `starved_thermal` + EP 直连自愈）；
  3. Exp 05 Trial 5A B01 v2（4L 独立子代理交付板）；
  4. Exp 05 Trial 5B ThatMicPre v1（2L 单层地铜板，验证自动跳过板边缝合孔消除 `via_dangling`）。

#### 5. 测试效果是什么
- **Exp 02 `A3`**：`23.7 s` 完成，`DRC = 0/0/4`，`bad_therm = 0`，`via_in_smd = 0`，`perim = 30`，EP `4/4/4/4`，最大退耦地距 `1.40 mm`（5/5 DFM 缺陷清零）。
- **Exp 03 `B1 s2`**：v1 原报 `1` 错（`starved_thermal`）+ `6` 未连接 → 升级 Pass-2 L 型短引线 + 浮铜清除 + EP 直连后，v3 降至 **`DRC = 0 errors / 0 unconnected`**（自动愈合 2 处孤立 SMD 地焊盘 + 1 处被动件热焊盘 + 2 处浮铜孤岛），铜长 `1190.32 mm`。
- **Exp 05 Trial 5A（4L B01 v2）**：收尾仅 **`5.4 s`**，独立复跑 **`DRC = 0/0`**、`0/5` DFM 缺陷、整机端到端耗时从 Astra v1 的 **`65 min → 16 min 28 s`（4.0× 提速）**。
- **Exp 05 Trial 5B（2L ThatMicPre v1）**：收尾仅 **`3.4 s`**，独立复跑 **`DRC = 0/0`**（超越原始人绘参考板的 `3` 个 DRC 错！）；合入单层地铜防悬空孔逻辑后，复测 **`via_dangling` 告警从 `23 → 0`**、总过孔从 `46 → 23`、DRC 保持 **`0 / 0`**。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_tools/analog_finish.py`](../py_tools/analog_finish.py)（含 `--audit-only`）、[`py_tools/ai4pcb_harness.py`](../py_tools/ai4pcb_harness.py)（`finish` / `audit`）。
- 启用 / 回退：`ai4pcb_harness.py finish`；跳过该步骤即可保留原始布线板。

---

### MOD-08 `[S6]` · 布线失败/拥堵热力图局部回灌精修与防退化门禁

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream / 旧 Harness） | 现在（本 Fork `route_feedback.py` + `ai4pcb_harness.py`） |
| :--- | :--- | :--- |
| 布线失败后的布局调整 | 无结构化空间反馈，Agent 靠猜或全板重摆，容易把原本通畅的区域改坏 | `route_feedback.py` 解析路由板与 `route.json`，联合提取失败网端点、高绕行比网（`len/MST_L1 ≥ 1.40`）、高过孔网与 `4 mm` 拥堵网格，输出受限 `hotspot_refs` 与走廊排斥对 `local_repulsion` |
| 采纳与回滚机制 | 人工比对，容易引入退化 | `ai4pcb_harness.py feedback-refine` 冻结非热点器件在局部半径内退火重布，严格按 `judge_key(after) < judge_key(before)` 决定保留，否则自动丢弃并支持 `undo` |

#### 2. 为什么改进
- 打通“布线拥堵/失败空间诊断 → 局部精准微调布局 → 重布验证”的闭环，避免推倒重来。

#### 3. 测试了吗
**⚠️ 已测试（发现关键适用边界与 1 处待增强点）**（单元测试 + Exp 04 B01 3 种子 + Exp 04b Splitflap 2L 种子配对测试）。

#### 4. 怎么测试
- **单元测试**：`python tests/test_ai4pcb_physics_and_feedback.py`（验证对真实布线板抽取热点与绕行网络）。
- **Exp 04（B01 3 颗 L2 阻断种子 `16, 12, 0`）**：对每颗种子用相同链条分别测回灌前后的 L2 快布与 FULL 全量布线指标（`blocking`, `joins_needed`, `vias`, `copper_mm`）。
- **Exp 04b（2L Splitflap `seed 2`，L2 `blocking=6`）**：缩小 `--corridor-gap 1.6`、`--max-hotspots 16`、`--local-radius 6.0` 测试 2 层拥堵板效果。

#### 5. 测试效果是什么
- **Exp 04（B01 3 种子配对实测）**：

| 种子 | 热点 / 走廊对 | L2 快布（前 → 后） | 全量布线（前 → 后） | 结论 |
| :--- | :---: | :--- | :--- | :--- |
| `seed 16` | `28 / 24` | `blk 4→3, vias 35→33, cu 1015.8→995.7 mm` | `blk 0→0, vias 60→59, cu 924.1→953.6 mm` | L2 有效疏通；全量已通时铜长 `+3.2%` |
| `seed 12` | `28 / 24` | `blk 3→4, vias 39→45, cu 1037.2→1059.0 mm` | `blk 0→0, vias 51→58, cu 933.4→972.1 mm` | 全量本就 0 阻断，外扩导致增孔增线 |
| `seed 0` | `28 / 24` | `blk 4→3, vias 43→62, cu 1026.3→1144.0 mm` | `blk 0→0, vias 61→70, cu 964.4→1031.8 mm` | L2 阻断减 1，但全量已通时外扩负收益 |

- **Exp 04b（Splitflap 2L `seed 2`）**：`BEFORE_L2` 与 `AFTER_L2` 均为 `blk=6, vias=160, cu=2651.0 mm`（无变化），排查发现 `route.json` 的 `failed_single` 在 2L L2 下为空，断线信息仅记录在 `quick_score.json` 的 `joins_needed` 中。
- **诚实结论**：
  1. **严禁对全量已 `0 blocking` 的板子无脑外扩**，必须只在全量 `blocking > 0` 时触发并强制开启 `keep-if-better`；
  2. 下一步待增强 `route_feedback.py` 兼容读取 `quick_score.json` / `check_connected.py` 断网端点，提升 2L 板触发灵敏度。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_placer/placement/route_feedback.py`](../py_placer/placement/route_feedback.py)、[`py_tools/ai4pcb_harness.py`](../py_tools/ai4pcb_harness.py)（`feedback-refine`, `undo`）。
- 启用 / 回退：显式调用 `feedback-refine`；未变优自动不覆盖，或随时 `undo`。

---

### MOD-09 `[S7]` · Windows `pythonw.exe` / `conhost.exe` 窗口站挂死修复

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream KRT on Windows） | 现在（本 Fork `ai4pcb/main`） |
| :--- | :--- | :--- |
| Windows 内部子进程创建 | `place_simple.py`、`judge_seeds.py`、`heights.py` 的 `subprocess.run`/`Popen` 未传 `creationflags` | 在 `os.name == 'nt'` 时统一传入 `creationflags=subprocess.CREATE_NO_WINDOW`（覆盖 `ai4pcb_harness.py`、`place_simple.py`、`judge_seeds.py`、`heights.py` 全部 4 处） |

#### 2. 为什么改进
- 基于 KiCad `python.exe` 建立的 venv 实际由 GUI 子系统 `pythonw.exe` 执行（无继承控制台）。在 Windows 高并发/锁屏后台运行时，每个子进程（`quick_score.py`、`kicad-cli`）都会新建一个 `conhost.exe`；当 `conhost.exe` 数量累积至 `~45` 个时，新子进程会彻底卡死在启动阶段（`2.7 MB RAM / 0.00 CPU`），直接拖满 `heights.py` 600 秒超时（实测导致 Trial 5A 布局白耗 `10 min`、`Exp 03`/`04b` 卡死）。

#### 3. 测试了吗
**✅ 已测试**（现场进程级复现对照 + Trial 5A 修复前 vs Trial 5B 修复后耗时对比 + 全套单元测试回归）。

#### 4. 怎么测试
- 复现对比：对比挂死期间 10 个 `pythonw.exe` 派生子进程（均含 `conhost.exe 0x4`，`2.7 MB / 0.00 CPU` 卡死超 6 分钟）与加 `CREATE_NO_WINDOW` / 继承控制台后的执行耗时；
- 端到端实测：在合入 4 处 `CREATE_NO_WINDOW` 修复后立即运行完整的 12-Seed 布局 + 6 路并行布线 + 收尾实战（Trial 5B）。

#### 5. 测试效果是什么
- 单次 `kicad-cli pcb drc`：**从永久挂死（触发 600 s 超时）降至 `3 s` 稳定完成**，零新增孤立 `conhost.exe`。
- 端到端 12-Seed 布局阶段：修复前 Trial 5A 耗时 `698.7 s`（被 600 s 超时卡住）；修复后 Trial 5B 仅耗时 **`183.6 s`**，全程 9 分 02 秒零卡顿完成。

#### 6. 代码锚点与开关 / 回退
- 代码：[`py_tools/ai4pcb_harness.py`](../py_tools/ai4pcb_harness.py)、[`py_placer/place_simple.py`](../py_placer/place_simple.py)、[`py_placer/judge_seeds.py`](../py_placer/judge_seeds.py)、[`py_placer/placement/heights.py`](../py_placer/placement/heights.py)。
- 启用 / 回退：Windows 自动生效，Linux/macOS 自动忽略。

---

### MOD-10 `[S7 · 待实施]` · `heights.py` 3D GLB 0° 规范栅格缓存与跨进程文件锁

#### 1. 原本 vs 现在（对照）
| 维度 | 原本（Upstream `heights.py` L1048–1125） | 计划改造（P0 Backlog） |
| :--- | :--- | :--- |
| 缓存键设计 | `_key()` 将含器件绝对旋转角的 `(pad ... (at x y rot))` 原文纳入 SHA1，导致每个旋转角度不同的随机种子 **0% 命中缓存** | 按 `(model_path, size, mtime, side)` 在 **0° 规范朝向**下缓存单封装 2D 高度/遮挡栅格，运行时用 NumPy 瞬时做 0/90/180/270° 旋转 |
| 并发缓存填充 | `~/.cache/krt_heights/` 无跨进程锁，6 路并行冷缓存时同时起 6 个 `kicad-cli pcb export glb`（每路 `~1.6 GB RAM / 45 s`） | 冷缓存导出加原子文件锁，避免 6 路重复导出与 `~10 GB` 内存峰值 |

#### 2. 为什么改进
- 在 Exp 01（`splitflap` 18 颗种子）和 Exp 03（`B1` 6 颗种子）中实测发现，实际 SA/布线只要 `50–77 s`，但每颗种子旋转不同导致重复触发 `kicad-cli pcb export glb`，单颗最长拖慢至 `393–412 s`（占去整个多 Seed 流程 70% 以上耗时）。

#### 3. 测试了吗
**⬜ 未实施（已完成源码根因定位与耗时量化，列为下一阶段 P0）**。

#### 4. 怎么测试（计划验收方案）
1. 新增 `tests/test_ai4pcb_heights_cache.py`：在 0/90/180/270° 旋转下对比新 NumPy 旋转栅格与原 `kicad-cli` 导出栅格，断言误差 `< 0.01 mm`；
2. 6 路并发冷缓存压力测试：验证 `kicad-cli pcb export glb` 仅被调用 1 次且无锁冲突；
3. 跑通上游 `tests/test_1645_judge_clean_seeds.py` 与 KRT 22 板高度回归测试，记录 18-Seed 总耗时对比。

#### 5. 测试效果是什么（基线测量 vs 预期目标）
- **当前实测基线**：18-Seed 3D 高度导出总计 `~13 min`（18 次导出，6 路并发内存峰值 `~10 GB`）。
- **实施后预期**：降至 **`< 45 s`**（1 次导出 + NumPy 旋转，预计提速 **5×–10×**）。

#### 6. 代码锚点与开关 / 回退
- 目标锚点：[`py_placer/placement/heights.py`](../py_placer/placement/heights.py)（`_key`, `_measure`, `_canonical_poses`）。
- 预留回退：计划通过 `KRT_HEIGHTS_LEGACY_CACHE=1` 环境变量回退原键逻辑。

---

## 附：标准化追加模板（新增改进点 `MOD-xx` 时直接复制）

```markdown
### MOD-<nn> `[S<stage>]` · <改进点名称>

#### 1. 原本 vs 现在（对照）
| 维度 | 原本 | 现在 |
| :--- | :--- | :--- |
| <对比项 1> | <旧做法> | <新做法> |

#### 2. 为什么改进
- <根因与具体失效案例/基线量化问题>

#### 3. 测试了吗
**✅ 已测试 / ⚠️ 已测试（有边界） / ⬜ 未实施**

#### 4. 怎么测试
- **单元测试**：`<精确命令>`
- **配对 A/B 实验**：<控制变量、测试板、对照组 vs 实验组、验收指标>

#### 5. 测试效果是什么
- <前后对照量化表格 + 正负结论与适用边界>

#### 6. 代码锚点与开关 / 回退
- 代码：`<相对路径>`（`<函数/CLI>`）
- 启用 / 回退：`<命令开关>` / `<如何关闭>`
```
