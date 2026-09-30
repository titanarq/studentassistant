// Dev-only real-browser check of the workspace layout controls (#538). NOT part of scripts/test.sh.
// Usage (see docs/modules/web.md):
//   npm i --no-save playwright-core        (nothing is added to package.json)
//   npx vite --port 5199                   (any spare port; the API is mocked, no backend needed)
//   node e2e/layout-check.mjs [http://127.0.0.1:5199] [screenshot dir]
// CHROME=/path/to/chrome overrides the browser (default /usr/bin/google-chrome).
// The API is mocked with page.route: no backend, vault or network is involved.
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const base = process.argv[2] ?? "http://127.0.0.1:5199";
const out = process.argv[3] ?? "e2e-shots";
mkdirSync(out, { recursive: true });
const WIDTHS = [1024, 1280, 1600];
const HEIGHT = 800;
let failures = 0;
const check = (ok, msg) => {
  if (!ok) failures += 1;
  console.log(`${ok ? "ok  " : "FAIL"} ${msg}`);
};

const browser = await chromium.launch({
  executablePath: process.env.CHROME ?? "/usr/bin/google-chrome",
  args: ["--no-sandbox"],
});

async function open(width, mode) {
  const context = await browser.newContext({ viewport: { width, height: HEIGHT } });
  const page = await context.newPage();
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname.endsWith("/topics")) return json([{ topic_id: "t", name: "Tema de prueba", subject_id: "s" }]);
    return json({ detail: "mock" }, 404);
  });
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("no", "NotAllowedError"));
  });
  await page.goto(`${base}/subjects/s/topics/t/${mode === "construir" ? "workspace" : "study"}`);
  await page.waitForSelector(".workspace-chat");
  await page.waitForTimeout(300);
  return { page, context };
}

const rect = (page, sel) =>
  page.evaluate((s) => {
    const e = document.querySelector(s);
    if (!e) return null;
    const cs = getComputedStyle(e);
    if (cs.display === "none" || cs.visibility === "hidden") return null;
    const r = e.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom };
  }, sel);

async function state(page) {
  return {
    left: await rect(page, ".workspace-left"),
    src: await rect(page, ".workspace-sources"),
    chat: await rect(page, ".workspace-chat"),
    doc: await rect(page, ".workspace-document"),
    srcToggle: await rect(page, ".workspace-left-toggle"),
    chatToggle: await rect(page, ".workspace-chat > .workspace-toggle, .workspace-chat .workspace-chat-tools .workspace-toggle"),
  };
}

const inside = (inner, outer) =>
  inner && outer && inner.x >= outer.x - 0.5 && inner.r <= outer.r + 0.5 && inner.y >= outer.y - 0.5 && inner.b <= outer.b + 0.5;
const overlap = (a, b) => a && b && a.x < b.r - 0.5 && b.x < a.r - 0.5 && a.y < b.b - 0.5 && b.y < a.b - 0.5;

// panel: "expanded" | "collapsed" | "hidden" (the chat is expanded over it)
function coherent(label, s, panel) {
  check(s.chat !== null, `${label}: chat visible`);
  check(s.chatToggle !== null, `${label}: chat toggle visible`);
  check(inside(s.chatToggle, s.chat), `${label}: chat toggle inside the chat box`);
  if (panel === "hidden") {
    check(s.src === null, `${label}: panel hidden`);
    check(
      s.chat && s.left && Math.abs(s.chat.b - s.left.b) < 2 && Math.abs(s.chat.y - s.left.y) < 2,
      `${label}: chat fills the column (no dead area)`,
    );
  } else {
    check(s.src !== null, `${label}: panel visible`);
    check(s.srcToggle !== null, `${label}: panel toggle visible`);
    check(inside(s.srcToggle, s.src), `${label}: panel toggle inside the panel box`);
    check(!overlap(s.src, s.chat), `${label}: panel and chat do not overlap`);
    check(s.chat && s.left && Math.abs(s.chat.b - s.left.b) < 2, `${label}: chat reaches the column bottom`);
    check(s.src && s.left && Math.abs(s.src.y - s.left.y) < 2, `${label}: panel at the column top`);
    if (panel === "collapsed") check(s.src && s.src.h < 80, `${label}: collapsed panel is one line (${Math.round(s.src?.h ?? 0)}px)`);
    else check(s.src && s.src.h > 120, `${label}: expanded panel has a body (${Math.round(s.src?.h ?? 0)}px)`);
  }
  check(!overlap(s.srcToggle, s.chatToggle), `${label}: toggles do not overlap`);
}

