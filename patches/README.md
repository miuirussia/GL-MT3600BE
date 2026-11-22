# GL-MT3600BE network backport

Source: [plcharriere/gl-mt3600be-openwrt at 92b668321f968b3bfe3b6a1de23317fe0f3f52ad](https://github.com/plcharriere/gl-mt3600be-openwrt/tree/92b668321f968b3bfe3b6a1de23317fe0f3f52ad). The comparison starts at its upstream parent `89e69d9` before the GL-MT3600BE changes. The full base commit, source filenames, original patch SHA-256 checksums, and disposition of all 115 patches are recorded in [plcharriere.json](plcharriere.json).

106 missing patches are backported in the bundles below. Nine fixes are already covered by the locked sources. OpenWrt, kernel, feed, and package source versions remain pinned as before: OpenWrt 25.12.6, Linux 6.12.112, backports 6.18.55, and mt76 `39c960c3ada558b4c2e7915772483d3731573d09`. Original authors and sign-offs are retained in each bundle; individual source patches remain accessible through the pinned source tree and inventory.

| Bundle | Changes |
| --- | --- |
| [kernel/100-plcharriere-mt7987-network.patch](kernel/100-plcharriere-mt7987-network.patch) | Ethernet XGMAC/MDC/MDIO, PHY firmware serialization, WED 3.1, RSS, PPE accounting/cache/QDMA/DSCP/ECN, station flow flushing, bridge moves, and driver teardown |
| [openwrt/150-plcharriere-mt76.patch](openwrt/150-plcharriere-mt76.patch) | MT7990 WED rings/tokens, DMA masks, firmware rate reports, recovery/DFS/PN handling, power save, per-radio settings, and station lifetime fixes |
| [openwrt/160-plcharriere-mac80211.patch](openwrt/160-plcharriere-mac80211.patch) | Per-radio power/antennas/coverage, DFS handling, channel reservations, fragmentation, AP VLAN isolation, and restart delBA |
| [openwrt/170-plcharriere-hostapd.patch](openwrt/170-plcharriere-hostapd.patch) | FT/EHT/HE/SAE bounds and state checks, probe-client events, AP MLD active links, and CAC fallback |
| [openwrt/180-plcharriere-iw.patch](openwrt/180-plcharriere-iw.patch) | Per-radio power, antennas, and coverage class |
| [openwrt/190-plcharriere-iwinfo.patch](openwrt/190-plcharriere-iwinfo.patch) | Multi-radio interface lookup, band filtering, and frequency-list initialization |
| [openwrt/200-plcharriere-board-integration.patch](openwrt/200-plcharriere-board-integration.patch) | Board defaults, WED module options, RSS packet steering, PHY firmware v7.3, LEDs, radio setup, PCIe Wi-Fi reset, and NAND geometry |

The already-covered fixes are mt76 disassociation queue selection (`113`), mac80211 stable fixes `909` through `915`, and mt76 key upload (`138`). The older mt76 implementation always creates the link and already uploads its keys in `mt7996_vif_link_add()`; it does not have the newer link-reuse helper that `138` fixes.

## Adaptation details

These bundles apply after the existing local patches. Nested package patches run after the locked upstream series, including the existing always-apply-TXD workaround. Kernel changes run after the target series and the local netfs and RTL8221B PHY-mode EEE patches. The board retains the RTL822x SerDes fixes and userspace fan control.

The port uses the Linux 6.12 Net DIM calling convention, existing flowtable pending/dead synchronization, and the older combined flowtable path implementation. It retains the older WED callback interface and adds the station-flush callback and WCID accounting needed by the new lifetime fixes. Station flows are flushed after the station is unpublished and before its WCID is released. PCS references are released during device cleanup.

For mt76, the masked DMA accessors cover WED, NPU, and plain MMIO builds. The firmware statistics parser validates the event length before reading the new ADM formats. Per-radio configuration uses the existing `wiphy_radio_cfg` layout, and restart delBA uses the older mac80211 API. iw validates the radio selector before encoding its 8-bit index, including for TX power and antenna commands. RSS packet steering explicitly maps a null CPU to an empty RPS mask; the newer source's helper otherwise pins RPS to CPU 0 on this older netifd.

`configs/kernel/thermal.config` enables CPU frequency cooling for the existing 117 °C cooling map. The board image uses 256 KiB erase blocks and 4096-byte pages. First-boot defaults enable WED, preserve an explicit flow-offloading setting or enabled SQM queue, set the 5 GHz TX burst only when absent, and mark router DNS answers CS5. Failed UCI writes return an error so OpenWrt retains the scripts for retry. LED triggers track WAN interface state.

## Kernel modules and rebuilding

`openwrt.lock.json` selects official vermagic: the builder forces the release hash `f0a7348f9cb959e1bf4d0447669b031e`. WED, flowtable, and wireless structures and callbacks still change across this port, so affected modules need a matching build despite the shared hash. Compatibility of unrelated official modules has not been verified. The original source and firmware artifact caches have not been reset by this port.

Rebuild the firmware with the new source inputs:

```sh
./build.sh reset-sources
./build.sh
```

## Validation

Validation includes applying the OpenWrt series to the locked commit, replaying nested patches against the locked package sources, cross-compiling the affected Linux networking objects and cfg80211/mac80211/mt7996 modules, and compiling hostapd with 802.11ac/ax/be/r, SAE, and OCV enabled. The standalone module checks allow unresolved kernel symbols at modpost because a complete kernel image is not linked in that check.

The port also checks native iw/iwinfo compilation, the board DTB, the pinned PHY firmware checksum, shell/ucode syntax, and the repository's unit tests. After review, 78 tests passed; the APK integration test was skipped because the build's host APK tool was unavailable. The RSS mask regression test exercises the actual prepared netifd helper with ucode without changing host networking. The iw regression tests compile the patched argument handlers and check valid commands, index bounds, and malformed selectors. The board defaults test simulates failed UCI writes and checks that the scripts return failure for retry. During the initial port, the isolated builder completed `configure`, selected `mt7996-wed-enable` and `libiwinfo`, merged CPU frequency cooling, and confirmed native vermagic; the default has since been restored to official vermagic. The locked feeds emit unrelated dependency warnings, including a weechat Kconfig cycle; the requested configuration still passed the builder's validation.

A full firmware build, boot, throughput, roaming, and recovery tests on a GL-MT3600BE remain necessary for hardware validation.
