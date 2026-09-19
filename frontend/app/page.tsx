"use client";

import { useEffect } from "react";
import { useAppStore } from "@/stores/app-store";
import { CreateCards } from "@/components/home/create-cards";
import { RecentGenerations } from "@/components/home/recent-generations";

export default function HomePage() {
  const loadCatalog = useAppStore((s) => s.loadCatalog);

  useEffect(() => {
    void loadCatalog();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="mx-auto flex min-h-[calc(100vh-3.5rem)] max-w-5xl flex-col justify-center gap-10 px-8 py-12">
      <div className="text-center">
        <h1 className="accent-text text-3xl font-bold tracking-tight">Welcome Back</h1>
        <p className="mt-2 text-sm text-muted-foreground">What do you want to create?</p>
      </div>

      <CreateCards />
      <RecentGenerations />
    </div>
  );
}
