// ADR-0050: in accounts mode the console holds a session cookie, sends the session's CSRF
// token on writes and never the shared token; it shows and hides actions by the capabilities
// the server returns, and works out nothing from a role.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "../lib/types";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

const accounts = (over: Partial<Session> = {}): Session => ({
  mode: "accounts",
  user: { kind: "user", name: "ada@example.com", role: "viewer" },
  capabilities: ["runs:read"],
  hasAccess: true,
  csrfToken: "csrf-123",
  project: "acme/bot",
  task: "default",
  oidc: false,
  ...over,
});

type Call = [string, RequestInit];
const headersOf = (call: Call) => (call[1]?.headers ?? {}) as Record<string, string>;

describe("the session in the API client", () => {
  beforeEach(() => { vi.resetModules(); localStorage.clear(); window.history.replaceState(null, "", "/"); });
  afterEach(() => vi.unstubAllGlobals());

  it("in accounts mode sends the CSRF token on writes and never a stale shared token", async () => {
    localStorage.setItem("opseval.token", "stale-token-from-token-mode");
    const fetchMock = vi.fn(async (url: string) => (url.endsWith("/auth/session") ? json(accounts()) : json([])));
    vi.stubGlobal("fetch", fetchMock);
    const { api, loadSession } = await import("../lib/api");
    const session = await loadSession();
    expect(session.mode).toBe("accounts");
    expect(headersOf(fetchMock.mock.calls[0] as unknown as Call).authorization).toBeUndefined();
    await api.runs();
    await api.cancelRun("r1");
    const [read, write] = fetchMock.mock.calls.slice(1) as unknown as Call[];
    expect(headersOf(read).authorization).toBeUndefined();
    expect(headersOf(read)["x-csrf-token"]).toBeUndefined(); // a read carries no token
    expect(headersOf(write).authorization).toBeUndefined();
    expect(headersOf(write)["x-csrf-token"]).toBe("csrf-123");
  });

  it("in token mode retries the session with the token, then sends it as before", async () => {
    localStorage.setItem("opseval.token", "kept");
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const auth = ((init?.headers ?? {}) as Record<string, string>).authorization;
      if (url.endsWith("/auth/session")) {
        return auth ? json({ ...accounts(), mode: "token", csrfToken: null, user: { kind: "operator", name: "operator", role: null } }) : json({ code: "UNAUTHORIZED" }, 401);
      }
      return json([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { api, loadSession } = await import("../lib/api");
    expect((await loadSession()).mode).toBe("token");
    await api.cancelRun("r1");
    const last = fetchMock.mock.calls.at(-1) as unknown as Call;
    expect(headersOf(last).authorization).toBe("Bearer kept");
    expect(headersOf(last)["x-csrf-token"]).toBeUndefined();
  });

  it("signing in adopts the new session's CSRF token", async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.endsWith("/auth/session") ? json(accounts({ user: null, csrfToken: null, capabilities: [], hasAccess: false }))
        : url.endsWith("/auth/login") ? json(accounts({ csrfToken: "fresh" })) : json([]));
    vi.stubGlobal("fetch", fetchMock);
    const { api, loadSession } = await import("../lib/api");
    await loadSession();
    await api.login("ada@example.com", "a long password");
    await api.cancelRun("r1");
    expect(headersOf(fetchMock.mock.calls.at(-1) as unknown as Call)["x-csrf-token"]).toBe("fresh");
  });
});

describe("the console by capability", () => {
  beforeEach(() => { vi.resetModules(); });
  afterEach(() => vi.unstubAllGlobals());

  async function shell(session: Session) {
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: "ok", version: "0", profile: "p" })));
    const { default: Shell } = await import("../components/Shell");
    const { SessionFixture } = await import("../lib/session");
    return render(
      <MemoryRouter><SessionFixture session={session}><Shell /></SessionFixture></MemoryRouter>,
    );
  }

  it("a viewer is offered no write and no admin page", async () => {
    await shell(accounts());
    expect(screen.queryByText("New run")).toBeNull();
    expect(screen.queryByText("API keys")).toBeNull();
    expect(screen.queryByText("Bulk import")).toBeNull();
    expect(screen.getByTestId("who").textContent).toContain("ada@example.com · viewer");
  });

  it("an admin is offered what the server granted", async () => {
    await shell(accounts({
      user: { kind: "user", name: "boss@example.com", role: "admin" },
      capabilities: ["runs:read", "runs:start", "catalogue:write", "catalogue:label", "label:protected", "manage"],
    }));
    expect(screen.getAllByText("New run").length).toBeGreaterThan(0);
    expect(screen.getByText("API keys")).toBeTruthy();
  });

  it("someone with no role in the project is told so, not shown empty pages", async () => {
    await shell(accounts({ hasAccess: false, capabilities: [] }));
    expect(screen.getByText(/No access to acme\/bot/)).toBeTruthy();
  });

  it("the cancel button is not offered without runs:start", async () => {
    const { CancelRun } = await import("../components/RunControl");
    const { SessionFixture } = await import("../lib/session");
    const { container } = render(<SessionFixture session={accounts()}><CancelRun id="r1" requested={false} /></SessionFixture>);
    expect(container.textContent).toBe("");
  });
});

describe("the sign-in page", () => {
  beforeEach(() => { vi.resetModules(); window.history.replaceState(null, "", "/"); });
  afterEach(() => vi.unstubAllGlobals());

  async function page(path: string, session: Session, fetchMock = vi.fn(async () => json({}))) {
    vi.stubGlobal("fetch", fetchMock);
    const { default: Login } = await import("../pages/Login");
    const { SessionFixture } = await import("../lib/session");
    render(<MemoryRouter initialEntries={[path]}><SessionFixture session={session}><Login /></SessionFixture></MemoryRouter>);
    return fetchMock;
  }

  const signedOut = accounts({ user: null, csrfToken: null, capabilities: [], hasAccess: false });

  it("says why an OIDC sign-in was refused", async () => {
    await page("/login?error=NO_ACCOUNT", { ...signedOut, oidc: true });
    expect(screen.getByText(/No Opseval account is linked/)).toBeTruthy();
    expect(screen.getByText("Sign in with single sign-on")).toBeTruthy();
  });

  it("names a rate limit rather than a wrong password", async () => {
    const fetchMock = vi.fn(async () => json({ code: "TOO_MANY_ATTEMPTS", message: "too many" }, 429));
    await page("/login", signedOut, fetchMock);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long password" } });
    fireEvent.submit(screen.getByRole("form", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByText(/Too many failed sign-ins/)).toBeTruthy());
    const [url, init] = fetchMock.mock.calls[0] as unknown as Call;
    expect(url).toBe("/api/v1/auth/login");
    expect(JSON.parse(String(init.body))).toEqual({ email: "ada@example.com", password: "a long password" });
  });
});
