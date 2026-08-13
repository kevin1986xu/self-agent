"use client";

/**
 * 工作台面板（M2-11 二开）：右侧抽屉，展示
 * - 计划：deepagents TodoListMiddleware 的 todos（stream values 实时）
 * - 文件：会话工作区产物（网关 /files API），可下载
 */

import { useEffect, useState } from "react";
import { useStreamContext } from "@/providers/Stream";

const GATEWAY =
  process.env.NEXT_PUBLIC_GATEWAY_URL ?? "http://localhost:8400";

type Todo = { content: string; status: "pending" | "in_progress" | "completed" };
type WsFile = { path: string; size: number; mtime: number };

const STATUS_ICON: Record<Todo["status"], string> = {
  pending: "○",
  in_progress: "◐",
  completed: "●",
};

export function WorkPanel() {
  const stream = useStreamContext();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"todos" | "files">("todos");
  const [files, setFiles] = useState<WsFile[]>([]);

  const todos: Todo[] =
    ((stream.values as Record<string, unknown>)?.todos as Todo[]) ?? [];

  useEffect(() => {
    if (!open || tab !== "files") return;
    let alive = true;
    const load = () =>
      fetch(`${GATEWAY}/files`)
        .then((r) => r.json())
        .then((d) => alive && setFiles(d.files ?? []))
        .catch(() => undefined);
    load();
    const t = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [open, tab]);

  const doing = todos.filter((t) => t.status !== "completed").length;

  return (
    <>
      <button
        onClick={() => setOpen(!open)}
        className="fixed top-16 right-0 z-40 rounded-l-lg border border-r-0 bg-white px-2 py-3 text-xs shadow-sm hover:bg-gray-50"
        title="工作台"
      >
        {open ? "»" : `工作台${doing ? ` (${doing})` : ""}`}
      </button>
      {open && (
        <div className="fixed top-0 right-0 z-30 flex h-screen w-80 flex-col border-l bg-white shadow-lg">
          <div className="flex border-b">
            {(
              [
                ["todos", `计划 ${todos.length ? `(${todos.length})` : ""}`],
                ["files", "文件"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`flex-1 py-3 text-sm ${
                  tab === key
                    ? "border-b-2 border-blue-800 font-semibold text-blue-900"
                    : "text-gray-500"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="flex-1 overflow-y-auto p-3">
            {tab === "todos" &&
              (todos.length ? (
                <ul className="space-y-2">
                  {todos.map((t, i) => (
                    <li
                      key={i}
                      className={`flex gap-2 text-sm ${
                        t.status === "completed"
                          ? "text-gray-400 line-through"
                          : t.status === "in_progress"
                            ? "font-medium text-blue-900"
                            : "text-gray-700"
                      }`}
                    >
                      <span>{STATUS_ICON[t.status] ?? "○"}</span>
                      <span>{t.content}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="pt-8 text-center text-sm text-gray-400">
                  当前会话暂无任务计划
                </p>
              ))}
            {tab === "files" &&
              (files.length ? (
                <ul className="space-y-2">
                  {files.map((f) => (
                    <li
                      key={f.path}
                      className="flex items-center justify-between gap-2 text-sm"
                    >
                      <span className="truncate" title={f.path}>
                        {f.path}
                      </span>
                      <a
                        className="shrink-0 text-blue-700 hover:underline"
                        href={`${GATEWAY}/files/download?path=${encodeURIComponent(f.path)}`}
                      >
                        下载
                      </a>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="pt-8 text-center text-sm text-gray-400">
                  工作区暂无产物文件
                </p>
              ))}
          </div>
          <div className="border-t p-2 text-center">
            <a
              className="text-xs text-gray-400 hover:text-blue-800"
              href={`${GATEWAY}/admin`}
              target="_blank"
            >
              打开管理台（技能 / 知识库 / 审批）↗
            </a>
          </div>
        </div>
      )}
    </>
  );
}
