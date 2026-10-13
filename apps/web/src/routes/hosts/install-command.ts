/**
 * The install command, as text.
 *
 * In its own module rather than beside the component that renders it, for two
 * reasons. The dull one is that a file mixing components and helpers defeats
 * fast refresh. The useful one is that the properties worth testing here are
 * properties of a *string* — that it carries the digest, that it carries no
 * credential, and that nothing is piped into a shell — and a test that had to
 * render a dialog to assert them would be testing the wrong thing.
 */

import type { PlatformArtifact } from '@/api/types';

/**
 * Render the four-line install command for one architecture.
 *
 * Four separate commands, deliberately not a pipeline. `sha256sum -c -` exits
 * non-zero, and because these are separate commands a mismatch stops the
 * paste before the `tar` — which is the whole reason this is not
 * `curl … | sudo bash`.
 *
 * The digest comes from the controller, which the administrator is already
 * authenticated to over TLS. An attacker who controls the download origin, or
 * the network between the host and it, cannot make a substituted tarball
 * match a digest that arrived inside the administrator's own session.
 *
 * No credential appears here and none may be added. The installer prompts for
 * it with the input hidden; a command line is readable by every local user
 * through `/proc/<pid>/cmdline`, and would also land in shell history.
 */
export function installCommand(artifact: PlatformArtifact, controllerOrigin: string): string {
  return [
    `curl -fsSLO ${artifact.url}`,
    `echo '${artifact.sha256}  ${artifact.archive}' | sha256sum -c -`,
    `tar xzf ${artifact.archive}`,
    `sudo ./hcc-agent install --controller ${controllerOrigin}`,
  ].join('\n');
}

/**
 * `linux/amd64` → the name plus the hardware people recognise.
 *
 * The Go architecture string is what the artifact is named after and is the
 * wrong thing to put in front of somebody choosing between a NAS and a
 * Raspberry Pi.
 */
export function platformLabel(platform: string): string {
  switch (platform) {
    case 'linux/amd64':
      return 'linux/amd64 (x86-64)';
    case 'linux/arm64':
      return 'linux/arm64 (Raspberry Pi 3+, Ampere)';
    case 'linux/arm':
      return 'linux/arm (Raspberry Pi 1, Zero)';
    default:
      return platform;
  }
}
