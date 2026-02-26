// Runtime proxy: /api/* → FASTAPI_URL/*
// next.config.mjs rewrites()는 빌드 타임에 고정되므로,
// 런타임에 FASTAPI_URL 환경변수를 읽어야 하는 경우 이 핸들러를 사용합니다.
import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const FASTAPI_BASE = (process.env.FASTAPI_URL || "http://localhost:8000").replace(/\/$/, "");

async function proxy(
  request: NextRequest,
  { params }: { params: { path: string[] } }
) {
  const pathStr = params.path.join("/");
  const { search } = new URL(request.url);
  const url = `${FASTAPI_BASE}/${pathStr}${search}`;

  const init: RequestInit = { method: request.method };
  const ct = request.headers.get("content-type");
  if (ct) init.headers = { "content-type": ct };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text();
  }

  const upstream = await fetch(url, init);
  const body = await upstream.arrayBuffer();

  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}

export const GET    = proxy;
export const POST   = proxy;
export const PUT    = proxy;
export const PATCH  = proxy;
export const DELETE = proxy;
