import { useState, type FormEvent } from "react";
import { KeyRound } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { KeyCreated, KeyScope } from "../lib/types";
import { Button, Callout, Card, CoralButton, Data, Explainer, Mono, Page, PageHeader, Pill, Table } from "../components/ui";

/** The scopes a key can be given (ADR-0050). Whether this admin may grant one, and whether a
 * system key may hold it, is the server's answer when the key is created. */
const SCOPES: { scope: KeyScope; help: string }[] = [
  { scope: "runs:read", help: "read runs, results and the catalogue" },
  { scope: "runs:start", help: "start and cancel runs" },
  { scope: "catalogue:write", help: "push catalogue versions, edit scenarios and suites" },
  { scope: "catalogue:label", help: "move unprotected labels" },
  { scope: "label:protected", help: "move protected labels such as production (off by default)" },
];

/** API keys of the served project: admins and owners only (the server refuses anyone else). */
export default function Keys() {
  const keys = useApi(api.keys);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<KeyScope[]>(["runs:read"]);
  const [system, setSystem] = useState(false);
  const [created, setCreated] = useState<KeyCreated>();
  const [error, setError] = useState("");

  const toggle = (s: KeyScope) => setScopes((xs) => (xs.includes(s) ? xs.filter((x) => x !== s) : [...xs, s]));
  const create = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      setCreated(await api.createKey({ name, scopes, system }));
      setName("");
      keys.reload();
    } catch (err) {
      setError((err as ApiError).message);
    }
  };
  const revoke = async (publicKey: string) => {
    try { await api.revokeKey(publicKey); keys.reload(); } catch (err) { setError((err as ApiError).message); }
  };

  return (
    <Page>
      <PageHeader title="API keys" sub="Keys for CI and scripts. A key never does more than the role of the person who made it, checked at each use." />
      <Explainer>Send a key as <Mono>Authorization: Bearer pk-oe-…:sk-oe-…</Mono>. The secret is shown once and stored only as a hash. A system key, for Opseval's own automation, can never move a protected label.</Explainer>
      {created && (
        <Callout tone="info">
          <b>Copy the secret now: it is not shown again.</b>
          <pre className="mt-2 overflow-x-auto rounded-xl bg-ink p-3 font-mono text-sm text-ink-text" data-testid="new-key">{created.authorization}</pre>
        </Callout>
      )}
      {error && <Callout>{error}</Callout>}
      <Card title="New key">
        <form onSubmit={(e) => void create(e)} className="space-y-4" aria-label="New key">
          <label className="block text-sm font-semibold">Name
            <input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} className="mt-1 w-full rounded-xl border border-line px-3 py-2 font-normal" />
          </label>
          <fieldset className="space-y-1">
            <legend className="text-sm font-semibold">Scopes</legend>
            {SCOPES.map(({ scope, help }) => (
              <label key={scope} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={scopes.includes(scope)} onChange={() => toggle(scope)} />
                <Mono>{scope}</Mono><span className="text-muted-ink">{help}</span>
              </label>
            ))}
          </fieldset>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={system} onChange={(e) => setSystem(e.target.checked)} />System key (Opseval's own automation)</label>
          <CoralButton type="submit" disabled={!name || scopes.length === 0}><KeyRound size={16} />Create key</CoralButton>
        </form>
      </Card>
      <Data res={keys}>
        {(rows) => (
          <Table head={["Name", "Key", "Scopes", "Created by", "Last used", ""]}>
            {rows.map((k) => (
              <tr key={k.publicKey} className="border-t border-line">
                <td className="px-4 py-3">{k.name} {k.system && <Pill>system</Pill>}</td>
                <td className="px-4 py-3"><Mono>{k.publicKey}</Mono> <span className="text-muted-ink"><Mono>{k.secretPrefix}…</Mono></span></td>
                <td className="px-4 py-3">{k.scopes.join(", ")}{k.tasks && <div className="text-xs text-muted-ink">tasks: {k.tasks.join(", ")}</div>}</td>
                <td className="px-4 py-3">{k.createdBy ?? <Pill tone="mute">nobody</Pill>}</td>
                <td className="px-4 py-3">{k.lastUsedAt ?? <Pill tone="mute" dashed>never</Pill>}</td>
                <td className="px-4 py-3">{k.revokedAt ? <Pill tone="fail">revoked</Pill> : <Button onClick={() => void revoke(k.publicKey)}>Revoke</Button>}</td>
              </tr>
            ))}
          </Table>
        )}
      </Data>
    </Page>
  );
}
