"use client";

import { useState } from "react";
import { GroupsTab } from "./GroupsTab";
import { MappingsTab } from "./MappingsTab";

const TABS = [
  { id: "groups",   label: "그룹 상태 관리" },
  { id: "mappings", label: "기사-아이돌 매핑" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function IdolsPage() {
  const [tab, setTab] = useState<TabId>("groups");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-heading font-bold gradient-text">Idols</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          그룹 활동 상태 수동 편집 및 기사-아이돌 매핑 관리
        </p>
      </div>

      {/* Tab switcher */}
      <div className="flex items-center gap-1 rounded-lg bg-muted p-1 w-fit">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`rounded-md px-4 py-1.5 text-xs font-semibold transition-all ${
              tab === t.id
                ? "bg-background text-primary shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "groups"   && <GroupsTab />}
      {tab === "mappings" && <MappingsTab />}
    </div>
  );
}
