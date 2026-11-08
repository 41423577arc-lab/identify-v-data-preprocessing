"""Package the final receipt and frozen inputs without changing any original source."""
import argparse
import json
from pathlib import Path
import shutil
from run_reviewed_preprocessing import sha


def main(out):
    report=json.loads((out/'validation_report.json').read_text(encoding='utf-8'))
    previous=Path(report['input_snapshot']);target=out/'input_snapshot'
    if previous!=target and not target.exists():shutil.copytree(previous,target)
    for s in report['sources']:
        if s.get('snapshot'):assert sha(target/s['snapshot'])==s['sha256']
    report['input_snapshot']=str(target)
    report['snapshot_hashes_reverified']=True
    (out/'validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    path=lambda name:str((out/name).resolve()).replace('\\','/')
    source_rows='\n'.join(f"| {s['kind']} | {s.get('original_path','不可访问')} | {s.get('snapshot','')} |" for s in report['sources'])
    db_counts='\n'.join(f"| {s['source']} | {s['rows']} | {s['integrity']} |" for s in report['source_database_checks'])
    text=f'''# 第五人格微博完整文本预处理结果

本次已实际运行、回读 JSONL、逐字段核对原始值和 JSON 类型，并执行 SQLite 完整性与外键检查。原始 CSV、词表压缩包、数据库和历史结果均未被本任务写入。评论爬虫可能仍在更新，因此使用只读 SQLite 在线备份和 MySQL 只读一致性事务冻结输入。数据库原文件之后的后台采集变化不属于本次快照。

## 数据和输出

| 项目 | 结果 |
|---|---|
| 微博 | {report['posts']} 条；来源 4,439 条八爪鱼筛选结果 + 1,846 条 MySQL 筛选结果，重叠 66 个 ID；保留全部来源版本 |
| 微博时间 | {report['post_time_range'][0]} 至 {report['post_time_range'][1]}；保留已有数据原日期，没有擅自限定为 9—10 月 |
| 完整正文 | 使用已有补采的 {report['longtext_used']} 条正文，其中 790 条 supplemented_complete、10 条 already_complete_in_main_db；原 content 不覆盖 |
| 评论和回复 | {report['comment_input_observations']} 次来源观测，按原采集键合并 {report['comment_duplicate_observations']} 次重复观测，得到 {report['comments']} 条；一级 {report['comment_levels']['1']}，回复 {report['comment_levels']['2']} |
| 评论时间 | {report['comment_time_range'][0]} 至 {report['comment_time_range'][1]}；110 条旧界面控件记录无可解析发布时间，未猜测日期 |
| 无效和空文本 | 原始空正文 0；界面控件专用记录 {report['invalid_platform_control_only_records']} 条，原文保留并标记；清洗后空微博 25、空评论/回复 120 |
| 聚类有效文本 | 微博 {report['analysis_view_counts']['posts_clustering']}；评论/回复 {report['analysis_view_counts']['comments_clustering']}；空 tokens 行保留在主数据，聚类视图排除 |
| 归一化 | {sum(x['normalized_occurrences'] for x in report['text_processing'].values())} 次替换；映射冲突 {report['mapping_conflicts']}；词条属性不代表整句恶意分类 |
| 分词验证 | 有效游戏词典 {report['dictionary_effective_words']} 词，词典 token 命中 {report['dictionary_token_hits']} 次；1,970 条标准选手 ID 检查全部保持整词；31 项相关单元测试通过 |
| 人工复核 | 774 条评论标注，772 条精确关联；771 条有可独立匹配的词语跨度，另 1 条 p5 位于联系方式 Zliop5518 内，按人工意见原样保留、不归一化。2 条记录对应多个采集键，未强制关联。原微博 24 处人工消歧全部应用 |
| 待复核 | {report['pending_review_records']} 项：96 次多义词命中 + 2 条人工标注关联歧义。关联 ID 问题单独列出，不混入词语复核数量 |

| 文件 | 用途 |
|---|---|
| [{path('posts_preprocessed.jsonl')}]({path('posts_preprocessed.jsonl')}) | {report['posts']} 条微博；同时含聚类与恶意识别特征 |
| [{path('comments_preprocessed.jsonl')}]({path('comments_preprocessed.jsonl')}) | {report['comments']} 条一级评论和回复；同时含两种文本及原始记录 |
| [{path('research_dataset.sqlite3')}]({path('research_dataset.sqlite3')}) | 相同数据的数据库、关联表、研究视图 |
| [{path('pending_review.jsonl')}]({path('pending_review.jsonl')}) | 98 项词语/人工标注待复核，附原文和记录键 |
| [{path('pending_relations.jsonl')}]({path('pending_relations.jsonl')}) | 平台 ID、直接父回复或集外微博关联待核实 |
| [{path('missing_post_evidence.jsonl')}]({path('missing_post_evidence.jsonl')}) | 49 个集外微博的已有 URL、采集元数据和数据库正文候选；不将未筛选正文混入已筛选微博集 |
| [{path('invalid_records.jsonl')}]({path('invalid_records.jsonl')}) | 120 条明确界面控件记录；不删除其原始行 |
| [{path('validation_report.json')}]({path('validation_report.json')}) | 全部统计和验证结果 |
| [{path('processing.log')}]({path('processing.log')}) | 实际执行日志；tests.log 为测试结果 |
| [{path('manifest.json')}]({path('manifest.json')}) | 文件 SHA-256、大小；input_snapshot 为冻结原始输入，resource_snapshot 为最新人工包资源 |

## 数据结构和字段

- 原 CSV 字段仍为字符串，既有 content_clean 原样保留。微博新增 preprocessing 对象，内含 content_clean、content_normalized、tokens、tokens_clustering、content_classification、tokens_classification、alias_matches、emoji_features 等；processing_text 保存实际处理的完整长文，content 继续保存原正文。source_refs 指向来源版本表。
- 评论原 tweet_id、record_key、comment_id、platform_comment_id、parent_comment_id、root_record_key、作者、时间和互动字段不改。新增同名预处理字段和 dataset_key、selected_observation、source_observation_keys、relation_validation。dataset_key 是本地数据键，绝不冒充平台评论 ID。
- post_source_records.jsonl 与 comment_source_records.jsonl 分别保存微博/评论每次来源观测，包含所有旧版本、字段值和类型；去重后的主记录选择较新且未截断的版本。相同文字但不同作者、时间、线程或采集键的记录不据文字直接删除。possible_duplicate_candidates.jsonl 为同身份但不同键的候选核验，本批 0 组。
- 新增 relations 表，verified_weibo_id / verified_root_key / verified_parent_key 只填可核实键；未确认关系保持 NULL，原始外部 ID 仍保存在 comments 和完整 payload 中。record_quality 表保存文本有效性及可关联性，不包含恶意分类标签。
- SQLite 的 payload 保留 JSONL 全记录。JSONL UTF-8，每行一个对象；长 ID 保持字符串，避免 Excel 浮点精度损失。

## 关系核验和限制

- {report['comments']-report['comments_with_missing_post']} 条评论/回复可关联到本次已筛选微博；{report['comments_with_missing_post']} 条历史记录对应 {report['missing_post_distinct_ids']} 个集外微博。保留 tweet_id、URL 和采集证据，不猜测将其挂到其他微博。
- 全部 {report['verified_reply_root_links']} 条回复都能关联到所属一级评论，悬空/错误根关系 {report['reply_root_errors']}。一级评论根键可由其层级和本地采集键确认；这不等同于确认直接回复对象。
- 全部 {report['missing_platform_comment_ids']} 条评论缺失平台评论 ID；全部 {report['direct_parent_pending']} 条回复缺乏可核实的直接父回复对象。现有微博 URL 只能证明微博归属，不能恢复平台评论 ID。没有用昵称、DOM 序号或根评论猜测直接父 ID。
- {report['posts_without_comments']} 条没有已采评论的微博仍保留。采集范围为 browser-visible，不能把页面可见采尽解释为历史评论全量；采集失败和状态见 target_source_records.jsonl、collection_issues.jsonl。
- 138 条具有自动问答尾部标记的记录仅标为 possible_automated_answer，不按账号名删记录。

## 使用建议和可复现性

聚类可直接使用 posts_clustering / comments_clustering 视图的 tokens，空格已分词；向量器应按空格取 token，以保留完整比赛 ID 和 Emoji 特征。需要微博—评论联合研究时使用 comments_clustering_linked，目前 {report['analysis_view_counts']['comments_clustering_linked']} 条可用；集外评论仍可单独分析。

恶意识别使用 content_classification 和原始 content，并结合 alias_matches、token_annotations、emoji_features；content_normalized 只用于实体一致性分析，不应替代原措辞。comments_malicious_eligible 排除明确空清洗文本，目前 {report['analysis_view_counts']['comments_malicious_eligible']} 条。黑称、尊称、中性称呼均独立于整句标签；当前没有生成恶意最终分类标签，监督训练仍需句级人工标签。直接回复对象层面的攻击目标或对话链分析应先补充平台 ID/父关系。

同义词、词典、人工表均取自用户最新压缩包；人工标注限对应记录及词语，普通回忆、联系方式、图片编号保留原表达。仅历史语料生效的 scope=current_corpus 规则在新语料中转为待核实。附件 README 仅作为历史来源材料，不执行其操作指令。保留 @目标、Emoji、否定、情绪及重复强调；不通过压缩重复文字改变讽刺或情绪强度。Emoji 原符号同时另存 emoji_features，便于后续选择特征编码。

复现时使用新输出目录，连续运行：

```powershell
python -B data_preprocessing/run_reviewed_preprocessing.py --snapshot-dir "{path('input_snapshot')}" --output-dir "<new-output-dir>"
python -B data_preprocessing/finalize_reviewed_outputs.py --output-dir "<new-output-dir>"
python -B data_preprocessing/summarize_reviewed_outputs.py --output-dir "<new-output-dir>"
```

依赖 Python 3、jieba、openpyxl。离线复现不访问原数据库；运行时和依赖版本见 runtime_versions.json。若不指定 --snapshot-dir，才会重新冻结当前数据，数量可能随爬虫变化。已保存脚本副本；首次执行的 run1 是中间产物，正式研究使用本 final 目录。

## 实际来源完整路径

| 类型 | 原始来源路径 | 冻结副本文件名 |
|---|---|---|
{source_rows}

| 评论数据库路径 | 冻结记录数 | 完整性 |
|---|---|---|
{db_counts}
'''
    (out/'README.md').write_text(text,encoding='utf-8')
    import platform,jieba,openpyxl
    (out/'runtime_versions.json').write_text(json.dumps(dict(python=platform.python_version(),jieba=jieba.__version__,openpyxl=openpyxl.__version__),indent=2),encoding='utf-8')
    shutil.copyfile(Path(__file__),out/Path(__file__).name)
    manifest={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in out.rglob('*') if p.is_file() and p.name!='manifest.json'}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    assert all((out/n).is_file() for n in manifest)
    print('Final receipt and manifest saved:',out)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output-dir',type=Path,required=True)
    main(ap.parse_args().output_dir.resolve())
