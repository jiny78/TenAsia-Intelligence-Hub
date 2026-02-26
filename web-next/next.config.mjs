/** @type {import('next').NextConfig} */
const nextConfig = {
  // standalone: Docker production 이미지에서 최소 파일만으로 서버 실행 가능
  // server.js + .next/static + public 만으로 동작
  output: "standalone",

  // ── 리버스 프록시 ──────────────────────────────────────────────
  // rewrites()는 빌드 타임에 평가되어 FASTAPI_URL이 localhost:8000으로 고정됨.
  // 대신 app/api/[...path]/route.ts 에서 런타임에 FASTAPI_URL을 읽어 프록시합니다.

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
