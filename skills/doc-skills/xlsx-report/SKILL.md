---
name: xlsx-report
description: 生成 Excel 表格（.xlsx）。适用于图斑清单、任务台账、排期表、数据导出等表格类交付物。输入为结构化 JSON 规格，输出可下载的 xlsx 文件。
---

# Excel 表格生成

## 步骤

1. 查询业务数据后，用 write_file 写规格到 `/work/xlsx_spec.json`：

```json
{
  "sheets": [
    {"name": "图斑清单",
     "headers": ["图斑编号", "类型", "面积(亩)", "状态"],
     "rows": [["汉川市-…-00001", "土地规委会", 1472.4, "待核查"]]}
  ]
}
```

- 每个 sheet 一张工作表；rows 里数字直接用数字类型（不要引号），便于用户后续计算；
- 数据必须来自真实工具查询结果。

2. 执行：

```
python skills/doc-skills/xlsx-report/scripts/make_xlsx.py work/xlsx_spec.json work/图斑清单.xlsx
```

3. 脚本打印 `OK <路径> <表数>`，把文件路径告知用户。

## 硬性纪律

- 产物一律写到 `/work/` 目录（execute 命令里写 `work/` 相对路径也等价），**禁止写 /tmp 或任何工作区之外的路径**；
- 脚本执行失败时如实报告 stderr，**禁止内联重写生成代码绕过脚本**——脚本是审计与格式一致性的保证。
