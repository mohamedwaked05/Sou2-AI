import { writeFile } from "node:fs/promises";
import process from "node:process";

const outputDirectory = process.argv[2];
const targets = await fetch("http://127.0.0.1:9222/json/list").then((response) =>
  response.json(),
);
const target = targets.find((item) => item.type === "page");
if (!target) throw new Error("No Chrome page target found.");

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let sequence = 0;
let authenticated = false;
let onboarding = false;
const pending = new Map();
const browserErrors = [];

function command(method, params = {}) {
  const id = ++sequence;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

const business = {
  id: "business-1",
  name: "Maya Bakery",
  description: "A neighborhood bakery serving fresh bread and pastries every day.",
  category: "BAKERY",
  custom_category: null,
  default_language: "en",
  governorate: "Beirut",
  district: "Beirut",
  city: "Beirut",
  address_line: "Hamra Street",
  status: "ACTIVE",
  is_active: true,
  profile_complete: true,
  first_incomplete_section: null,
  onboarding_submitted_at: "2026-08-23T08:00:00Z",
  working_hours: [
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
  ].map((weekday) => ({
    weekday,
    is_closed: weekday === "SUNDAY",
    shifts: weekday === "SUNDAY" ? [] : [{ start: "09:00", end: "17:00" }],
  })),
};
const owner = {
  id: "owner-1",
  email: "owner@example.com",
  first_name: "Maya",
  last_name: "Haddad",
  email_verified_at: "2026-08-23T08:00:00Z",
  status: "ACTIVE",
};

function responseFor(url, method) {
  const path = new URL(url).pathname;
  if (path.endsWith("/auth/refresh")) {
    return authenticated
      ? [200, { access_token: "visual-review-token" }]
      : [401, { error: { code: "refresh_token_invalid", message: "Session ended." } }];
  }
  if (path.endsWith("/auth/me")) return [200, owner];
  if (path === "/api/v1/businesses") return [200, [business]];
  if (path === "/api/v1/businesses/business-1") {
    return [
      200,
      onboarding
        ? {
            ...business,
            status: "PENDING",
            is_active: false,
            profile_complete: false,
            first_incomplete_section: "business_details",
            onboarding_submitted_at: null,
          }
        : business,
    ];
  }
  if (path.endsWith("/knowledge/documents")) {
    return [
      200,
      [
        {
          id: "document-1",
          original_filename: "bakery-policies.docx",
          mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          file_size_bytes: 18432,
          status: "READY",
          failure_code: null,
          page_count: 2,
          created_at: "2026-08-23T08:00:00Z",
          updated_at: "2026-08-23T08:02:00Z",
        },
      ],
    ];
  }
  if (path.endsWith("/ai-usage/current")) {
    return [
      200,
      {
        window_start: "2026-08-23T00:00:00Z",
        window_end: "2026-08-24T00:00:00Z",
        reset_at: "2026-08-24T00:00:00Z",
        daily_token_allowance: 50000,
        owner_reserved_tokens: 0,
        input_tokens_used: 800,
        output_tokens_used: 1200,
        total_tokens_used: 2000,
        tokens_currently_reserved: 0,
        tokens_remaining: 48000,
        usage_percentage: 4,
        status: "normal",
      },
    ];
  }
  if (path.endsWith("/owner-chat/messages")) {
    return [200, { items: [], next_cursor: null }];
  }
  if (method === "PATCH") return [200, business];
  if (path.endsWith("/onboarding/confirm")) {
    return [200, { ...business, status: "PENDING", is_active: false }];
  }
  return [200, { message: "Request accepted." }];
}

socket.addEventListener("message", async (event) => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const task = pending.get(message.id);
    if (!task) return;
    pending.delete(message.id);
    if (message.error) task.reject(new Error(message.error.message));
    else task.resolve(message.result);
    return;
  }
  if (message.method === "Runtime.exceptionThrown") {
    browserErrors.push(message.params.exceptionDetails.text);
  }
  if (message.method === "Log.entryAdded" && message.params.entry.level === "error") {
    browserErrors.push(message.params.entry.text);
  }
  if (message.method === "Fetch.requestPaused") {
    const [status, body] = responseFor(
      message.params.request.url,
      message.params.request.method,
    );
    await command("Fetch.fulfillRequest", {
      requestId: message.params.requestId,
      responseCode: status,
      responseHeaders: [{ name: "Content-Type", value: "application/json" }],
      body: Buffer.from(JSON.stringify(body)).toString("base64"),
    });
  }
});

