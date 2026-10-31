// A run a CI job reported (ADR-0058 E2): marked as such, linked to its job, and never through a
// link that is not http(s).
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CiOrigin, jobLink } from "../components/CiOrigin";
import type { RunSummary } from "../lib/types";

const ci = (runUrl: string | null): Pick<RunSummary, "origin" | "ci"> => ({
  origin: "ci",
  ci: { provider: "github", repository: "acme/bot", ref: "refs/heads/main", sha: "a".repeat(40), runUrl, label: "production" },
});

describe("the CI origin", () => {
  it("is drawn only for a CI run", () => {
    const { container } = render(<CiOrigin run={{ origin: null, ci: null }} />);
    expect(container.innerHTML).toBe("");
  });

  it("links to the job with rel=noopener in a new tab", () => {
    const { container } = render(<CiOrigin run={ci("https://github.com/acme/bot/actions/runs/7")} detail />);
    const a = container.querySelector("a");
    expect(a?.getAttribute("href")).toBe("https://github.com/acme/bot/actions/runs/7");
    expect(a?.getAttribute("rel")).toContain("noopener");
    expect(a?.getAttribute("target")).toBe("_blank");
    expect(container.textContent).toContain("GitHub Actions");
    expect(container.textContent).toContain("label production");
    expect(container.querySelector("[data-origin='ci']")).not.toBeNull();
  });

  it("never links anything but http(s) without user-info", () => {
    for (const bad of ["javascript:alert(1)", "data:text/html,x", "https://u:p@x.test/", "not a url", "", null]) {
      expect(jobLink(bad)).toBeNull();
      const { container, unmount } = render(<CiOrigin run={ci(bad)} />);
      expect(container.querySelector("a")).toBeNull();
      expect(container.textContent).toContain("CI");  // still marked as a CI run
      unmount();
    }
    expect(jobLink("http://ci.example.com/job/1")).toBe("http://ci.example.com/job/1");
  });
});
