import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const here = path.dirname(fileURLToPath(import.meta.url));
const cacheFile = path.join(here, ".cache", "benchmark_private.json");
const outputDir = path.join(here, "results");
const outputFile = path.join(outputDir, "中文分词工具对比测试.xlsx");
const data = JSON.parse(await fs.readFile(cacheFile, "utf8"));
const { meta, samples, outcomes, tokens } = data;
const wb = Workbook.create();
const navy = "#17365D";
const blue = "#D9EAF7";
const gray = "#F3F6F9";
const green = "#E2F0D9";
const red = "#FCE4D6";

function setupSheet(name, title, width) {
  const sheet = wb.worksheets.add(name);
  sheet.showGridLines = false;
  sheet.getRange(`A1:${width}1`).values = [[title, ...Array(width.charCodeAt(0) - 65).fill("")]];
  sheet.getRange(`A1:${width}1`).format = { fill: navy, font: { bold: true, size: 15, color: "#FFFFFF" } };
  sheet.getRange(`A1:${width}1`).format.rowHeight = 30;
  return sheet;
}

function writeTable(sheet, row, headers, rows, widths = []) {
  const lastCol = String.fromCharCode(64 + headers.length);
  sheet.getRange(`A${row}:${lastCol}${row}`).values = [headers];
  sheet.getRange(`A${row}:${lastCol}${row}`).format = {
    fill: "#4472C4", font: { bold: true, color: "#FFFFFF" }, wrapText: true,
  };
  if (rows.length) sheet.getRange(`A${row + 1}:${lastCol}${row + rows.length}`).values = rows;
  sheet.getRange(`A${row}:${lastCol}${row + rows.length}`).format.borders = { preset: "all", style: "thin", color: "#D9E1F2" };
  sheet.getRange(`A${row + 1}:${lastCol}${row + rows.length}`).format.wrapText = true;
  widths.forEach((width, index) => {
    const col = String.fromCharCode(65 + index);
    sheet.getRange(`${col}:${col}`).format.columnWidth = width;
  });
  sheet.freezePanes.freezeRows(row);
  return row + rows.length;
}

const support = {
  jieba: "支持 add_word；逐词导入（低）",
  pkuseg: "未能运行；未验证（web 用户词典接口待确认）",
  jiojio: "支持 cws_user_dict；文件导入（中）",
  thulac: "支持 user_dict；每词一行（低）",
  cutword: "支持 custom_dict_path；需转为‘词 词频’两列（中）",
};
const perf = setupSheet("性能对比", "中文分词工具性能与资源对比", "M");
perf.getRange("A2:M4").values = [
  ["Python", meta.python, "系统", "Windows x64", "处理器", "Intel64 M140", "逻辑CPU", meta.logical_cpus, "", "", "", "", ""],
  ["样本数", meta.sample_count, "微博", meta.sample_by_type["微博"], "一级评论", meta.sample_by_type["一级评论"], "楼中楼", meta.sample_by_type["楼中楼"], "", "", "", "", ""],
  ["方法", "固定抽样；预热；5轮中位数", "", "", "库SHA256", meta.source_db_sha256.slice(0, 12), "词典SHA256", meta.dictionary_sha256.slice(0, 12), "", "", "", "", ""],
];
perf.getRange("A2:A4").format = { fill: blue, font: { bold: true } };
perf.getRange("C2:C4").format = { fill: blue, font: { bold: true } };
perf.getRange("E2:E4").format = { fill: blue, font: { bold: true } };
perf.getRange("G2:G4").format = { fill: blue, font: { bold: true } };
const perfHeaders = ["工具", "配置", "运行状态", "首次初始化秒", "缓存初始化中位秒", "总分词秒", "条/秒", "字符/秒", "加载后内存MB", "峰值内存MB", "包及模型MB", "含运行依赖MB", "词典支持及成本"];
const totalChars = samples.reduce((sum, item) => sum + item.text.length, 0);
const perfRows = outcomes.map((x) => {
  const ok = x.ok;
  return [x.tool, x.custom ? "领域词典" : "默认", ok ? "成功" : "未完成",
    ok ? x.first_init_s : null, ok ? x.init_median_s : null, ok ? x.elapsed_median_s : null,
    ok ? meta.sample_count / x.elapsed_median_s : null,
    ok ? totalChars / x.elapsed_median_s : null,
    ok ? x.rss_loaded_bytes / 1e6 : null,
    ok ? x.rss_peak_bytes / 1e6 : null,
    x.package_bytes == null ? null : x.package_bytes / 1e6,
    x.package_plus_dependencies_bytes == null ? null : x.package_plus_dependencies_bytes / 1e6,
    support[x.tool]];
});
const perfEnd = writeTable(perf, 6, perfHeaders, perfRows, [14, 14, 12, 15, 18, 13, 13, 15, 15, 15, 14, 17, 45]);
perf.getRange(`D7:H${perfEnd}`).format.numberFormat = "0.00";
perf.getRange(`I7:L${perfEnd}`).format.numberFormat = "0.0";
perf.getRange(`A7:M${perfEnd}`).format.rowHeight = 34;
for (let i = 0; i < perfRows.length; i++) {
  const row = 7 + i;
  perf.getRange(`C${row}`).format.fill = perfRows[i][2] === "成功" ? green : red;
}

