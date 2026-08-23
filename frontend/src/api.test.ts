import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, resetApiSessionForTests } from "./api";

function response(body: unknown, status = 200) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const user = {
  id: "user-1",
  email: "owner@example.com",
  first_name: "Maya",
  last_name: "Haddad",
  email_verified_at: "2026-08-22T10:00:00Z",
  status: "ACTIVE",
};

describe("authentication session handling", () => {
  beforeEach(() => resetApiSessionForTests());
  afterEach(() => vi.unstubAllGlobals());

  it("uses the login access token to load the user without refreshing", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({ access_token: "login-access" }))
      .mockResolvedValueOnce(response(user));
    vi.stubGlobal("fetch", fetchMock);

    await api.login({ email: user.email, password: "secret", keep_me_signed_in: true });
    await expect(api.me()).resolves.toEqual(user);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/v1/auth/login");
    expect(String(fetchMock.mock.calls[1][0])).toBe("/api/v1/auth/me");
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("Authorization")).toBe(
      "Bearer login-access",
    );
  });

  it("restores a reload session from the refresh cookie and then loads the user", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({ access_token: "restored-access" }))
      .mockResolvedValueOnce(response(user));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.restoreSession()).resolves.toEqual(user);

    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/v1/auth/refresh");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      credentials: "include",
    });
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("Authorization")).toBe(
      "Bearer restored-access",
    );
  });

  it("deduplicates concurrent rotating refresh requests", async () => {
    let releaseRefresh: ((value: Response) => void) | undefined;
    const pendingRefresh = new Promise<Response>((resolve) => {
      releaseRefresh = resolve;
    });
    const fetchMock = vi.fn((input: string | URL | Request) => {
      if (String(input).endsWith("/auth/refresh")) return pendingRefresh;
      return Promise.resolve(response(user));
    });
    vi.stubGlobal("fetch", fetchMock);

    const first = api.restoreSession();
    const second = api.restoreSession();
    releaseRefresh?.(response({ access_token: "shared-access" }));
    await expect(Promise.all([first, second])).resolves.toEqual([user, user]);

    expect(
      fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/auth/refresh")),
    ).toHaveLength(1);
  });

  it("fails restoration once when the refresh cookie is invalid", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(response({ error: { code: "refresh_token_invalid" } }, 401));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      Promise.allSettled([api.restoreSession(), api.restoreSession()]),
    ).resolves.toEqual([
      expect.objectContaining({ status: "rejected" }),
      expect.objectContaining({ status: "rejected" }),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("logs out the backend refresh session and clears the in-memory access token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({ access_token: "login-access" }))
      .mockResolvedValueOnce(response({ message: "Signed out successfully." }))
      .mockResolvedValueOnce(response({ error: { code: "not_authenticated" } }, 401))
      .mockResolvedValueOnce(
        response({ error: { code: "refresh_token_invalid" } }, 401),
      );
    vi.stubGlobal("fetch", fetchMock);

    await api.login({
      email: user.email,
      password: "secret",
      keep_me_signed_in: false,
    });
    await api.logout();
    await expect(api.me()).rejects.toMatchObject({ status: 401 });

    expect(String(fetchMock.mock.calls[1][0])).toBe("/api/v1/auth/logout");
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "POST",
      credentials: "include",
    });
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).has("Authorization")).toBe(
      false,
    );
  });
});
