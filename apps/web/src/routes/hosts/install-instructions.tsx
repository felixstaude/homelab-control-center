import { AlertTriangle, Check, Copy, Terminal } from 'lucide-react';
import { useState } from 'react';

import type { AgentReleases } from '@/api/types';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { installCommand, platformLabel } from '@/routes/hosts/install-command';

/**
 * The install command for one host, with the digest that makes it worth
 * copying.
 *
 * ## Why this is a separate component from the credential
 *
 * Two things get copied and they have opposite handling rules. The command
 * contains **no secret**: it is safe in shell history, safe in a chat message
 * while asking a colleague to run it, safe in an issue report. The credential
 * is none of those. Two copy buttons with no explanation invites copying the
 * wrong one into the wrong place, so each is labelled with what it is and one
 * line says why they are separate.
 *
 * ## Why the digest is here at all
 *
 * The administrator is already authenticated to the controller over TLS — it
 * is the thing holding their fleet's credentials. So the controller, not the
 * download origin, is what vouches for the binary. The digest travels inside
 * this session, which means an attacker who controls the mirror or the network
 * between the host and the mirror cannot make a substituted tarball match it.
 *
 * `sha256sum -c -` exits non-zero, and because the four lines are separate
 * commands rather than a pipeline, a mismatch stops the paste before the
 * `tar`. That is the entire reason the command is not `curl … | sudo bash`.
 *
 * ## When there is no release
 *
 * No release of this project has been published, so `available` is false on
 * every build so far and this component renders the build-from-source path
 * instead. It does **not** render a plausible-looking download command for a
 * URL that would 404 — a digest beside an invented URL is a claim nobody can
 * check, which is worse than no claim.
 */
export function InstallInstructions({
  releases,
  controllerOrigin,
}: {
  releases: AgentReleases | undefined;
  /** The origin the agent should dial. The browser's own, which is the one
   * the administrator reached this page on and therefore the one that
   * resolves and has a certificate. */
  controllerOrigin: string;
}) {
  const platforms = releases?.platforms ?? [];
  const [selected, setSelected] = useState(platforms[0]?.platform ?? 'linux/amd64');
  const [copied, setCopied] = useState(false);

  if (!releases) {
    return (
      <p className="text-text-muted text-xs leading-relaxed">
        Loading the install instructions&hellip;
      </p>
    );
  }

  if (!releases.available || platforms.length === 0) {
    return <BuildFromSource reason={releases.unavailable_reason} />;
  }

  const artifact = platforms.find((entry) => entry.platform === selected) ?? platforms[0];
  if (!artifact) return <BuildFromSource reason={releases.unavailable_reason} />;
  const command = installCommand(artifact, controllerOrigin);

  const copy = () => {
    // The same defensive shape as the credential's copy button, and for the
    // same reason: `navigator.clipboard` is typed as always present and is
    // genuinely `undefined` over plain HTTP on a non-loopback origin, where
    // reading `.writeText` off it throws synchronously.
    try {
      void navigator.clipboard
        .writeText(command)
        .then(() => {
          setCopied(true);
        })
        .catch(() => {
          setCopied(false);
        });
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="text-text-secondary text-xs font-medium">
            Architecture of the host
          </span>
          <Select
            className="w-56"
            value={selected}
            onChange={(event) => {
              setSelected(event.target.value);
              setCopied(false);
            }}
          >
            {platforms.map((entry) => (
              <option key={entry.platform} value={entry.platform}>
                {platformLabel(entry.platform)}
                {entry.tier === 'best-effort' ? ' — best effort' : ''}
              </option>
            ))}
          </Select>
        </label>
        <div className="flex items-center gap-2">
          <span className="text-text-faint text-xs">agent {releases.recommended_version}</span>
          <Button variant="secondary" size="sm" onClick={copy}>
            {copied ? <Check /> : <Copy />}
            {copied ? 'Copied' : 'Copy command'}
          </Button>
        </div>
      </div>

      {artifact.tier === 'best-effort' ? (
        <Alert variant="warn" title="This architecture is best-effort">
          The <code className="font-mono">{platformLabel(artifact.platform)}</code> build is
          compiled and run under emulation on every change. It has never run on real hardware
          of this architecture.
        </Alert>
      ) : null}

      <pre
        className="bg-surface-overlay border-border-default text-text-secondary
          overflow-x-auto rounded-lg border px-3 py-2.5 font-mono text-xs leading-relaxed"
      >
        {command}
      </pre>

      <p className="text-text-muted flex items-start gap-1.5 text-xs leading-relaxed">
        <Terminal aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
        <span>
          <span className="text-text-primary font-medium">
            This command contains no secret.
          </span>{' '}
          It is safe in shell history and safe to send to a colleague. The second line checks
          the download against a digest this control plane holds, so a substituted file fails
          before anything is extracted. The credential below is the secret, and the installer
          asks for it with the input hidden &mdash; it never reaches a command line, where{' '}
          <code className="font-mono">/proc</code> would make it readable by every local user.
        </span>
      </p>
    </div>
  );
}

/**
 * What to show when there is no published release.
 *
 * The honest answer, and the one every build of this project gives today. It
 * is deliberately not a download command with a placeholder URL: a command
 * that looks real and 404s costs an operator more time than a paragraph
 * telling them to build it.
 */
function BuildFromSource({ reason }: { reason: string | null }) {
  return (
    <div className="flex flex-col gap-3">
      <Alert variant="warn" title="No prebuilt agent is available">
        {reason ??
          'This control plane has no agent release to offer, so there is no verified download.'}
      </Alert>
      <p className="text-text-muted text-xs leading-relaxed">
        Build the agent from the repository on any machine with Go 1.24 or newer &mdash; not
        necessarily the one being monitored &mdash; then copy the binary across and run its
        installer. The binary installs itself, so there is nothing else to fetch.
      </p>
      <pre
        className="bg-surface-overlay border-border-default text-text-secondary
          overflow-x-auto rounded-lg border px-3 py-2.5 font-mono text-xs leading-relaxed"
      >
        {[
          'git clone https://github.com/felixstaude/homelab-control-center',
          'cd homelab-control-center && make agent-build',
          '',
          '# then, on the host to be monitored:',
          'sudo ./hcc-agent install --controller <this control plane>',
        ].join('\n')}
      </pre>
      <p className="text-text-faint flex items-start gap-1.5 text-xs">
        <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
        <span>
          The full procedure, including what has and has not been verified about the hardened
          systemd unit, is <code className="font-mono">agents/linux/deploy/README.md</code> in
          the repository.
        </span>
      </p>
    </div>
  );
}
