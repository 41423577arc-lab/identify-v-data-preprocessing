# 第五人格微博数据预处理

本仓库公开微博文本清洗、相关性筛选、别名归一化、分词及微博—评论关系校验代码。

## 仓库内容

- `data_preprocessing/`：预处理脚本、通用辅助程序和不含真实语料的关系校验/清洗测试。
- `data_preprocessing/rules.json`：可解释的主题相关性规则配置。
- `data_preprocessing/text_resources/`：程序所需的空白资源结构模板，不含原项目人工词表。
- `requirements.txt`：Python 运行依赖。

## 未公开内容

原工作目录约 3.5 GB，包含微博和评论正文、CSV/JSONL、SQLite 数据库、冻结输入快照、生成报告及逐条人工审核资料。这些数据和人工判断均未上传。含真实微博 ID 和人工恶意表达审阅结论的专项分析脚本也没有包含在公开版中。

资源模板为空，不代表原项目词典或人工标注。运行时请只使用你有权处理的数据，并在本地准备相应词表和审核资源；不要将原始语料、人工标注、数据库或凭据提交到公开仓库。

## 安装

```powershell
python -m pip install -r requirements.txt
```

## 主要入口

- `data_preprocessing/prepare_text.py`：正文正则清洗、别名归一化和 jieba 分词。
- `data_preprocessing/preprocess.py`：依据 `rules.json` 对微博做相关性分层和逐行审计。
- `data_preprocessing/run_reviewed_preprocessing.py`：从本地冻结快照生成微博、评论/回复处理结果及 SQLite。
- `data_preprocessing/finalize_reviewed_outputs.py`：补充质量标记并建立分析视图。
- `data_preprocessing/topic_relevance.py`：按微博原始 `来源超话` 字段精确识别指定的无关超话。
- `data_preprocessing/filter_date_and_audit_collection.py`：按日期和来源超话筛选微博、同步处理关联评论并检查输出关系。
- `data_preprocessing/prepare_linked_dataset.py`：构建微博与评论/回复关联数据。

## 本地数据运行

生成数据应保存在 `data_preprocessing/outputs/`，该路径已加入 `.gitignore`。完整预处理需要本地输入快照和审核资源包，快照应包含 `sources.json`、微博/评论源文件及 `reviewed.zip`。使用新的输出目录进行离线重放：

```powershell
python -B data_preprocessing/run_reviewed_preprocessing.py --snapshot-dir <本地输入快照目录> --output-dir <新的全量输出目录>
python -B data_preprocessing/finalize_reviewed_outputs.py --output-dir <同一全量输出目录>
python -B data_preprocessing/filter_date_and_audit_collection.py --source-dir <全量输出目录> --output-dir <新的筛选目录>
```

来源超话筛选只对规范化后与完整超话名精确相同的记录生效：`ALCu表白墙`、`娱乐`、`sky光遇黑市`、`教育`、`荣耀茶话会`、`蛋仔派对交易`、`超自然行动组`。被筛微博及关联评论的 ID 和排除原因写入本地输出审计文件；完整预处理来源保留原始记录和关系。

`prepare_text.py` 和 `preprocess.py` 接受输入/输出参数；运行 `--help` 查看选项。连接 MySQL 或微博采集工作流的辅助脚本需要单独的采集项目和本地凭据；本仓库不含这些凭据。

清洗正文会移除回复路由和 @昵称；两个文本预处理入口使用输入记录中的作者昵称做最长匹配。无法确认昵称边界的片段写入 `removed_platform_fragments`，并标记 `mention_boundary_review_required`，便于检查是否连带移除了正文。邮箱保留。转发原文匹配使用移除昵称前的 `matching_body`，避免因不同提及对象被删除而错误合并原文。昵称仅用于辅助清洗，不能单独证明对应的某条微博。超话仅删除明确的开头或末尾平台标签串；专栏边界不明确时保留正文并标待复核。
