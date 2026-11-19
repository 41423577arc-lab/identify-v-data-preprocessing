# 第五人格微博中文分词工具对比

本目录用于在不改动正式预处理流程、词典和数据库的情况下，复现五种中文分词工具的本地对比。逐条原始样本和分词结果只写入本机工作簿；`results/*.xlsx` 与 `.cache/` 已忽略，不应提交。

## 目录

- `run_benchmark.py`：以只读方式从正式 SQLite 库抽样，独立子进程运行分词与资源计时。
- `build_workbook.mjs`：将本地私有中间结果整理为四张中文工作表，并在成功导出后清理缓存。
- `requirements.txt`：隔离测试环境依赖。
- `results/中文分词工具对比测试.xlsx`：本机结果工作簿，含逐条真实样本，仅本地保留。

## 运行方法

在项目根目录 `identify_V` 下执行 PowerShell。测试依赖装在仓库之外的虚拟环境；默认数据库和词典只读引用当前项目根目录。

```powershell
$envPath = Join-Path $env:LOCALAPPDATA 'identifyv-segmentation-benchmark-venv'
python -m venv $envPath
& (Join-Path $envPath 'Scripts/python.exe') -m pip install -r '.\identify-v-data-preprocessing\data_preprocessing\segmentation_benchmark\requirements.txt'
& (Join-Path $envPath 'Scripts/python.exe') '.\identify-v-data-preprocessing\data_preprocessing\segmentation_benchmark\run_benchmark.py'
```

随后在可访问当前 Codex 工作区 `@oai/artifact-tool` 的 Node.js 环境中运行 `build_workbook.mjs`。脚本会读取 `.cache/benchmark_private.json`，生成 `results/中文分词工具对比测试.xlsx`，然后删除中间原文缓存。若数据库或词典位置不同，可用 `--db`、`--dictionary`、`--glossary` 显式传入路径。

运行前建议确认以下本机资源文件仍位于项目外部测试目录约定的位置：

- 数据库：`database/identify_v.sqlite3`
- 领域词典：`data_preprocessing/text_resources/dwrg_dict.txt`
- 词条复核表：`data_preprocessing/text_resources/glossary_review.json`

## 方法

- 数据库以 SQLite `mode=ro` 打开。按固定 SHA-256 种子从微博正文、一级评论和楼中楼抽取 25、45、40 条，共 110 条；优先覆盖领域词典词、现有词表标记的攻击性别称、中英/数字混写、重复字符/标点/Emoji，再按固定散列顺序补齐。样本不含用户名和记录 ID。
- 性能表保留源数据库与领域词典 SHA-256 前 12 位，以便核对抽样所用的本地文件版本。
- 每个工具及词典配置在独立进程中运行。测首次初始化、预热后的缓存初始化、预热后分词 5 轮的中位耗时；同一批文本用于吞吐、峰值 RSS 与边界指标。时间不计 Python 进程启动时间。
- 磁盘体积按隔离环境内包文件和可解析运行依赖文件大小估算；THULAC、jiojio 等包内模型文件包含在工具包体积中。Windows RSS 使用 `psutil` 采样，不等同于全机空闲内存。
- 没有可靠人工分词金标准，不计算 Precision、Recall 或 F1。质量表只报告 343 个样本内领域词典候选词组、17 个词表标记攻击性表达的整词保留数和比例。它们是边界诊断指标，不是完整准确率，也不代表恶意言论分类。
- 自定义词典分别采用 `jieba.add_word`、jiojio `cws_user_dict`、THULAC `user_dict`，以及 cutword `custom_dict_path`（转换为词+词频两列）。工具词典加载格式不同，适配成本见性能表。

## 本次结果与建议

环境为 Windows 10 x64、Python 3.11.5、Intel64 Family 6 Model 140 Stepping 1、8 逻辑处理器。默认/领域词典两种配置均成功运行：jieba、jiojio、THULAC、cutword。pkuseg 两种配置未运行。

- 领域词典下，jieba 和 cutword 均完整保留 332/343（96.8%）个词典实体候选；jieba 默认为 213/343（62.1%），cutword 默认为 137/343（39.9%）。
- 17 个现有词表标记的攻击性表达中，jieba 默认和领域词典均为 17/17；cutword 默认和领域词典均为 17/17；jiojio 领域词典为 16/17；THULAC 两种配置均为 13/17。
- 110 条样本的中位分词耗时：jieba 领域词典约 0.024 秒（约 4,524 条/秒）；cutword 领域词典约 0.0065 秒（约 17,040 条/秒）。绝对耗时都很短，样本规模不足以说明生产规模吞吐优势。
- 峰值内存：jieba 领域词典约 230 MB；cutword 领域词典约 210 MB；jiojio 领域词典约 2,025 MB；THULAC 领域词典约 1,664 MB。磁盘包+必要依赖估算分别约 43 MB、113 MB、226 MB、510 MB（同顺序）。
- pkuseg 0.0.25 在 Python 3.11 / Windows x64 源码构建时需要 Microsoft Visual C++ 14.0+；补装 numpy 和 wheel 并关闭构建隔离后仍因编译器缺失失败。PyPI 上该版本的 Windows 预编译轮仅列到 CPython 3.8，故本次不报告虚构的 web 模型性能或质量。详见 [pkuseg PyPI](https://pypi.org/project/pkuseg/) 和上游 [web 模型配置](https://github.com/lancopku/pkuseg-python/blob/master/pkuseg/config.py)。
- cutword 标准包入口无条件导入 NER 模块并依赖未声明的 `torch`；测试通过直接加载包内公开 `Cutter` 分词器模块绕过该入口，不安装 PyTorch。它能加载项目领域词典，但需要生成词频两列文件。

**建议保留现有 jieba，不替换生产分词器。** cutword 在本次样本里更快，且自定义词典的实体与攻击表达完整率与 jieba 相同，但现有 jieba 管线已集成别称归一、词表词频、停用词和信号词适配；110 条语料的绝对时间差很小。切换到 cutword 要额外维护词典格式转换，并复核现有预处理链路行为，换来的吞吐提升不足以抵消依赖与迁移成本。jiojio 和 THULAC 在这台机器上的内存或磁盘开销更高；pkuseg 当前不能在此 Python/Windows 环境运行。

## 上游资料

- [jiojio PyPI](https://pypi.org/project/jiojio/)
- [THULAC Python 官方说明](https://github.com/thunlp/THULAC-Python)
- [cutword PyPI](https://pypi.org/project/cutword/)
