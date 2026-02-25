"use client";
import { AutomationStatusCard } from "@/components/automation/AutomationStatusCard";
import { ResolutionFeed } from "@/components/automation/ResolutionFeed";
import { ConflictList } from "@/components/automation/ConflictList";

export default function AutomationPage() {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-heading font-bold gradient-text">Automation</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          AI 자율 처리 현황을 모니터링하고 미해결 데이터 충돌을 검토합니다.
        </p>
      </div>

      {/* 24h Summary */}
      <AutomationStatusCard />

      {/* Two-column layout: feed + conflicts */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <ResolutionFeed />
        <ConflictList status="OPEN" />
      </div>
    </div>
  );
}
