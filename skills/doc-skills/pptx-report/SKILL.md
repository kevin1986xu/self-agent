---
name: pptx-report
description: 生成 PowerPoint 演示文稿（.pptx）。适用于巡检周报、任务汇报、数据汇总等需要 PPT 交付物的场景。输入为结构化 JSON 规格，输出可下载的 pptx 文件。
---

# PPT 报告生成

把业务数据整理成 JSON 规格文件，调用脚本生成 pptx。

## 步骤

1. 先查询所需业务数据（图斑/任务/告警/媒体等工具）。
2. 用 write_file 把内容规格写到工作区，如 `/work/report_spec.json`，格式：

```json
{
  "title": "汉川市巡检周报",
  "subtitle": "2026-08-11 ~ 2026-08-17",
  "sections": [
    {
      "heading": "本周任务概览",
      "bullets": ["完成核查任务 12 架次", "覆盖图斑 9 个"],
      "table": {
        "headers": ["图斑编号", "面积(亩)", "状态"],
        "rows": [["汉川市-…-00001", "1472.4", "已核查"]]
      }
    }
  ]
}
```

- 每个 section 生成一页；`bullets` 与 `table` 均可选；
- 表格控制在 8 行以内，超出拆分为多个 section；
- 数据必须来自真实工具查询结果，不得编造。

3. 执行脚本（工作区根目录相对路径）：

```
python skills/doc-skills/pptx-report/scripts/make_pptx.py work/report_spec.json work/巡检周报.pptx
```

4. 确认输出：脚本打印 `OK <路径> <页数>`。把文件路径告知用户（文件在会话工作区，可下载）。

## 注意

- 输出文件名用中文业务名（如 `巡检周报.pptx`）；
- 脚本失败时把 stderr 原样报告，不要自行猜测内容重试超过 2 次。

## 硬性纪律

- 产物一律写到 `/work/` 目录（execute 命令里写 `work/` 相对路径也等价），**禁止写 /tmp 或任何工作区之外的路径**；
- 脚本执行失败时如实报告 stderr，**禁止内联重写生成代码绕过脚本**——脚本是审计与格式一致性的保证。
