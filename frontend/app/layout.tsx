import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import { AgentSidebar } from "@/components/agent/agent-sidebar";
import { Toaster } from "sonner";

export const metadata: Metadata = {
  title: "AI Video Studio",
  description: "AI 视频生成整合工作台 —— LLM / TTS / 生图 / 视频生成统一工作界面",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Sidebar />
        <Topbar />
        <div className="flex pl-[72px] pt-14">
          <main className="min-w-0 flex-1">{children}</main>
          <AgentSidebar />
        </div>
        <Toaster theme="dark" position="bottom-right" richColors />
      </body>
    </html>
  );
}
