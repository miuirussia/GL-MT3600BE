# OpenWrt for GL-MT3600BE

Build [OpenWrt 25.12.6](https://downloads.openwrt.org/releases/25.12.6/targets/mediatek/filogic/) for `glinet_gl-mt3600be`, `mediatek/filogic`, Linux 6.12.112. Includes LuCI with HTTPS and English and Russian interface languages.

## Build

Requires Docker with Compose. Allow 4 CPUs, 8 GiB RAM, and 40 GiB disk space.

With rootless Docker (including Lima), shared host mounts may reject ownership changes. In that case, outputs and signing keys are staged in the Linux work volume and copied back to `artifacts/` and `.keys/` when the command exits, including after a build failure.

```sh
./build.sh                       # Build the Docker image and firmware
JOBS=8 ./build.sh                # Default: JOBS=4
```

Output goes to `artifacts/25.12.6/`. The firmware image is:

```text
bin/targets/mediatek/filogic/openwrt-25.12.6-mediatek-filogic-glinet_gl-mt3600be-squashfs-sysupgrade.bin
```

The output also includes APK packages, configuration, the lock file, a build manifest, and SHA256SUMS. Each successful build replaces the previous output directory.

```sh
./build.sh image                 # Build only the Docker image
./build.sh prepare               # Check out sources and feeds; apply patches
./build.sh configure             # Run and validate defconfig
./build.sh download              # Download package sources
./build.sh menuconfig            # Configure packages interactively
./build.sh shell                 # Open Bash in /work/openwrt
./build.sh make -j1 package/htop/compile V=s
./build.sh reset-sources         # Remove the source tree and compiled files
```

After `menuconfig`, save your selection on the host:

```sh
cp artifacts/menuconfig.config configs/custom.config
```

## GitHub Actions

`.github/workflows/build.yml` builds directly on Ubuntu 24.04 without Docker. Run it manually through Actions or push a `v*` tag:

```sh
git tag v0.1.0-rc1
git push origin v0.1.0-rc1
```

A successful build creates a **prerelease** containing firmware images, a full build archive, and SHA256SUMS. Manual runs use a `ci-<run>-<attempt>` tag. Existing releases are not overwritten.

To reuse the APK signing key across runs, set the `APK_PRIVATE_KEY` repository secret to the contents of `.keys/private-key.pem`. Without this secret, each CI build generates a new key.

Docker and Actions use the same `scripts/builder.py`, lock file, and configuration. Both install dependencies through `scripts/install-deps.sh` using `scripts/build-packages.txt`. Actions caches downloads and ccache. Ubuntu system package versions may change; the installed versions are recorded in `builder-packages.txt`.

## Packages and configuration

Add package selections to `configs/packages.config`:

```config
CONFIG_PACKAGE_htop=y
CONFIG_PACKAGE_curl=y
CONFIG_PACKAGE_tcpdump=m
```

Use `=y` to include a package in the firmware or `=m` to build a separate APK. Put other Kconfig options in `configs/custom.config`; these are applied last. Changes under `configs/` do not require a source reset.

| Directory | Contents |
| --- | --- |
| `packages/<name>/` | Local package: `Makefile`, `files/`, `patches/` |
| `files/` | Files copied into the firmware root filesystem |
| `uci-defaults/` | First-boot configuration scripts |
| `patches/openwrt/*.patch` | Patches to the OpenWrt tree |
| `patches/feeds/<feed>/*.patch` | Patches to a feed checkout |
| `patches/kernel/*.patch` | Linux source patches |

Patches are applied in filename order. OpenWrt and feed patches must use `a/...` and `b/...` paths relative to the repository root. Kernel patches are appended to the upstream patch series and applied during `target/linux/prepare`.

`110-mt76-mt7996-always-apply-txd.patch` installs the [mt76 issue #1043 workaround](https://github.com/openwrt/mt76/issues/1043#issuecomment-4414905981) into the mt76 package patch series. It makes the mt7996 driver always fill TXWI and set `MT_CT_INFO_APPLY_TXD` to address slow IPv4 NAT downloads over Wi-Fi.

`120-gl-mt3600be-disable-broken-eee.patch` applies the GL-MT3600BE Device Tree workaround from [OpenWrt PR #25996](https://github.com/openwrt/openwrt/pull/25996) for [issue #20967](https://github.com/openwrt/openwrt/issues/20967). It disables 100BASE-TX and 1000BASE-T EEE advertisement on the external RTL8221B PHY to avoid packet loss and slow Ethernet transmission on 25.12. This is a board-specific workaround; the PR has not been merged and hardware validation on GL-MT3600BE is still needed.

After changing local packages, files, scripts, patches, or the lock file:

```sh
./build.sh reset-sources
./build.sh
```

Downloads, ccache, signing keys, and exported artifacts are preserved. Save any source edits made inside Docker as patches before resetting.

## Third-party feeds

Add a repository to the `feeds` array in `openwrt.lock.json`:

```json
{
  "name": "extra",
  "url": "https://github.com/OWNER/openwrt-packages.git",
  "commit": "REPLACE_WITH_FULL_40_CHARACTER_COMMIT_SHA"
}
```

Replace the URL and SHA with actual values. Use feeds compatible with OpenWrt 25.12 and its apk package manager. Select their packages in `configs/packages.config`.

Pin `PKG_SOURCE_VERSION` and source archive hashes in local recipes. The lock file requires full commit SHAs rather than branch names.

## uci-defaults

Create a script such as `uci-defaults/99-local-settings`:

```sh
#!/bin/sh
uci set system.@system[0].hostname='Beryl7'
uci commit system
exit 0
```

The builder checks it with `sh -n` and installs it in `/etc/uci-defaults/` with mode `0755`. A matching filename in `files/etc/uci-defaults/` causes an error. Keep regular files directly in this directory, without subdirectories or symlinks.

OpenWrt removes each script after it succeeds. Failed scripts may run again, so make them safe to repeat and return `exit 0` only after successful configuration.

## Vermagic

`100-vermagic-override.patch` uses the official OpenWrt ABI hash from `profiles.json`: `f0a7348f9cb959e1bf4d0447669b031e`. The kernel version is checked before building; the resulting hash is checked before export.

A matching hash does not guarantee kmod compatibility after changing the kernel configuration or sources. The patch does not disable Linux module loading checks. Build kmods alongside the firmware when modifying the kernel.

To restore the original hash calculation, set `kernel.vermagic_mode` to `"native"` in the lock file and run `reset-sources`.

## Reproducibility

The OpenWrt commit, feed commits, release metadata, source epoch, Debian image digest, and apt snapshot are pinned. Docker uses `linux/amd64`, emulated on Apple Silicon. Sources and caches reside in the Linux volume `routerfw_work`.

The APK signing key is generated in `.keys/` and reused across builds. Transfer it separately when reproducing a build on another machine; never publish `private-key.pem`. Only the public key is exported. Bit-for-bit equality between two full builds has not been verified.

Environment checks:

```sh
./build.sh configure
docker compose run --rm --entrypoint python3 builder -m unittest discover -s /inputs/tests
```