await command("Page.enable");
await command("Runtime.enable");
await command("Log.enable");
await command("Fetch.enable", { patterns: [{ urlPattern: "*/api/v1/*" }] });

const delay = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

async function evaluate(expression) {
  const result = await command("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

async function waitFor(expression, timeout = 8000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (await evaluate(expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${expression}`);
}

async function setViewport(width, height) {
  await command("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: width < 768,
  });
}

async function open(path, { width, height, dark, signedIn, onboardingMode = false }) {
  authenticated = signedIn;
  onboarding = onboardingMode;
  await setViewport(width, height);
  await command("Page.navigate", { url: "http://127.0.0.1:5173/login" });
  await waitFor("location.origin === 'http://127.0.0.1:5173'");
  await evaluate(
    `localStorage.setItem('sou2ai-theme', '${dark ? "dark" : "light"}'); location.href = 'http://127.0.0.1:5173${path}'`,
  );
  await waitFor("!document.querySelector('.session-loading') && document.querySelector('h1')");
  await delay(150);
}

async function inspect(name) {
  const result = await evaluate(`(() => {
    const root = document.documentElement;
    const offenders = [...document.querySelectorAll('body *')]
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.right > innerWidth + 1 || rect.left < -1;
      })
      .slice(0, 8)
      .map((element) => element.className || element.tagName);
    return {
      name: ${JSON.stringify(name)},
      width: innerWidth,
      height: innerHeight,
      title: document.title,
      heading: document.querySelector('h1')?.textContent,
      horizontalOverflow: root.scrollWidth > innerWidth,
      scrollWidth: root.scrollWidth,
      offenders,
      theme: root.classList.contains('dark') ? 'dark' : 'light'
    };
  })()`);
  const screenshot = await command("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
  });
  await writeFile(`${outputDirectory}/${name}.png`, Buffer.from(screenshot.data, "base64"));
  return result;
}

const results = [];
for (const [path, name] of [
  ["/login", "auth-login"],
  ["/register", "auth-register"],
  ["/forgot-password", "auth-forgot"],
  ["/verify-email?token=visual", "auth-verify"],
  ["/reset-password?token=visual", "auth-reset"],
]) {
  for (const dark of [false, true]) {
    await open(path, { width: 1440, height: 900, dark, signedIn: false });
    results.push(await inspect(`${name}-${dark ? "dark" : "light"}-1440`));
  }
}

for (const [path, name] of [
  ["/businesses", "business-picker"],
  ["/businesses/business-1/overview", "overview"],
  ["/businesses/business-1/chat", "chat"],
  ["/businesses/business-1/conversations", "conversations"],
  ["/businesses/business-1/knowledge", "knowledge"],
  ["/businesses/business-1/analytics", "analytics"],
  ["/businesses/business-1/customers", "customers"],
  ["/businesses/business-1/data-sources", "data-sources"],
  ["/businesses/business-1/settings", "settings"],
  ["/account", "account"],
]) {
  for (const dark of [false, true]) {
    await open(path, { width: 1440, height: 900, dark, signedIn: true });
    results.push(await inspect(`${name}-${dark ? "dark" : "light"}-1440`));
  }
}

for (const [width, height] of [
  [1024, 768],
  [768, 1024],
  [390, 812],
]) {
  await open("/businesses/business-1/settings", {
    width,
    height,
    dark: false,
    signedIn: true,
  });
  results.push(await inspect(`settings-light-${width}`));
  await open("/businesses/business-1/overview", {
    width,
    height,
    dark: true,
    signedIn: true,
  });
  if (width < 768) {
    await evaluate("document.querySelector('.mobile-menu')?.click()");
    await waitFor("document.querySelector('.sidebar-mobile')");
  }
  results.push(await inspect(`overview-dark-${width}`));
}

await open("/businesses/business-1/onboarding", {
  width: 390,
  height: 812,
  dark: false,
  signedIn: true,
  onboardingMode: true,
});
results.push(await inspect("onboarding-step-1-mobile"));
await evaluate("document.querySelector('.form-actions > button')?.click()");
await waitFor("document.querySelector('.onboarding-intro')");
results.push(await inspect("onboarding-intro-mobile"));
await evaluate("document.querySelector('.onboarding-intro .btn')?.click()");
for (let step = 1; step <= 4; step += 1) {
  await evaluate("document.querySelector('form button[type=submit]:last-child')?.click()");
  await waitFor(`document.querySelector('.onboarding-title p')?.textContent?.includes('Step ${step + 1}')`);
  results.push(await inspect(`onboarding-step-${step + 1}-mobile`));
}

console.log(JSON.stringify({ results, browserErrors }, null, 2));
socket.close();
