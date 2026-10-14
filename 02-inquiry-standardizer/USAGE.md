# 阀门询价中英对照整理

把客户发来的阀门询价资料，整理成**公司统一格式的中英对照询价表**（含技术审查备注）。

## 三步流程

```
① 读懂资料            ② 抽成统一字段            ③ 出表
Excel / Word / PDF  →  AI 抽成 12 个标准列  →  中英对照 Excel + 审查备注
（纯程序）              （DeepSeek）               （纯程序）
```

第 ② 步是核心——客户发来的格式千奇百怪，只有模型能把它们收敛成同一套字段。

## 目标表格式（严格复刻既定交付格式）

单 Sheet，12 个标准字段：

| No. | Valve Type | Size | Rating | End Connection | Port | Body Material | Trim (Ball/Stem/Seat) | Design Standard | Special Requirements | Qty | Unit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 序号 | 阀门类型 | 尺寸 | 压力等级 | 端部连接 | 通径型式 | 阀体材质 | 内件（球体/阀杆/阀座） | 设计标准 | 特殊要求 | 数量 | 单位 |

**每个单元格内是「英文在上、中文在下」两行**，例如：

```
RF (Raised Face)
凸面法兰
```

### 动态补充列：标出「你没提供什么」

除了固定的 12 列，DeepSeek 还会审阅这份询价单，判断**哪些必要参数没有交代清楚**，
把它们补成额外列（最多 4 列），有值的照常填，没值的那一格直接标成：

```
Not provided
未提供
```

例如这份单子里有的阀门写了结构长度、有的没写，就会自动多出两列：

| … | Face-to-Face (FF) 结构长度 | Working Fluid 工作介质 |
|---|---|---|
| 序号 2 | 229 mm | Water & hydrocarbon condensate 水与烃类凝析液 |
| 序号 6 | **Not provided 未提供** | Heat transfer oil 导热油 |

把这张表发回给客户，他一眼就知道自己漏了什么，少一轮来回扯皮。

判定标准是三条同时满足：报价必需 / 不在 12 个标准列内 / 这份资料确实缺失或只写了一半。
没有缺失就不加列，不会硬凑。

样式规格（已通过往返测试验证，与参照文件逐项一致）：

| 元素 | 规格 |
|---|---|
| 标题行 | `A1:L1` 合并，深蓝底 `1F4E79` / 白字 14 加粗，行高 22.5 |
| 表头行 | 中蓝底 `2E75B6` / 白字 10 加粗，居中换行，四边细框，行高 33 |
| 数据行 | 字 10，行高 48；A-F 与 K-L 居中，G-J 左对齐 |
| 合计行 | 浅蓝底 `DDEBF7`，`Total/合计` + 总数量 |
| 备注区 | `A:L` 合并，深蓝标题 + 每条备注一行，格式 `Note N: <英>\n备注N：<中>` |

## 目录结构

```
valve-inquiry-bilingual/
├─ src/
│  ├─ pipeline.py     主流程 / 命令行入口
│  ├─ readers.py      多格式读取：Excel / Word / PDF / 文本
│  ├─ extractor.py    ★ AI 结构化抽取（12 个标准字段 + 审查备注）
│  ├─ terms.py        术语库（自动积累，可人工增补）
│  ├─ exporter.py     ★ 导出询价表（严格复刻交付格式）
│  ├─ masker.py       ★ 本地脱敏（MTO 流程用：项目名/位号/图号换代号）
│  └─ mto.py          ★ MTO 中英对照（脱敏后按原结构全字段对照）
├─ glossary/          ★ 术语库（项目资产，随代码入库）
│  ├─ terms.json      自动生成 + AI 补译，程序维护
│  ├─ terms_extra.csv ★ 人工增补，优先级最高，改这里最方便
│  └─ terms.csv       给人看的对照表
├─ data/
│  └─ input/          客户发来的原始询价（本地数据，不入库）
├─ output/            生成的表 + 抽取结果（不入库）
├─ tools/
│  ├─ inspect_template.py  查看任意 xlsx 的完整结构（内容 + 格式）
│  ├─ roundtrip_test.py    往返测试：验证导出格式是否与参照一致
│  ├─ extract_terms.py     从历史成品提取术语库
│  ├─ make_sample.py       生成测试样本
│  └─ peek.py              命令行查看生成的 xlsx
├─ run.bat
└─ .env.example
```

## ⚠️ 绝对不能上传到代码仓库的文件

项目根目录已经放好了 `.gitignore`，下面这些会被自动挡住。**不要删掉里面的任何一条。**

| 文件 | 里面是什么 | 泄露后果 |
|---|---|---|
| `data/mask_map.json` | 脱敏对照表（真实信息 ↔ 代号） | 拿它一查就能还原，**脱敏全部作废** |
| `.env` | DeepSeek API Key | 别人拿你的 Key 调用，**账单算在你头上** |
| `data/input/` | 客户发来的原始资料 | 真实项目文件外泄 |
| `output/` | 生成的对照表、抽取结果 | 含业务数据 |

> **git 会记住每一个版本。** 上传后再删掉文件，历史记录里仍然看得到，
> 要彻底清除得重写整个历史。所以必须在**第一次上传之前**就挡住。

**推代码前自查一句**：确认 `.gitignore` 在、`.env` 不在暂存区，再推。

## 一、装依赖

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt     # Windows
```

## 二、配 API Key

复制 `.env.example` 为 `.env`，填入 DeepSeek 的 Key（申请：https://platform.deepseek.com）：

```
DEEPSEEK_API_KEY=sk-你的key
```

## 三、用起来

```bash
# 正常流程
run.bat "data\input\埃及项目询价.xlsx"

