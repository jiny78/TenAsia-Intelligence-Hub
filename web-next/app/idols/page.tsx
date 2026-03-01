"use client";

import { useState } from "react";
import { GroupsTab } from "./GroupsTab";
import { ArtistsTab } from "./ArtistsTab";
import { MappingsTab } from "./MappingsTab";
import { EnrichPanel } from "./EnrichPanel";

const TABS = [
  { id: "groups",   label: "그룹 관리" },
  { id: "artists",  label: "아티스트 관리" },
  { id: "mappings", label: "기사-아이돌 매핑" },
  { id: "enrich",   label: "프로필 보강" },
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
      {tab === "artists"  && <ArtistsTab />}
      {tab === "mappings" && <MappingsTab />}
      {tab === "enrich"   && <EnrichPanel />}
    </div>
  );
}
