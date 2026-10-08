// ADR-0031: the console adopts the token from the printed link's fragment, removes it from
// the address bar, and sends it on every API call.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("the API token", () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("is taken from the fragment, stripped from the URL and sent as a bearer token", async () => {
    window.history.replaceState(null, "", "/runs#token=abc123");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { api } = await import("../lib/api");
    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe("/runs");
    await api.runs();
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer abc123");
  });

  it("is kept across a reload: adopted from the link once, then sent without it", async () => {
    window.history.replaceState(null, "", "/#token=from-the-link");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    await import("../lib/api"); // the first page load adopts it
    vi.resetModules();
    window.history.replaceState(null, "", "/runs"); // a reload, with no fragment
    const fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { api } = await import("../lib/api");
    await api.runs();
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer from-the-link");
  });

  it("uses a stored token when there is no fragment", async () => {
    localStorage.setItem("opseval.token", "kept");
    window.history.replaceState(null, "", "/");
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { api } = await import("../lib/api");
    await api.health();
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer kept");
  });

  it("turns a server error into a typed error with its machine code", async () => {
    window.history.replaceState(null, "", "/");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "UNAUTHORIZED", message: "no" }), { status: 401 })));
    const { api, ApiError } = await import("../lib/api");
    await expect(api.runs()).rejects.toBeInstanceOf(ApiError);
    await expect(api.runs()).rejects.toMatchObject({ status: 401, code: "UNAUTHORIZED" });
  });
});
