/**
 * Optional origin-throughput endpoint. Configure UPSTREAM_URL (a stable large
 * binary object you are allowed to download). SPEED_TOKEN is optional: leave it
 * unset to allow direct /__speed access, or configure it to require a password.
 * To integrate into an existing Worker, call handleSpeed(request, env) at the
 * start of its fetch handler and return its result when it is not null.
 */
export async function handleSpeed(request, env) {
  const url = new URL(request.url);
  if (url.pathname !== "/__speed") return null;
  if (request.method !== "GET") return new Response("Method not allowed", { status: 405 });
  if (env.SPEED_TOKEN &&
      request.headers.get("X-Speed-Token") !== env.SPEED_TOKEN &&
      url.searchParams.get("token") !== env.SPEED_TOKEN) {
    return new Response("Unauthorized", { status: 401 });
  }
  let upstream;
  try {
    upstream = new URL(env.UPSTREAM_URL);
    if (upstream.protocol !== "https:") throw new Error("HTTPS required");
  } catch {
    return new Response("Set UPSTREAM_URL to an HTTPS binary download", { status: 503 });
  }
  const headers = new Headers({ "Accept-Encoding": "identity", "Cache-Control": "no-cache" });
  if (request.headers.has("Range")) headers.set("Range", request.headers.get("Range"));
  try {
    const response = await fetch(upstream, { headers, redirect: "follow", cache: "no-store" });
    if (![200, 206].includes(response.status) || !response.body) {
      return new Response("Upstream download failed", { status: 502 });
    }
    const outgoing = new Headers();
    // Preserve the original MIME type so the client can reject HTML/JSON error pages.
    for (const key of ["Content-Type", "Content-Length", "Content-Range", "Content-Encoding", "Accept-Ranges"]) {
      if (response.headers.has(key)) outgoing.set(key, response.headers.get(key));
    }
    outgoing.set("Cache-Control", "no-store");
    outgoing.set("X-Upstream-Mode", "origin");
    outgoing.set("X-Upstream-Landed", new URL(response.url || upstream.href).hostname);
    return new Response(response.body, { status: response.status, headers: outgoing });
  } catch {
    return new Response("Upstream connection failed", { status: 502 });
  }
}

export default {
  async fetch(request, env) {
    const result = await handleSpeed(request, env);
    if (result) return result;
    return new Response("CFSCAN-SNI zone ready", {
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  },
};
