import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("./speed-worker.js", import.meta.url), "utf8");
const { handleSpeed } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const env = { SPEED_TOKEN: "test-token", UPSTREAM_URL: "https://upstream.invalid/object.bin" };

test("leaves unrelated paths to the existing Worker", async () => {
  assert.equal(await handleSpeed(new Request("https://zone.invalid/other"), env), null);
});

test("requires a matching token only when authentication is configured", async () => {
  const response = await handleSpeed(new Request("https://zone.invalid/__speed"), env);
  assert.equal(response.status, 401);
  const wrong = await handleSpeed(new Request("https://zone.invalid/__speed?token=wrong"), env);
  assert.equal(wrong.status, 401);
});

test("public endpoint works without a configured password", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async () => new Response("public binary", {
    headers: { "Content-Type": "application/octet-stream" },
  });
  for (const password of [undefined, ""]) {
    const response = await handleSpeed(new Request("https://zone.invalid/__speed"), {
      UPSTREAM_URL: env.UPSTREAM_URL, SPEED_TOKEN: password,
    });
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "public binary");
  }
});

test("accepts CFIP token query authentication without forwarding credentials upstream", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async (url, options) => {
    assert.equal(url.href, env.UPSTREAM_URL);
    assert.equal(url.searchParams.has("token"), false);
    assert.equal(options.headers.has("X-Speed-Token"), false);
    return new Response("binary sample");
  };
  const response = await handleSpeed(new Request(`https://zone.invalid/__speed?token=${env.SPEED_TOKEN}`), env);
  assert.equal(response.status, 200);
});

test("streams the fixed upstream and confirms the origin headers", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async (url, options) => {
    assert.equal(url.href, env.UPSTREAM_URL);
    assert.equal(options.headers.get("Range"), "bytes=0-999");
    assert.equal(options.headers.has("X-Speed-Token"), false);
    return new Response("binary sample", { headers: { "Content-Type": "application/octet-stream" } });
  };
  const request = new Request("https://zone.invalid/__speed", {
    headers: { "X-Speed-Token": env.SPEED_TOKEN, Range: "bytes=0-999" },
  });
  const response = await handleSpeed(request, env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("X-Upstream-Mode"), "origin");
  assert.equal(response.headers.get("X-Upstream-Landed"), "upstream.invalid");
  assert.equal(await response.text(), "binary sample");
});

test("upstream failures remain failures", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async () => new Response("error", { status: 503 });
  const request = new Request("https://zone.invalid/__speed", { headers: { "X-Speed-Token": env.SPEED_TOKEN } });
  const response = await handleSpeed(request, env);
  assert.equal(response.status, 502);
  assert.equal(response.headers.has("X-Upstream-Mode"), false);
});
