// A run a CI job reported to the server (ADR-0058 E2): a badge, what the CI system said, and a
// link back to the job. The server keeps only http(s) links; the console checks again, so a
// stored `javascript:` URL can never become a link.
import type { RunSummary } from "../lib/types";
import { Pill } from "./ui";

const PROVIDER: Record<string, string> = {
  github: "GitHub Actions",
  gitlab: "GitLab CI",
  jenkins: "Jenkins",
  azure: "Azure Pipelines",
  circleci: "CircleCI",
  bitbucket: "Bitbucket Pipelines",
};

/** The link when it is an absolute http(s) URL without user-info; otherwise null. */
export function jobLink(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    if ((u.protocol !== "https:" && u.protocol !== "http:") || u.username || u.password) return null;
    return u.href;
  } catch {
    return null;
  }
}

export function CiOrigin({ run, detail = false }: { run: Pick<RunSummary, "origin" | "ci">; detail?: boolean }) {
  if (run.origin !== "ci" || !run.ci) return null;
  const ci = run.ci;
  const link = jobLink(ci.runUrl);
  const said = [PROVIDER[ci.provider] ?? ci.provider, ci.repository, ci.ref, ci.sha?.slice(0, 12), ci.label && `label ${ci.label}`]
    .filter(Boolean)
    .join(" · ");
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" title={said} data-origin="ci">
      <Pill tone="neutral">CI</Pill>
      {detail && <span className="text-xs">{said}</span>}
      {link && (
        <a href={link} target="_blank" rel="noopener noreferrer" className="text-xs underline">
          {detail ? "open the CI job" : "job"}
        </a>
      )}
    </span>
  );
}
