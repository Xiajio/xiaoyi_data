// 小亿数据演示站静态服务：内网演示用，含 CSP frame-ancestors 宿主白名单。
// 用法：node serve.mjs [port]（默认 5274），目录为脚本所在目录。
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";

// 仓库化部署时服务构建产物 dist/；独立目录部署时服务脚本所在目录。
const here = fileURLToPath(new URL(".", import.meta.url));
const root = existsSync(join(here, "dist", "index.html")) ? join(here, "dist") : here;
const port = Number(process.argv[2] ?? 5274);
// 允许嵌入本站的宿主（与构建期 VITE_DEMO_ALLOWED_ORIGINS 对齐）。
const FRAME_ANCESTORS = "http://10.10.70.12:5273 http://localhost:5273";
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
};

createServer(async (req, res) => {
  try {
    const url = new URL(req.url ?? "/", "http://localhost");
    let path = normalize(decodeURIComponent(url.pathname));
    if (path === "/" || path === "\\") path = "/index.html";
    const file = join(root, path);
    if (!file.startsWith(root)) { res.writeHead(403).end(); return; }
    const body = await readFile(file);
    res.writeHead(200, {
      "content-type": TYPES[extname(file).toLowerCase()] ?? "application/octet-stream",
      "content-security-policy": `frame-ancestors 'self' ${FRAME_ANCESTORS}`,
      "cache-control": "no-cache",
    });
    res.end(body);
  } catch {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("not found");
  }
}).listen(port, "0.0.0.0", () => console.log(`xiaoyi-data demo listening at http://0.0.0.0:${port}`));
