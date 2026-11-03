# mtk-repartition-skills

> [!WARNING]
> **DWYOR — Do With Your Own Risk.** Repartitioning can wipe your data, soft-brick or hard-brick your device,
> erase IMEI/calibration data or void your warranty. Everything here is provided "as is", without any warranty.
> **You alone are responsible for any damage, data loss or other consequence of using this skill, its scripts or
> its zips.** The repository owner and the skill's authors accept no liability whatsoever. Back up first, read every
> step, and do not proceed if you do not understand it.

An agent skill for safely editing the GPT partition table of MediaTek (MTK) Android phones and tablets
(UFS or eMMC) from a rooted Android shell over adb. It is written step by step, with explicit checks and hard
STOP conditions, so that even a small model can follow it without improvising.

Typical jobs:
- grow `super` and shrink `userdata` (the space is taken from the start of `userdata`);
- shrink `userdata` and add new partitions in the freed space (for example for a second OS);
- back up, check or restore the original GPT.

Out of scope: Qualcomm/Exynos devices, resizing filesystems in place, and SP Flash Tool scatter edits.

## Contents

```
skills/mtk-repartition/
  SKILL.md      the procedure (prerequisites, finding the LUN and sector size, backup, plan, verify,
                write, LK fastboot checks, restore, troubleshooting)
  gpt_tool.py   Python 3 helper, stdlib only, runs on the PC against dump files
```

`gpt_tool.py` subcommands:

| Command | What it does |
|---|---|
| `show` | Lists entries (LBA ranges, sizes, free gaps), checks both header and entry-array CRCs and that primary and backup agree |
| `dumpcmds` | Prints the exact `dd` commands that dump both GPT copies, with limits taken from the partitions (on MTK, partitions such as `misc` can start below `first_usable_lba`, so a blind `count=34` dump would include partition data) |
| `plan` | `--grow NAME:+SIZE --take-from NEXT`, `--shrink-end NAME:SIZE`, `--add NAME:SIZE\|rest` (1 MiB aligned). Writes the new primary/backup images, `plan.txt` (expected `fastboot getvar partition-size` values, sha256s), `write-gpt.sh` and `restore-gpt.sh` |
| `verify` | Independent check of a plan: CRCs, overlaps, usable range, entry capacity, unchanged entries byte-identical, protected partitions untouched, expected changed/added names |
| `mkzip` | `--plan DIR --sector SS [--name NAME] [--by-name super] [--out DIR]`. Builds `apply-NAME.zip` and `undo-NAME.zip`, flashable in TWRP (see below) |

The generated `write-gpt.sh` is a dry run unless given `--write`. Before writing, it checks root, the sector size,
the disk size and the sha256 of every file, and that the disk still holds exactly the backed-up GPT. It then writes
the primary and backup copies and reads both back. `restore-gpt.sh` puts the original GPT back the same way.

### Flashable recovery zips (`mkzip`)

As an alternative to running `write-gpt.sh` from rooted Android, `mkzip` turns a verified plan directory into two
zips for TWRP or any recovery that runs `META-INF/com/google/android/update-binary` as a shell script:

- `apply-NAME.zip` writes the new GPT; `undo-NAME.zip` writes the original GPT back.
- Each zip holds the `update-binary` (POSIX `sh`, works with toybox or busybox), a `#MAGIC` `updater-script`
  placeholder, all four GPT images (`gpt/gpt-primary.bin`, `gpt/gpt-backup.bin`, `gpt/new-primary.bin`,
  `gpt/new-backup.bin`) and `manifest.txt` (disk name, by-name partition used to find the disk, logical sector size,
  disk size, LBA offsets/counts, sha256 of every image, expected `partition-size` values).
- Before writing, the script requires: the by-name link (default `super`) resolves to a partition of the manifest disk;
  `/sys/block/DISK/queue/logical_block_size` and `/sys/block/DISK/size` match; every payload file and the manifest
  have the recorded sha256; `/data` and the changed partitions are not mounted; and each GPT copy on disk is
  exactly the expected "before" image or already the target image. Anything else: it prints `!! STOP: ...`, writes
  nothing, and exits 1. If the disk already holds the target table it says `ALREADY APPLIED` and exits 0.
- It writes the primary copy, then the backup copy (`dd conv=notrunc,fsync`), syncs, reads both back and compares
  sha256. On a failure it prints how to recover (flash the other zip, which accepts a half-written table, or the exact
  `dd` lines to run from the recovery's `adb shell`).
- It never formats anything. The recovery keeps the old partition table until reboot, so the script tells the user
  to reboot to LK fastboot, check `getvar partition-size`, then erase `userdata` and `metadata` when userdata changed.
- The undo zip restores only the table, not data: userdata must be erased again, added partitions are gone, and
  content of `super` beyond its original size is lost.
- `MTKREP_TEST_ROOT=<dir>` is a test-only switch (fake `/dev/block`, `/sys`, `/proc/mounts`, an image file as the
  disk) for offline tests; never set it on a device.

## Install

Claude Code (personal skill):

```sh
mkdir -p ~/.claude/skills
cp -r skills/mtk-repartition ~/.claude/skills/
```

Or copy it into a project's `.claude/skills/`. Then ask, for example, "grow super by 8 GiB on my MTK tablet"
or invoke `/mtk-repartition`.

## Safety notes

- Growing `super` by taking space from `userdata` moves the start of `userdata`: **all user data is lost**.
  The skill asks for explicit confirmation first.
- Growing the `super` partition does not grow the dynamic-partition metadata inside it; the extra space stays unused
  until a super image built for the new size (`lpmake`) is flashed. Check with `lpdump`.
- Partitions such as `preloader`, `nvram`, `nvdata`, `protect1/2`, `md1img`, `seccfg`, `lk`, `boot` are never
  moved or resized; `nvram`/`nvdata`/`protect*` hold the IMEI and calibration data.
- SP Flash Tool "Format All + Download" or "Firmware Upgrade" with the stock scatter file restores the stock layout.
- Keep the GPT backups off the device. DWYOR: you use this entirely at your own risk (see the warning at the top).

## Status

`gpt_tool.py` was tested offline against real GPT dumps from an MT6789 tablet (UFS, 4096-byte sectors) and against
synthetic 512- and 4096-byte-sector images: the full flow (dump, plan, verify, write to an image file, restore) and
the refusal cases (bad CRCs, protected partitions, unaligned sizes, overlapping or oversized plans, mismatched
dumps). The device-side scripts were exercised with GNU tools, not yet with toybox on Android, and the `lpmake`
re-flash path is described only at a high level.

The `mkzip` update-binary was tested offline under `dash` and `busybox sh` (with a busybox-only `PATH`, as in TWRP)
against 512- and 4096-byte-sector disk images and a sparse full-size image built from the real MT6789 dumps: apply,
re-apply, undo back to a byte-identical disk, half-written tables, and the refusal cases (wrong disk size, sector
size or disk, tampered payload, unexpected table, mounted `/data`, missing `sha256sum`). It has not yet been
flashed in TWRP on a device.
