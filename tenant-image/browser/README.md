# Private founder browser parity

Camofox matches the founder service: `@askjo/camofox-browser` 1.11.2,
`camoufox-js` 0.11.1 and Playwright 1.61.0 from its complete npm lockfile;
Firefox/Camoufox 135.0.1 beta.24. Package and browser archives are checked by
SHA256 in the Dockerfile. Browser package code is fetched from the public npm
registry; no founder files, profiles or keys enter the build context.
The verified browser archive contains `fontconfigs/`; the image adds the
`fontconfig` alias expected by camoufox-js 0.11.1 and the Camofox external-bundle
adapter, retaining the archive's OS-specific fonts configuration unchanged.

The image build starts the actual server as `hermes`, launches the actual browser,
loads a local synthetic page, and verifies its snapshot. This is an availability
gate, not a static import check. Linux/amd64 is required, matching the tenant image.

Each private runtime has its own supervised server at **127.0.0.1:9377**.
Group runtimes exit before reading any browser state or starting a process.
The source service's blanket `pkill`/X-display deletion is not copied. The server's
public-listener default is replaced by a source-checked loopback bind. Crash
reporting/Sentry and the external VNC viewer are disabled; no public port/tunnel
is created. Existing Hermes backend selection is preserved, not forcibly switched.

Browser profile cookies, localStorage, cookie imports, checkpoints and private
files live only in an owner-only tmpfs directory. Every five seconds and at orderly
shutdown they are AES-GCM sealed with the existing tenant DEK, bound to runtime ID,
into `$HERMES_HOME/.squire/browser-state.enc`. Restic backs up that ciphertext.
An abrupt kill can lose the last five seconds of profile changes. Runtime root
can still read memory/keys, as with all existing tenant credentials. Browser trace
files are ephemeral and are not included in the durable snapshot.

Migration input must be copied read-only from the founder's `.camofox` directory;
`BrowserState.import_from(path)` checks symlinks, imports only named state
subdirectories, and seals the target without modifying the source. Import with
the destination runtime's DEK and exact runtime ID. Do not place unsealed browser
state onto the destination volume. Source profile IDs and explicit Hermes user-ID
configuration must be preserved; changing the profile path can change Hermes'
automatically derived user ID. Test an existing authenticated session before cutover.

Container resource limits are the outer isolation boundary. The port preserves
owned browser process cleanup, admission under cgroup memory pressure, one browser
lease, deadlines and circuit breakers. It deliberately does not claim the source
VPS's systemd per-workload cgroups exist inside Railway. General terminal/kernel/LSP
workloads share their tenant container and still need appropriate Railway CPU/RAM
limits; this is not equivalent to separate kernel-enforced per-workload memory caps.
