// Exercise the production bundle with synthetic data in an isolated Chrome profile.
// No live application, existing browser session, credentials or Gemini calls are used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const dist = path.join(frontend, "dist");
const html = readFileSync(path.join(dist, "index.html"), "utf8");
const asset = html.match(/src="(\/assets\/[^\"]+\.js)"/)[1];
const css = html.match(/href="(\/assets\/[^\"]+\.css)"/)[1];
const chrome = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].find((candidate) => candidate && existsSync(candidate));
assert.ok(chrome, "Chrome is required; set CHROME_PATH to its executable");

function fixture() {
  const ids = Array.from(
    { length: 25 },
    (_, i) => `${String(i + 1).padStart(8, "0")}-1111-4111-8111-111111111111`,
  );
  const conversations = ids.map((id, i) => ({
    id,
    title: i < 2 ? "Repeated long synthetic conversation title" : `Older chat ${i}`,
    creator_user_id: "owner",
    channel: "owner_web",
    next_turn_number: 1,
    last_message_at: null,
    latest_message_preview: "Long synthetic preview. ".repeat(12) + i,
    archived: i === 3,
    created_at: "2026-10-13T00:00:00Z",
    updated_at: "2026-10-13T00:00:00Z",
  }));
  const business = (id) => ({
    id,
    name: `Synthetic ${id}`,
    status: "ACTIVE",
    is_active: true,
    profile_complete: true,
    working_hours: [],
    category: "OTHER",
  });
  window.fetch = async (input, init = {}) => {
    const url = new URL(String(input), location.origin);
    if (
      url.origin !== location.origin ||
      (init.method && init.method !== "GET" && url.pathname !== "/api/v1/auth/refresh")
    ) {
      throw Error("Only synthetic read-only API requests are allowed");
    }
    const p = url.pathname;
    let data;
    if (p === "/api/v1/auth/refresh") data = { access_token: "synthetic" };
    else if (p === "/api/v1/auth/me")
      data = {
        id: "owner",
        email: "owner@example.test",
        first_name: "Test",
        last_name: "Owner",
        email_verified_at: "2026-10-13T00:00:00Z",
        status: "ACTIVE",
      };
    else {
      const match = p.match(/^\/api\/v1\/businesses\/([AB])(?:\/(.*))?$/);
      if (!match) throw Error(`Unexpected synthetic endpoint ${p}`);
      const [, tenant, tail] = match;
      if (!tail) data = business(tenant);
      else if (tail === "conversations")
        data = { items: conversations, next_cursor: null };
      else {
        const conversation = tail.match(/^conversations\/([^/]+)(\/messages)?$/);
        if (!conversation) throw Error(`Unexpected synthetic endpoint ${p}`);
        const [, id, messages] = conversation;
        if (!messages) data = conversations.find((item) => item.id === id);
        else {
          // Exercise stale responses during rapid selection and tenant changes.
          if (id === ids[1]) await new Promise((resolve) => setTimeout(resolve, 100));
          data = {
            conversation_id: id,
            items: [
              {
                id: `${tenant}-${id}`,
                role: "assistant",
                sequence_number: 2,
                content: `${tenant} history ${id}\n${"Synthetic paragraph. ".repeat(300)}`,
                created_at: "2026-10-13T00:00:00Z",
                generation_state: null,
                sources: [],
              },
            ],
            next_cursor: null,
          };
        }
      }
    }
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  window.fixtureIds = ids;
}

async function exercise() {
  const ids = window.fixtureIds;
  const trace = [];
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const check = (condition, message) => {
    if (!condition) throw Error(message);
  };
  async function until(test, label) {
    for (let i = 0; i < 200; i++) {
      if (test()) return;
      await pause(20);
    }
    throw Error(`Timed out: ${label}`);
  }
  const historyText = (id, tenant = "A") => `${tenant} history ${id}`;
  const shows = (id, tenant) =>
    document
      .querySelector('[role="log"]')
      ?.textContent.includes(historyText(id, tenant));
  async function selected(index, tenant = "A") {
    await until(
      () =>
        location.pathname.endsWith("/conversations") &&
        location.search === `?conversation=${ids[index]}` &&
        shows(ids[index], tenant),
      `selection ${index}`,
    );
    const button = document.querySelectorAll(".conversation-index-list > button")[
      index
    ];
    check(
      button?.getAttribute("aria-pressed") === "true",
      "Selected button must match URL and history",
    );
  }
  async function clickConversation(index, title = false) {
    const button = document.querySelectorAll(".conversation-index-list > button")[
      index
    ];
    button.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    await pause(60);
    const bounds = button.getBoundingClientRect();
    const list = button.parentElement.getBoundingClientRect();
    check(bounds.width <= list.width + 1, "Conversation button overflows its list");
    const target = title
      ? button.querySelector("strong").getBoundingClientRect()
      : bounds;
    const x = target.x + target.width / 2,
      y = target.y + target.height / 2;
    const hit = document.elementFromPoint(x, y);
    check(
      hit?.closest(".conversation-index-list > button") === button,
      "Conversation click is intercepted by another panel",
    );
    for (const type of ["mousedown", "mouseup", "click"])
      hit.dispatchEvent(
        new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          clientX: x,
          clientY: y,
          button: 0,
        }),
      );
    trace.push({ index, width: bounds.width, listWidth: list.width });
  }
  const link = (text) =>
    [...document.querySelectorAll("a")].find(
      (item) => item.textContent.trim() === text || item.title === text,
    );
  try {
    await selected(0); // default selection must not overwrite subsequent explicit IDs
    for (const index of [1, 2, 3, 24, 2]) {
      await clickConversation(index, index === 2);
      await selected(index);
      if (index === 3)
        check(!link("Open chat"), "Archived history must remain read-only");
    }
    check(
      link("Open chat")?.getAttribute("href").endsWith(`conversation=${ids[2]}`),
      "Open chat uses the selected ID",
    );
    link("Open chat").click();
    await until(
      () =>
        location.pathname.endsWith("/chat") &&
        document.body.textContent.includes(historyText(ids[2])),
      "Open chat",
    );
    history.back();
    await selected(2);
    history.forward();
    await until(() => location.pathname.endsWith("/chat"), "Forward to chat");
    history.back();
    await selected(2);
    link("Business Settings").click();
    await until(() => location.pathname.endsWith("/settings"), "Settings");
    link("Conversations").click();
    await selected(2);
    await clickConversation(1);
    await clickConversation(2);
    await selected(2);
    await pause(180);
    check(
      shows(ids[2]) &&
        !document
          .querySelector('[role="log"]')
          .textContent.includes(historyText(ids[1])),
      "Stale response replaced the selected history",
    );
    history.back();
    await selected(1);
    history.forward();
    await selected(2);
    history.pushState(null, "", `/businesses/B/conversations?conversation=${ids[2]}`);
    dispatchEvent(new PopStateEvent("popstate"));
    await selected(2, "B");
    check(
      !document.querySelector('[role="log"]').textContent.includes("A history"),
      "Previous tenant history leaked",
    );
    document.querySelector("#result").textContent = JSON.stringify({
      passed: true,
      trace,
    });
  } catch (error) {
    document.querySelector("#result").textContent = JSON.stringify({
      passed: false,
      error: error.message,
      url: location.href,
      trace,
    });
  }
}

