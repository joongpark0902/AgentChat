"use strict";
/* 서비스 워커: ① 작업 완료 알림(푸시) 표시 ② 알림을 누르면 그 대화 열기 ③ PC 와 연결이 끊겼을 때 안내 화면.
   대화 내용·파일은 폰에 저장하지 않는다(캐시하지 않음). */

const OFFLINE_HTML = `<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>작업방</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:-apple-system,"Apple SD Gothic Neo",sans-serif;background:#fff;color:#1d1d1f;text-align:center;padding:24px}
@media (prefers-color-scheme: dark){body{background:#1c1c1e;color:#f5f5f7}}p{color:#86868b;line-height:1.6}button{margin-top:14px;border:0;border-radius:12px;background:#0a84ff;color:#fff;font-size:16px;font-weight:600;padding:11px 22px}</style>
</head><body><div><h2>PC 에 연결할 수 없습니다</h2><p>PC 가 켜져 있는지, 작업방이 실행 중인지,<br>폰의 Tailscale 이 켜져 있는지 확인해 주세요.</p>
<button onclick="location.reload()">다시 시도</button></div></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// 첫 화면(/)을 열 때만 끼어든다: PC 가 꺼져 있으면 안내 화면. 나머지 요청은 그대로 통과.
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : "" }; }
  const conv = d.data && d.data.conv;
  e.waitUntil(self.registration.showNotification(d.title || "작업방", {
    body: d.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    tag: conv || "agentchat",       // 같은 대화 알림은 하나로 바꿔 끼움
    renotify: true,
    data: { conv },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const conv = e.notification.data && e.notification.data.conv;
  const url = "/" + (conv ? `#conv=${encodeURIComponent(conv)}` : "");
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if ("focus" in w) {
        w.postMessage({ type: "open-conv", conv });
        return w.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});
