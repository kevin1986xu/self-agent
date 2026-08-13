---
name: docx-report
description: 生成 Word 文档（.docx）。适用于核查报告、证据留存、正式公文类交付物。输入为结构化 JSON 规格，输出可下载的 docx 文件。
---

# Word 报告生成

## 步骤

1. 查询业务数据后，用 write_file 写规格到 `/work/doc_spec.json`（与 pptx-report 同构）：

```json
{
  "title": "图斑核查报告",
  "subtitle": "汉川市-土地规委会-20260626-00001",
  "sections": [
    {"heading": "核查结论", "paragraphs": ["经无人机航拍核查，……"],
     "bullets": ["面积 1472.4 亩", "状态：已核查"],
     "table": {"headers": ["项目", "值"], "rows": [["图斑编号", "…-00001"]]}}
  ]
}
```

- section 支持 `paragraphs`（正文段落）/ `bullets` / `table`，均可选；
- 数据必须来自真实工具查询结果。

2. 执行：

```
python skills/doc-skills/docx-report/scripts/make_docx.py work/doc_spec.json work/核查报告.docx
```

3. 脚本打印 `OK <路径>`，把文件路径告知用户。

## 硬性纪律

- 产物一律写到 `/work/` 目录（execute 命令里写 `work/` 相对路径也等价），**禁止写 /tmp 或任何工作区之外的路径**；
- 脚本执行失败时如实报告 stderr，**禁止内联重写生成代码绕过脚本**——脚本是审计与格式一致性的保证。
