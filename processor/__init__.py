"""
processor 패키지 — Gemini 기반 데이터 정제 모듈

파이프라인:
    scraper (원시 HTML)
        └─► processor.cleaner.ArticleCleaner.process()
                ├─ HTML 정제 → 텍스트 추출
                ├─ Gemini API → 구조화 데이터 추출
                ├─ Pydantic 유효성 검증 (ArticleExtracted)
                └─ DB upsert (scraper.db.upsert_article)
"""