# 多份资料合并整理
run.bat "data\input\A.xlsx" "data\input\B.pdf" "data\input\C.docx" -o "output\691-2610-26_Inquiry_Bilingual.xlsx"

# 补充已知信息，帮助模型判断
run.bat "data\input\询价.xlsx" --hint "项目编号 691-2610-26，埃及项目"

# 离线重出表（不需要 Key，用来调格式）
python src/pipeline.py --from-json "output\xxx_extracted.json"
```

每次运行会产出两个文件：

- `output/<项目编号>_Inquiry_Bilingual_<时间>.xlsx` —— 交付用的表
- `output/<项目编号>_..._extracted.json` —— 模型的抽取结果，改格式时可直接重出表

## 处理工程 MTO 表（另一种输入形态）

客户发来的不一定是"询价单"，也可能是**工程 MTO（材料清单）**——字段多、带位号/P&ID/管线号。
这种情况走另一条流程，并且**先脱敏**：

```bash
# 第 1 步：本地脱敏（必须在发给 AI 之前做）
python src/masker.py "原始.xlsx" -o "data/input/mto_masked.xlsx" --map "data/mask_map.json"

# 第 2 步：中英对照（保留原表结构，列名和值都补中文）
python src/mto.py "data/input/mto_masked.xlsx" -o "output/MTO_Bilingual.xlsx" --title "[PROJECT NAME] 阀门 MTO"
```

### 脱敏替换了什么

| 内容 | 替换为 |
|---|---|
| 项目名 | `[PROJECT NAME]` |
| 文档编号 | `[DOC NO.]` |
| 阀门位号 / P&ID 图号 / 管线号 | `REF-0001`、`REF-0002`…（同一个原值永远对应同一个代号） |
| 自由文本（描述、备注）里的工程编号 | 就地替换成 `REF-NNNN` |

⚠️ **映射表 `data/mask_map.json` 只存本地，不要外发。** 需要还原时用它反查。

### 为什么这么快：按唯一值翻译

MTO 表看着行数多（实测 388 行 × 27 列），但**每一列里真正不同的取值很少**。
所以流程是「提取唯一值 → 批量翻译 → 本地回填」：

- 388 行数据，实际只有 **107 个不同的取值** + **22 个列名**需要翻译
- token 消耗极小
- 更重要的是：**同一个值在任何位置译法必然一致**，不会前半张表译"闸阀"、后半张表译"闸门阀"

### 列名会带着语境一起翻译

列名连同该列的样本值一起发给模型，避免同一个词在不同列译错。
实测遇到的例子：`UNIT` 出现两次，C 列的值是 `UNIT#1`（译「单元」），T 列的值是 `PCS`（译「单位」）。
两级合并表头也会合成通顺的列名：`Medium operating parameters` + `P kgf/cm2` → 介质操作压力。

### 输出前会审阅「这份清单缺了什么」

翻译完成后、导出之前，会把列结构再发一次给模型审阅：**从报价角度看，这份清单还缺哪些必要参数？**

判定标准三条同时满足：属于报价必需 / 现有列没覆盖 / 整份清单都没提供。
判定出的参数追加成表尾的新列，用**橙色表头**和原表列区分，每个产品行的该格填：

```
Not provided
未提供
```

实测这次判定缺少 5 项：**内件材质、设计标准、结构长度、法兰标准、检验与试验**。
逐行标出来之后，客户一眼能看到这份 MTO 还差什么，不用来回追问。

> ⚠️ **注意**：原表里本来就空着的单元格**保持原样**，没有填占位符——
> 工程 MTO 里的空值通常表示「该行不需要这项」，硬填会污染原表语义。
> 只有**新增的补充列**才逐行标「未提供」。

## 四、术语库会越用越准

| 文件 | 谁维护 | 说明 |
|---|---|---|
| `glossary/terms.json` | 程序自动 | 历史成品提取 + 每次运行自动积累 |
| `glossary/terms_extra.csv` | **你手动** | 优先级最高，发现译法不对就加一行 |

```csv
中文,英文
丁腈橡胶,Nitrile (NBR)
环形槽面,RTJ (Ring Type Joint)
```

**每次发现译得不对，加一行，以后不会再错。**

## 五、支持范围

| 输入 | 状态 |
|---|---|
| Excel（.xlsx） | ✅ |
| Word（.docx） | ✅ |
| PDF（文字型，含项目文件） | ✅ |
| 纯文本 / CSV | ✅ |
| PDF 图纸（CAD 矢量） | ⚠️ 能提文字，但尺寸标注/图框会混进来，需人工核对 |
| PDF 图纸（扫描件）/ 图片 / 铭牌照片 | ⛔ 待接入识图，当前会明确提示 |

## 六、验证格式是否正确

改了 `exporter.py` 之后，跑一遍往返测试就能知道有没有跑偏：

```bash
python tools/roundtrip_test.py "C:\Users\Administrator\WorkBuddy\2026-08-09-11-18-29\691-2610-26_Inquiry_Bilingual.xlsx"
```

它会读参照文件 → 重新导出 → 逐项比对 sheet 名 / 合并单元格 / 列宽 / 行高 / 内容 / 样式。
当前状态：**完全一致**。

## 七、下一步

1. **接识图** —— 铭牌照片和扫描图纸。DeepSeek 官方有视觉模型 `deepseek-v4-flash-vision-exp`，
   不用换供应商（注意：阿里云百炼渠道的 DeepSeek 只支持文本）
2. **PDF 图纸专项处理** —— 过滤尺寸标注和图框，只留技术要求与阀门参数表
3. **套网页版 + 登录** —— 让客户自己上传、下载（云服务器方案）
4. **Docker 化** —— 方便部署到云服务器