const click = (page, name) => page.getByRole("button", { name, exact: true }).click();

for (const mode of ["construir", "estudiar"]) {
  for (const width of WIDTHS) {
    const tag = `${mode}@${width}`;
    console.log(`--- ${tag}`);
    const { page, context } = await open(width, mode);
    const shot = (n) => page.screenshot({ path: `${out}/${tag}-${n}.png` });
    const panelToggle = page.locator(".workspace-left-toggle");
    const isCollapsed = async () => (await page.locator('.workspace-sources[data-collapsed="true"]').count()) > 0;

    const initial = (await isCollapsed()) ? "collapsed" : "expanded";
    coherent(`${tag} initial (${initial})`, await state(page), initial);
    await shot("1-initial");
    if (initial === "collapsed") await panelToggle.click();
    coherent(`${tag} panel expanded`, await state(page), "expanded");
    await shot("2-expanded");

    // The three cards share border, corners and shadow, and the panel's edges line up with the chat's.
    const styleOf = (sel) =>
      page.evaluate((s) => {
        const c = getComputedStyle(document.querySelector(s));
        return [c.borderTopWidth, c.borderTopColor, c.borderTopLeftRadius, c.boxShadow].join("|");
      }, sel);
    const expandedStyle = await styleOf(".workspace-sources");
    check(expandedStyle === (await styleOf(".workspace-chat")), `${tag} panel and chat share border/radius/shadow`);
    const e = await state(page);
    check(Math.abs(e.src.x - e.chat.x) < 1 && Math.abs(e.src.r - e.chat.r) < 1, `${tag} panel and chat edges align`);
    check(e.chat.y - e.src.b > 4 && e.chat.y - e.src.b < 32, `${tag} gap between panel and chat (${Math.round(e.chat.y - e.src.b)}px)`);

    await panelToggle.click();
    coherent(`${tag} panel collapsed`, await state(page), "collapsed");
    check(expandedStyle === (await styleOf(".workspace-sources")), `${tag} collapsed panel keeps its card (border/radius/shadow)`);
    const bg = await page.evaluate(() => getComputedStyle(document.querySelector(".workspace-sources")).backgroundColor);
    check(bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent", `${tag} collapsed panel keeps its background (${bg})`);
    const c = await state(page);
    check(c.srcToggle.r <= c.src.r - 4 && c.srcToggle.r >= c.src.r - 16, `${tag} collapsed toggle at the header's right end`);
    await shot("3-collapsed");

    await click(page, "Ampliar chat");
    coherent(`${tag} chat expanded (panel was collapsed)`, await state(page), "collapsed");
    {
      // The maximized chat fills the column below the pill: no overlap, bottom flush with the column.
      const m = await state(page);
      check(m.chat.y >= m.src.b, `${tag} maximized chat starts below the pill`);
      check(Math.abs(m.chat.b - m.left.b) < 2, `${tag} maximized chat reaches the column bottom`);
    }
    await shot("4-chat-expanded");
    await click(page, "Reducir chat");
    coherent(`${tag} chat reduced -> panel back to collapsed`, await state(page), "collapsed");

    await panelToggle.click();
    coherent(`${tag} panel expanded again`, await state(page), "expanded");
    await click(page, "Ampliar chat");
    coherent(`${tag} chat expanded (panel was expanded)`, await state(page), "hidden");
    await click(page, "Reducir chat");
    coherent(`${tag} chat reduced -> panel back to expanded`, await state(page), "expanded");

    // Both toggles in sequence, then a reload with the persisted state.
    await panelToggle.click();
    await click(page, "Ampliar chat");
    await click(page, "Reducir chat");
    coherent(`${tag} sequence: collapse, expand chat, reduce chat`, await state(page), "collapsed");
    await panelToggle.click();
    await click(page, "Ampliar chat");
    await page.reload();
    await page.waitForSelector(".workspace-chat");
    await page.waitForTimeout(300);
    const afterReload = await state(page);
    coherent(`${tag} reload with the chat expanded`, afterReload, afterReload.src === null ? "hidden" : afterReload.src.h < 80 ? "collapsed" : "expanded");
    await shot("5-reload");
    await context.close();
  }
}
await browser.close();
console.log(failures === 0 ? "ALL OK" : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
