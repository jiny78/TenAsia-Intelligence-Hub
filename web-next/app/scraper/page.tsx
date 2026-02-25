import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { ScrapeForm } from "@/components/scraper/scrape-form";
import { JobList } from "@/components/scraper/job-list";

export default function ScraperPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-heading font-bold gradient-text">Scraper</h1>
        <p className="mt-1 text-sm text-muted-foreground">날짜 범위를 선택하여 기사를 수집합니다.</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[360px,1fr]">
        {/* Control Card */}
        <Card>
          <CardHeader className="pb-4">
            <CardTitle className="text-base font-heading">스크래핑 설정</CardTitle>
            <CardDescription>날짜와 옵션을 선택하고 실행하세요.</CardDescription>
          </CardHeader>
          <CardContent>
            <ScrapeForm />
          </CardContent>
        </Card>

        {/* Live Jobs */}
        <Card>
          <CardHeader className="pb-4">
            <CardTitle className="text-base font-heading">작업 목록</CardTitle>
            <CardDescription>5초마다 자동 갱신됩니다.</CardDescription>
          </CardHeader>
          <CardContent>
            <JobList />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
