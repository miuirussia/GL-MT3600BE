# OpenWrt for GL-MT3600BE

[OpenWrt 25.12.6](https://downloads.openwrt.org/releases/25.12.6/targets/mediatek/filogic/) for `glinet_gl-mt3600be` (`mediatek/filogic`), Linux 6.12.112. Includes LuCI with HTTPS, English and Russian, the Outline theme, Forkop, fan control, temperature sensors, and zram swap.

On first boot: hostname `Beryl7`, LAN `192.168.69.1/24`, LuCI at **https://192.168.69.1**. Change these defaults in [uci-defaults/99-local-settings](uci-defaults/99-local-settings).

## Build

Requires Docker with Compose. Builds run in a Linux container with four parallel jobs by default.

Linux and macOS:

```sh
./build.sh
JOBS=8 ./build.sh                # Override the default JOBS=4
```

Windows: start Docker Desktop with the WSL 2 backend and Linux containers, then run in PowerShell:

```powershell
.\build.ps1
$env:JOBS = '8'; .\build.ps1
```

If PowerShell blocks scripts, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`. In WSL, use `./build.sh`. Keep shell scripts, patches, and package recipes at LF; `.gitattributes` handles this for new checkouts.

Both launchers accept the same commands:

| Command | Action |
| --- | --- |
| `image` | Build only the Docker image |
| `prepare` | Check out sources and feeds; apply patches |
| `configure` | Run and validate defconfig |
| `download` | Download package sources |
| `menuconfig` | Configure packages interactively |
| `shell` | Open Bash in `/work/openwrt` |
| `make -j1 package/htop/compile V=s` | Build a single package |
| `reset-sources` | Remove the source tree and compiled files |

For example, `./build.sh menuconfig`. To keep the selection:

```sh
cp artifacts/menuconfig.config configs/custom/menuconfig.config
```

In PowerShell: `Copy-Item artifacts/menuconfig.config configs/custom/menuconfig.config`.

Output: `artifacts/25.12.6/`, replaced after each successful build. The sysupgrade image is under:

```text
bin/targets/mediatek/filogic/openwrt-25.12.6-mediatek-filogic-glinet_gl-mt3600be-squashfs-sysupgrade.bin
```

The output also contains APKs, configuration, build metadata, and `SHA256SUMS`. Sources, downloads, and ccache stay in the Docker volume `routerfw_work`; signing keys stay in `.keys/`. Docker uses `linux/amd64`, emulated on Apple Silicon.

## Configuration

| Path | Contents |
| --- | --- |
| `configs/device/*.config` | Target, image, build settings, and OpenWrt kernel options |
| `configs/packages/*.config` | Package selections and dependencies of prebuilt APKs |
| `configs/custom/*.config` | Local overrides and saved menuconfig selections |
| `configs/kernel/*.config` | Linux kernel overrides, merged separately |

OpenWrt fragments load in order: `device`, `packages`, `custom`; files within each section load alphabetically. The last value wins. Configuration changes do not require `reset-sources`.

Add packages to `configs/packages/*.config`:

```config
CONFIG_PACKAGE_htop=y
CONFIG_PACKAGE_curl=y
CONFIG_PACKAGE_tcpdump=m
```

`=y` includes a package in the firmware; `=m` builds a separate APK. Use `configs/custom/*.config` for other OpenWrt options.

Linux overrides in `configs/kernel/` use `CONFIG_*` without the OpenWrt `KERNEL_` prefix. They enable conntrack events and labels, netfilter egress, traffic-control actions, XFRM, TLS offload, FS-Cache, and `/proc/config.gz`, and set 16-port UART limits. They merge after target defaults, remain subject to Kconfig dependencies, and are exported as `kernel.config`.

## Packages and patches

| Path | Contents |
| --- | --- |
| `packages/<name>/` | Local package recipes, files, and patches |
| `apk/` | Prebuilt APKs or download descriptors |
| `files/` | Root filesystem overlay |
| `uci-defaults/` | First-boot shell scripts |
| `patches/openwrt/*.patch` | OpenWrt patches |
| `patches/feeds/<feed>/*.patch` | Feed patches |
| `patches/kernel/*.patch` | Linux source patches |

After changing any of these or `openwrt.lock.json`, reset and rebuild:

```sh
./build.sh reset-sources
./build.sh
```

This preserves downloads, ccache, keys, and exported artifacts. Save source edits made inside Docker as patches before resetting.

Patches apply alphabetically. OpenWrt and feed patches use `a/...` and `b/...` paths relative to the respective repository root. Kernel patches follow the upstream series during `target/linux/prepare`.

Two network workarounds are included:

- [110-mt76-mt7996-always-apply-txd.patch](patches/openwrt/110-mt76-mt7996-always-apply-txd.patch) addresses slow IPv4 NAT downloads over Wi-Fi ([mt76 #1043](https://github.com/openwrt/mt76/issues/1043#issuecomment-4414905981)).
- [120-gl-mt3600be-disable-broken-eee.patch](patches/openwrt/120-gl-mt3600be-disable-broken-eee.patch) disables 100/1000BASE-T EEE advertisement on the external RTL8221B PHY to address packet loss and slow Ethernet transmission ([OpenWrt #20967](https://github.com/openwrt/openwrt/issues/20967), [PR #25996](https://github.com/openwrt/openwrt/pull/25996)). Hardware validation on GL-MT3600BE is still needed.

### Prebuilt APKs

| Package | Version | LuCI location |
| --- | --- | --- |
| [Outline](https://github.com/tickcount/luci-theme-outline/releases/tag/v1.4.1) | 1.4.1 | System → System → Language and Style |
| [Forkop](https://github.com/miuirussia/forkop/releases/tag/2.0.19), LuCI app and Russian translation | 2.0.19 | Services → Forkop |
| [sing-box-forkop](https://github.com/miuirussia/sing-box-forkop/releases/tag/v1.14.4-r5) | 1.14.4-r5, `base` | — |
| [luci-app-fancontrol](https://github.com/zkcaryq/luci-app-fancontrol) | 1.1.0-r2 | System → Fan Control |
| [luci-app-temp-status](https://github.com/gSpotx2f/luci-app-temp-status), Russian translation | 0.8.1-r1 | Status page |

Outline is enabled by default; Bootstrap remains available. Fan control defaults to system control. Runtime dependencies are selected in `configs/packages/{outline,forkop,system}.config`; `system.config` also enables `zram-swap`.

To add a package, place its `.apk` in `apk/`, or create `apk/example.apk.txt` with a download URL and SHA-256 on separate lines:

```text
https://example.com/example.apk
SKIP
```

Replace `SKIP` with the checksum to verify the download. The builder downloads this as `example.apk`; a checksum mismatch or an existing file with that name stops the build.

All APKs in this directory enter the firmware without Kconfig entries. They must match OpenWrt 25.12 and `aarch64_cortex-a53` or `noarch`. Select dependencies in `configs/packages/` or supply their APKs; missing dependencies fail the build. Use regular files directly in `apk/`, with simple filenames. Copies are re-signed with the build key and exported under `artifacts/25.12.6/apk/`.

To update a package, change its descriptor filename, URL, and checksum, then reset and rebuild. Included descriptors pin SHA-256 checksums. Fancontrol uses a `continuous` release asset, which upstream may replace or remove; temp-status URLs are pinned to a commit.

### Third-party feeds

Add an entry to the `feeds` array in `openwrt.lock.json`:

```json
{
  "name": "extra",
  "url": "https://github.com/OWNER/openwrt-packages.git",
  "commit": "REPLACE_WITH_FULL_40_CHARACTER_COMMIT_SHA"
}
```

Use feeds compatible with OpenWrt 25.12 and APK, then select their packages in `configs/packages/`. Local recipes should pin `PKG_SOURCE_VERSION` and source archive hashes.

### First-boot scripts

Place shell scripts directly in `uci-defaults/`. The builder checks them with `sh -n` and installs them in `/etc/uci-defaults/` with mode `0755`. Subdirectories, symlinks, and filenames duplicated in `files/etc/uci-defaults/` are rejected.

OpenWrt removes scripts after they succeed and retries failed scripts. Make them safe to repeat and return zero only after configuration succeeds.

## Official kmods

`100-vermagic-override.patch` forces the official ABI hash from `profiles.json`: `f0a7348f9cb959e1bf4d0447669b031e`. The builder checks the kernel version before building and the hash before export. The firmware includes the official APK kmod repository for this kernel.

`configs/device/kernel.config` disables `CONFIG_KERNEL_KALLSYMS` to match the official release. Enabling it changes `struct module` and causes `.gnu.linkonce.this_module section size must match` errors with official kmods, even with the forced hash. Existing firmware needs rebuilding and flashing to apply this setting.

The hash alone does not guarantee compatibility after kernel changes; Linux module loading checks remain active. Build kmods with the firmware when modifying the kernel. To restore native hash calculation, set `kernel.vermagic_mode` to `"native"` in the lock file, then reset and rebuild.

## GitHub Actions

[build.yml](.github/workflows/build.yml) builds on Ubuntu 24.04 on pushes to `main`, `v*` tags, or manual runs. It uses the same builder and inputs as Docker.

Successful builds publish firmware, a full build archive, and checksums as a prerelease:

- `main` updates the rolling `prerelease` release and tag. Failed, cancelled, or outdated builds do not move the tag.
- `v*` tags create separate releases.
- Manual runs on other branches use `ci-<run>-<attempt>` tags.

Set the repository secret `APK_PRIVATE_KEY` to the contents of `.keys/private-key.pem` to reuse the signing key. Without it, each CI build generates a new key.

Actions caches downloads and ccache, including after failed builds. It rebuilds the source tree and toolchain each run; cache statistics appear in the job summary.

## Build reproducibility

`openwrt.lock.json` pins OpenWrt and feed commits, release metadata, and the source epoch. The Ubuntu image and apt packages are not pinned; installed versions are recorded in `builder-packages.txt`. Bit-for-bit reproducibility has not been verified.

Local builds reuse the signing key in `.keys/`. Copy it separately when building on another machine; never publish `private-key.pem`. Only the public key is exported.

## Checks

```sh
./build.sh configure
docker compose run --rm --entrypoint python3 builder -m unittest discover -s /inputs/tests
```

On Windows, use `.\build.ps1 configure`. Launcher checks run without Docker:

```powershell
.\tests\test_build_windows.ps1
```

[test-windows.yml](.github/workflows/test-windows.yml) runs these checks in Windows PowerShell 5.1 and PowerShell 7.
