import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // standalone: Docker production 이미지에서 최소 파일만으로 서버 실행 가능
  // server.js + .next/static + public 만으로 동작
  output: "standalone",

  images: {
    remotePatterns: [
      { protocol: "https", hostname: "*.amazonaws.com" },
      { protocol: "https", hostname: "*.s3.amazonaws.com" },
      // 로컬 개발: FastAPI 정적 파일 서버
      { protocol: "http", hostname: "localhost" },
      { protocol: "http", hostname: "localhost", port: "8000" },
      // Docker 컨테이너 내부 네트워크에서 api 서비스 접근
      { protocol: "http", hostname: "api" },
    ],
  },
};

export default nextConfig;