const quality = setupSheet("分词质量对比", "领域词与标记表达完整性（非准确率）", "H");
quality.getRange("A2:H3").values = [
  ["实体完整率", "词典命中", "词组整词出现", "比例", "攻击表达完整率", "人工词表标记", "词组整词出现", "比例"],
  ["准确率", "无人工金标准", "不计算准确率", "", "指标说明", "仅作边界诊断", "非分类结论", ""],
];
quality.getRange("A2:H2").format = { fill: blue, font: { bold: true }, wrapText: true };
quality.getRange("A3:H3").format = { fill: gray, wrapText: true };
const qHeaders = ["工具", "配置", "实体整词命中", "实体词组总数", "实体完整率", "攻击性词组整词命中", "攻击性词组总数", "攻击性表达完整率"];
const qRows = outcomes.map((x) => [x.tool, x.custom ? "领域词典" : "默认",
  x.ok ? x.entity_hits : null, x.ok ? x.entity_total : null,
  x.ok && x.entity_total ? x.entity_hits / x.entity_total : null,
  x.ok ? x.hostile_hits : null, x.ok ? x.hostile_total : null,
  x.ok && x.hostile_total ? x.hostile_hits / x.hostile_total : null]);
const qEnd = writeTable(quality, 5, qHeaders, qRows, [15, 14, 16, 16, 14, 21, 20, 19]);
quality.getRange(`E6:E${qEnd}`).format.numberFormat = "0.0%";
quality.getRange(`H6:H${qEnd}`).format.numberFormat = "0.0%";
quality.getRange(`A6:H${qEnd}`).format.rowHeight = 27;

const cases = setupSheet("典型样本对照", "典型真实微博文本：默认与领域词典分词结果（本地敏感内容）", "P");
cases.getRange("A2:P2").values = [["本机固定抽样", "正式研究库真实文本", "词表标记仅作切分边界观察，不代表恶意言论分类", "", "", "", "", "", "", "", "", "", "", "", "", ""]];
cases.getRange("A2:P2").format = { fill: gray, font: { italic: true, color: "#404040" }, wrapText: true };
const toolLabels = ["jieba（默认）", "jieba（领域词典）", "pkuseg（默认）", "pkuseg（领域词典）", "jiojio（默认）", "jiojio（领域词典）", "thulac（默认）", "thulac（领域词典）", "cutword（默认）", "cutword（领域词典）"];
const caseHeaders = ["样本编号", "记录类型", "覆盖类别", "原始文本", ...toolLabels, "词典实体候选", "攻击性词表候选"];
const sampleRows = samples.map((s, i) => [s.sample_id, s.record_type, s.tags.join("；"), s.text,
  ...toolLabels.map((label) => (tokens[label]?.[i] || []).join(" / ") || (tokens[label] ? "（空结果）" : "未运行")),
  s.entities.join("；"), s.sensitive.join("；")]);
const caseEnd = writeTable(cases, 4, caseHeaders, sampleRows, [12, 12, 28, 55, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 30, 30]);
cases.getRange(`A5:P${caseEnd}`).format.rowHeight = 54;
cases.getRange(`D5:P${caseEnd}`).format.verticalAlignment = "top";
cases.freezePanes.freezeColumns(4);

const exceptions = setupSheet("运行异常", "安装、兼容性与词典适配记录", "E");
const issues = [];
for (const x of outcomes) {
  if (!x.ok) issues.push([x.tool, x.custom ? "领域词典配置" : "默认配置", "未完成", x.error || x.install_reason || "运行失败", x.install_reason || "按错误信息记录；未伪造结果"]);
}
issues.push(["pkuseg", "安装与web模型", "未完成", "Python 3.11 / Windows x64 构建需要 Microsoft Visual C++ 14.0+；补装构建工具并关闭隔离后仍缺编译器。", "没有改用其他分词库或下载大型模型；当前环境没有该编译器。"]);
issues.push(["cutword", "默认导入", "兼容绕行", "标准包入口无条件导入可选NER模块，进而需要未声明的torch。", "通过独立加载同包公开Cutter模块避开torch；CPU分词器本体正常运行。"]);
issues.push(["自定义词典", "适配说明", "已记录", "统一把项目词典转换为各工具可接受格式；pkuseg未能安装。", "jieba逐词add_word；jiojio cws_user_dict；THULAC单词每行；cutword词+词频两列。"]);
writeTable(exceptions, 3, ["工具/主题", "配置", "状态", "异常或限制", "处理及影响"], issues, [18, 20, 15, 65, 65]);

wb.recalculate();
await fs.mkdir(outputDir, { recursive: true });
const preview = await wb.render({ sheetName: "性能对比", autoCrop: "all", scale: 1, format: "png" });
const previewFile = path.join(here, ".cache", "workbook_preview.png");
await fs.writeFile(previewFile, new Uint8Array(await preview.arrayBuffer()));
const xlsx = await SpreadsheetFile.exportXlsx(wb);
await xlsx.save(outputFile);
console.log(JSON.stringify({ outputFile, sheets: ["性能对比", "分词质量对比", "典型样本对照", "运行异常"], rows: samples.length, previewFile }, null, 2));
await fs.rm(path.join(here, ".cache"), { recursive: true, force: true });
