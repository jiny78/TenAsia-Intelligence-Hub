import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // standalone: Docker production 이미지에서 최소 파일만으로 서버 실행 가능
  // server.js + .next/static + public 만으로 동작
  output: "standalone",

  // ── 리버스 프록시 ──────────────────────────────────────────────
  // 브라우저가 /api/* 를 호출하면 Next.js 서버가 FastAPI로 전달합니다.
  // - 브라우저 입장에서는 동일 출처(same-origin) → CORS 불필요
  // - FASTAPI_URL: App Runner 런타임 환경 변수 (빌드 시 불필요)
  // - 로컬 개발: NEXT_PUBLIC_API_URL 설정 시 프록시 미사용 (direct 호출)
  async rewrites() {
    const fastapiUrl = process.env.FASTAPI_URL || "http://localhost:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${fastapiUrl}/:path*`,
      },
    ];
  },

  images: {
    remotePatterns: [
      { protocol: "https", hostname: "*.amazonaws.com" },
      { protocol: "https", hostname: "*.s3.amazonaws.com" },
      // App Runner 프로덕션 도메인
      { protocol: "https", hostname: "*.awsapprunner.com" },
      // 로컬 개발: FastAPI 정적 파일 서버
      { protocol: "http", hostname: "localhost" },
      { protocol: "http", hostname: "localhost", port: "8000" },
      // Docker 컨테이너 내부 네트워크에서 api 서비스 접근
      { protocol: "http", hostname: "api" },
    ],
  },
};

export default nextConfig;