const server = http.createServer((request, response) => {
  const name = new URL(request.url, "http://fixture.invalid").pathname;
  if ([asset, css, "/sou2ai-logo.png"].includes(name)) {
    response.setHeader(
      "Content-Type",
      name.endsWith(".js")
        ? "application/javascript"
        : name.endsWith(".css")
          ? "text/css"
          : "image/png",
    );
    response.end(readFileSync(path.join(dist, name)));
  } else if (name.startsWith("/api/")) {
    response.writeHead(500).end("Real API access forbidden");
  } else {
    response.setHeader("Content-Type", "text/html");
    response.end(
      `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="${css}"><script>(${fixture})();</script><script type="module" src="${asset}"></script></head><body><div id="root"></div><pre id="result">PENDING</pre><script type="module">(${exercise})();</script></body></html>`,
    );
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
try {
  for (const width of [1440, 1024, 390]) {
    const profile = mkdtempSync(path.join(os.tmpdir(), "sou2ai-conversation-browser-"));
    const child = spawn(
      chrome,
      [
        "--headless=new",
        "--no-first-run",
        "--disable-extensions",
        "--disable-background-networking",
        "--no-proxy-server",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        `--user-data-dir=${profile}`,
        `--window-size=${width},1000`,
        "--dump-dom",
        "--virtual-time-budget=20000",
        `http://127.0.0.1:${server.address().port}/businesses/A/conversations`,
      ],
      { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
    );
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    const code = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        child.kill();
        reject(Error("Isolated Chrome timed out"));
      }, 45000);
      child.on("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
      child.on("exit", (status) => {
        clearTimeout(timer);
        resolve(status);
      });
    });
    assert.equal(code, 0, stderr);
    const raw = stdout.match(/<pre id="result">(.*?)<\/pre>/s)?.[1];
    assert.ok(
      raw && raw !== "PENDING",
      `Chrome test did not finish; profile: ${profile}`,
    );
    const result = JSON.parse(
      raw.replaceAll("&amp;", "&").replaceAll("&lt;", "<").replaceAll("&gt;", ">"),
    );
    assert.equal(result.passed, true, JSON.stringify(result));
    console.log(
      `PASS ${width}px: click targets, sequential URLs/history, archived view, Open chat, Back/Forward, return, rapid switching and tenant isolation`,
    );
  }
} finally {
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
