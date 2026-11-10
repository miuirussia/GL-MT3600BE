# OpenWrt for GL-MT3600BE

Build [OpenWrt 25.12.6](https://downloads.openwrt.org/releases/25.12.6/targets/mediatek/filogic/) for `glinet_gl-mt3600be`, `mediatek/filogic`, Linux 6.12.112. Includes LuCI with HTTPS and English and Russian interface languages.

## Build

Requires Docker with Compose. Builds default to four parallel jobs (`JOBS=4`); CPU, RAM, and disk needs depend on the selected packages. The launchers support Linux, macOS, and Windows; builds run in a Linux container.

### Linux and macOS

```sh
./build.sh                       # Build the Docker image and firmware
JOBS=8 ./build.sh                # Default: JOBS=4
```

### Windows

Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with the WSL 2 backend and start it in Linux containers mode. Run from the project directory in Windows PowerShell 5.1 or PowerShell 7:

```powershell
.\build.ps1                       # Build the Docker image and firmware
$env:JOBS = '8'; .\build.ps1       # Default: JOBS=4
```

If PowerShell blocks local scripts, allow them for the current session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\build.ps1
```

Git attributes keep build inputs at LF. Save new shell scripts, patches, and package recipes with LF endings. For an older checkout with CRLF inputs, use a fresh clone and preserve your local edits. In a WSL Linux checkout, use `./build.sh`.

### Commands

Pass these commands to `./build.sh` or `.\build.ps1`:

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

After `menuconfig`, save your selection on the host:

```sh
cp artifacts/menuconfig.config configs/custom.config
```

In PowerShell, use `Copy-Item artifacts/menuconfig.config configs/custom.config`.

### Output and storage

Output goes to `artifacts/25.12.6/`. The firmware image is:

```text
bin/targets/mediatek/filogic/openwrt-25.12.6-mediatek-filogic-glinet_gl-mt3600be-squashfs-sysupgrade.bin
```

The output also includes APK packages, configuration, the lock file, a build manifest, and `SHA256SUMS`. Each successful build replaces the previous output directory. Package symlinks are exported as regular files or directories, so Windows symlink privileges are not required.

Sources, downloads, and ccache stay in the Linux volume `routerfw_work`; signing keys are stored in `.keys/`. With rootless Docker (including Lima), outputs and keys may be staged in the work volume and copied to the host when the command exits, including after a build failure.

## GitHub Actions

[build.yml](.github/workflows/build.yml) builds directly on Ubuntu 24.04 without Docker on every push to `main`. A new push cancels the previous run for that branch. You can also run it manually through Actions or push a `v*` tag:

```sh
git tag v0.1.0-rc1
git push origin v0.1.0-rc1
```

A successful build publishes a **prerelease** containing firmware images, a full build archive, and SHA256SUMS. Builds from `main` update the `prerelease` release, replace its assets, and move the `prerelease` tag to the built commit after publication succeeds. Failed or cancelled builds do not move the tag; outdated runs skip publication if `main` has advanced. Manual runs on other branches use a `ci-<run>-<attempt>` tag. Releases for `v*` and `ci-*` tags are not overwritten.

To reuse the APK signing key across runs, set the `APK_PRIVATE_KEY` repository secret to the contents of `.keys/private-key.pem`. Without this secret, each CI build generates a new key.

Docker and Actions use the same builder, lock file, configuration, and dependency list in `scripts/build-packages.txt`. Actions caches downloads and ccache; installed system package versions are recorded in `builder-packages.txt`.

Actions restores the latest cache for the same inputs, falling back to one with the same lock file and dependency list, then saves a new snapshot after each build attempt, including failed builds. Cancelled runs do not save a cache. Compiler identity is checked by content so rebuilding the toolchain does not invalidate ccache solely because compiler timestamps changed. The first build with this setting warms the compiler cache; later builds can reuse it. The job summary reports cache hits and misses for the current build. Sources, `build_dir`, and `staging_dir` are not cached, so tools and toolchain builds, configuration, and linking still take time.

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
| `apk/*.apk` | Prebuilt third-party OpenWrt packages included in the firmware |
| `files/` | Files copied into the firmware root filesystem |
| `uci-defaults/` | First-boot configuration scripts |
| `patches/openwrt/*.patch` | Patches to the OpenWrt tree |
| `patches/feeds/<feed>/*.patch` | Patches to a feed checkout |
| `patches/kernel/*.patch` | Linux source patches |

Patches are applied in filename order. OpenWrt and feed patches must use `a/...` and `b/...` paths relative to the repository root. Kernel patches are appended to the upstream patch series and applied during `target/linux/prepare`.

Included network workarounds:

- `110-mt76-mt7996-always-apply-txd.patch`: always fills TXWI and sets `MT_CT_INFO_APPLY_TXD` in mt7996 to address slow IPv4 NAT downloads over Wi-Fi ([mt76 #1043](https://github.com/openwrt/mt76/issues/1043#issuecomment-4414905981)).
- `120-gl-mt3600be-disable-broken-eee.patch`: disables 100BASE-TX and 1000BASE-T EEE advertisement on the external RTL8221B PHY to address packet loss and slow Ethernet transmission ([OpenWrt #20967](https://github.com/openwrt/openwrt/issues/20967), [PR #25996](https://github.com/openwrt/openwrt/pull/25996)). Hardware validation on GL-MT3600BE is still needed.

After changing local packages, prebuilt APKs, the `files/` overlay, `uci-defaults/` scripts, patches, or the lock file:

```sh
./build.sh reset-sources
./build.sh
```

Downloads, ccache, signing keys, and exported artifacts are preserved. Save any source edits made inside Docker as patches before resetting.

## Prebuilt APKs

Place OpenWrt `.apk` files in `apk/`. All packages in this directory are included in the firmware without Kconfig entries:

```sh
cp /path/to/example-1.0-r1.apk apk/
./build.sh reset-sources
./build.sh
```

Packages must match OpenWrt 25.12 and `aarch64_cortex-a53` or `noarch`. Add dependencies to `configs/packages.config` or supply their APKs in the same directory. Missing dependencies fail the build.

Use regular files with simple filenames; subdirectories and symlinks are not supported. The builder re-signs copies with its build key and exports them under `artifacts/25.12.6/apk/`.

Run `reset-sources` after changing files in `apk/`.

## Third-party feeds

Add a repository to the `feeds` array in `openwrt.lock.json`:

```json
{
  "name": "extra",
  "url": "https://github.com/OWNER/openwrt-packages.git",
  "commit": "REPLACE_WITH_FULL_40_CHARACTER_COMMIT_SHA"
}
```

Replace the URL and SHA with actual values; the lock file requires full commit SHAs. Use feeds compatible with OpenWrt 25.12 and its APK package manager, then select packages in `configs/packages.config`. Pin `PKG_SOURCE_VERSION` and source archive hashes in local recipes.

## uci-defaults

Create a script such as `uci-defaults/99-local-settings`:

```sh
#!/bin/sh
uci set system.@system[0].hostname='Beryl7' &&
    uci commit system
```

The builder checks it with `sh -n` and installs it in `/etc/uci-defaults/` with mode `0755`. A matching filename in `files/etc/uci-defaults/` causes an error. Keep regular files directly in this directory, without subdirectories or symlinks.

OpenWrt removes each script after it succeeds. Failed scripts may run again, so make them safe to repeat and ensure they return a zero status only after successful configuration.

## Vermagic

`100-vermagic-override.patch` uses the official OpenWrt ABI hash from `profiles.json`: `f0a7348f9cb959e1bf4d0447669b031e`. The kernel version is checked before building; the resulting hash is checked before export.

A matching hash does not guarantee kmod compatibility after changing the kernel configuration or sources. The patch does not disable Linux module loading checks. Build kmods alongside the firmware when modifying the kernel.

To restore the original hash calculation, set `kernel.vermagic_mode` to `"native"` in the lock file and run `reset-sources`.

## Reproducibility

The OpenWrt commit, feed commits, release metadata checksum, and source epoch are pinned. Docker uses `ubuntu:24.04` without an image digest; apt repositories and dependency versions are not pinned. Actions also installs dependencies from Ubuntu repositories. Both build environments can therefore change over time; exported `builder-packages.txt` records the installed package versions but does not pin them. Docker uses `linux/amd64`, emulated on Apple Silicon.

The APK signing key is generated in `.keys/` and reused across builds. Transfer it separately when reproducing a build on another machine; never publish `private-key.pem`. Only the public key is exported. Bit-for-bit equality between two full builds has not been verified.

## Checks

```sh
./build.sh configure
docker compose run --rm --entrypoint python3 builder -m unittest discover -s /inputs/tests
```

On Windows, use `.\build.ps1 configure`, then the same `docker compose run` command.

Test the PowerShell launcher without a Docker daemon:

```powershell
.\tests\test_build_windows.ps1
```

[test-windows.yml](.github/workflows/test-windows.yml) runs launcher checks in Windows PowerShell 5.1 and PowerShell 7.
