---
name: mtk-repartition
description: Safely edit the GPT partition table of a MediaTek (MTK) Android phone or tablet (UFS or eMMC) from a rooted Android shell over adb. Use when the user wants to grow `super`, shrink `userdata`, add new partitions in the freed space (for example for a second OS), check or back up the GPT, restore the original GPT from backups, or build flashable TWRP zips that apply or undo the change. Does not cover Qualcomm/Exynos devices, resizing filesystems in place, or anything done through SP Flash Tool scatter files.
---

# MTK GPT repartition (grow super / shrink userdata / add partitions / restore)

Every step is written out. Do them in order. Do not improvise, skip, or merge steps.
**STOP** means: do not continue; tell the user exactly what failed and wait.

**DWYOR (Do With Your Own Risk):** before step 1, tell the user plainly that repartitioning can wipe data,
brick the device or destroy IMEI/calibration data; that everything is provided "as is" without warranty; and that
the user alone bears all risk and responsibility, not the skill's authors or repository owner. Continue only after
the user explicitly accepts this.

The helper `gpt_tool.py` sits next to this file (use its absolute path, called `TOOL` below).
It runs on the **PC** (Python 3, no extra packages). It works only on dump files and never
writes to the device (the zips from `mkzip` write only when flashed in recovery, section 14).
Every command ends with `RESULT: ...` on success, or prints `STOP: ...` and exits with status 1.
If you see `STOP:` or `ERROR:`, STOP.

## 1. When to use / when not

Use for:
- growing `super` (space taken from the start of `userdata`, which follows it),
- shrinking `userdata` (moving its end) to free space at the end of the disk,
- adding partitions in that freed space,
- backing up, checking, or restoring the GPT.

Do NOT use when:
- the device is not MediaTek, or the bootloader is locked, or there is no root shell;
- the user wants to keep the data in `userdata` (both growing `super` and shrinking `userdata`
  **destroy all of userdata**; there is no way around this here);
- the goal is to move or resize any partition other than `super`/`userdata` (see section 5);
- the user only has SP Flash Tool access (no booting Android): this skill needs a rooted Android.

## 2. Prerequisites (check every one; STOP if one is missing)

1. Unlocked bootloader. Root through Magisk or KernelSU.
   Check: `adb shell su -c id` must print `uid=0(root)`.
2. `adb` and `fastboot` on the PC. `adb devices` shows exactly one device.
3. `adb reboot bootloader` reaches **LK fastboot** (MTK bootloader fastboot) and
   `fastboot devices` shows the device there. Then `fastboot reboot` back to Android.
4. Python 3 on the PC: `python3 TOOL --help` works.
5. Battery above 50 %. Nothing else running on the device (no OTA, no app installs).
6. The user knows the stock firmware is at hand (for SP Flash Tool, see section 11).
7. Make a work directory on the PC, e.g. `gpt-work/`, and run all PC commands inside it.

## 3. Find the disk (LUN), sector size and disk size

Run:
```sh
adb shell su -c "ls -l /dev/block/by-name/"
```
If that fails, use `adb shell su -c "ls -l /dev/block/platform/*/by-name/"`.

Find the lines for `super` and `userdata`. Example: `super -> /dev/block/sdc60`.
- The disk is the name without the trailing number: `sdc60` -> `sdc`; `mmcblk0p45` -> `mmcblk0`.
- `super` and `userdata` MUST be on the same disk. If not: STOP.

Write these values down (the "worksheet"). In every later command, replace the words `DEV`,
`SS`, `SIZE512`, `DISK` and `TOOL` with these values (do not type them literally):

| Value | How to get it | Example |
|---|---|---|
| `DISK` | from by-name, e.g. `sdc` | `sdc` |
| `DEV` | `/dev/block/DISK` | `/dev/block/sdc` |
| `SS` (sector size) | `adb shell su -c "cat /sys/block/DISK/queue/logical_block_size"` | `4096` |
| `SIZE512` | `adb shell su -c "cat /sys/block/DISK/size"` (always in 512-byte units) | `250069680` |
| `R` (device work dir) | fixed | `/data/local/tmp/gpt` |

- `SS` must be read, never assumed. UFS is usually 4096, eMMC usually 512. If it is neither 512
  nor 4096: STOP.
- Never use a partition node (`sdc60`) or a `pgpt`/`sgpt` by-name link for GPT reads or writes;
  always the whole disk `DEV`.

## 4. Mandatory backup of BOTH GPT copies

A GPT has two copies: the primary at the start of the disk and the backup at the very end
(backup entry array + backup header in the last LBA). Dumping only the last LBA misses the backup
entries: the tool rejects such a dump.

1. Create the device work dir as the shell user (so `adb push` can write into it later):
   ```sh
   adb shell mkdir -p /data/local/tmp/gpt
   ```
2. Dump the first 34 sectors (read-only) and pull them:
   ```sh
   adb shell su -c "dd if=DEV of=/data/local/tmp/gpt/head.bin bs=SS count=34"
   adb shell su -c "chmod 644 /data/local/tmp/gpt/head.bin"
   adb pull /data/local/tmp/gpt/head.bin head.bin
   ```
3. Let the tool print the exact dump commands:
   ```sh
   python3 TOOL dumpcmds --head-dump head.bin --sector SS --dev DEV --disk-size-512 SIZE512
   ```
   STOP on `STOP:` (wrong disk, wrong sector size, damaged GPT).
4. Run every `adb ...` line it printed, in order, exactly as printed. Check that the pulled file
   sizes match the `# expected sizes` line.
5. Check the dumps:
   ```sh
   python3 TOOL show gpt-primary.bin gpt-backup.bin --sector SS
   ```
   Must end with `RESULT: OK` and `primary vs backup: consistent`. Lines starting with `note:`
   about partitions below the first usable LBA are normal on MTK. Otherwise STOP.
6. Copy `gpt-primary.bin` and `gpt-backup.bin` to a **second place off the device** (another
   disk, cloud, USB stick) and record their sha256 (`sha256sum gpt-*.bin`). Do not continue until
   the user confirms the copy exists. These two files are the only way back besides SP Flash Tool.

## 5. What may change, what must never change

The table on MTK is usually ordered `... super, userdata, [free space], flashinfo` (`flashinfo`
or similar small partitions sit at the very end; they stay where they are).

Only `super` and `userdata` may change; new partitions may be added in free space. Everything
else must stay byte-identical. Examples of MTK partitions that must NEVER move or change:
`preloader*`, `pgpt`, `sgpt`, `proinfo`, `nvram`, `nvcfg`, `nvdata`, `persist`, `protect1`,
`protect2`, `seccfg`, `sec1`, `otp`, `efuse`, `md1img*`, `spmfw*`, `scp*`, `sspm*`, `mcupm*`,
`dpm*`, `pi_img*`, `gz*`, `lk*`, `tee*`, `boot*`, `init_boot*`, `vendor_boot*`, `dtbo*`,
`vbmeta*`, `para`, `misc`, `frp`, `metadata`, `expdb`, `logo`, `connsys*`, `boot_para`,
`dram_para`, `flashinfo`. (`nvram`/`nvdata`/`protect*` hold IMEI and calibration: losing them
breaks the radio for good.) The tool refuses to touch these; never try to work around that.

## 6. The data-loss warning (ask before planning)

- Growing `super` means moving the **start** of `userdata`. Shrinking `userdata` cuts its
  filesystem. Either way **everything in userdata is erased**: apps, photos, downloads, internal
  storage. Android re-creates userdata empty on the next boot.
- Tell the user exactly this, ask them to back up their data, and get an explicit "yes, wipe
  userdata" reply. Without it: STOP.

### Growing super does not grow the dynamic partitions

`super` holds a liblp metadata block that records its own "block device size". Making the GPT
partition bigger does **not** change that number, so Android keeps using only the old size; the
new space is simply unused (this is harmless). Check after the job:
```sh
adb shell su -c "lpdump"
```
Under `Block device table:` the `Size:` of `super` is what liblp uses. Compare it with the new
GPT size of super (`plan.txt`). To actually use the new space:
- re-flash a `super` image built with `lpmake` for the new size (`--device super:<new size in
  bytes>`, same metadata size/slots/groups as stock, partitions from the stock images or from
  `lpunpack` of the stock super image), with `fastboot flash super super.img`; or
- leave it until such an image exists; nothing breaks meanwhile.
Do not try to edit the liblp metadata by hand.

## 7. Plan (on the PC)

Pick the operation(s). Sizes use `KiB`, `MiB`, `GiB` (binary); `GB` is rejected on purpose.
Alignment defaults to 1 MiB; sizes must be multiples of it.

| Goal | Options |
|---|---|
| grow super by 8 GiB (takes userdata's start) | `--grow super:+8GiB --take-from userdata` |
| shrink userdata to 60 GiB (end moves, frees the tail) | `--shrink-end userdata:60GiB` |
| add a partition of 12 GiB in the freed space | `--add NAME:12GiB` |
| add a partition filling the rest of the free space | `--add NAME:rest` |

Operations run in the order given. `--add` uses the largest free gap. The default type GUID for
new partitions is the MTK "basic data" type; pass `--add-type <guid>` for another (e.g. Linux
filesystem `0fc63daf-8483-4772-8e79-3d69d8477de4`). `--min-remaining` (default `1GiB`) is the
smallest size a shrunk partition may keep; keep userdata at several GiB at least.

```sh
python3 TOOL plan --primary gpt-primary.bin --backup gpt-backup.bin --sector SS --dev DEV \
    --out plan1 <operations>
```
It must end with `RESULT: PLAN OK (verified)`. It prints the BEFORE and AFTER tables and writes
`plan1/`: `gpt-primary.bin`, `gpt-backup.bin` (copies of the originals), `new-primary.bin`,
`new-backup.bin`, `write-gpt.sh`, `restore-gpt.sh`, `plan.txt`.

Show the user the AFTER table and `plan1/plan.txt`. Ask them to confirm the new sizes. If they
want something different, re-run plan with a new `--out` directory.

## 8. Verify (on the PC; independent second check)

```sh
python3 TOOL verify --orig-primary gpt-primary.bin --orig-backup gpt-backup.bin \
    --new-primary plan1/new-primary.bin --new-backup plan1/new-backup.bin --sector SS \
    --expect-changed <changed names, comma separated> --expect-added <added names or empty>
```
Use the `changed:` and `added:` lists from `plan.txt`, e.g. `--expect-changed super,userdata
--expect-added ""`. It must end with `RESULT: VERIFY OK`. It checks both header CRCs and both
entry-array CRCs, no overlaps, all partitions inside the usable range, entry count fits the entry
area, every untouched entry byte-identical, and only the listed partitions changed. Else STOP.

## 9. Write (device)

To write from TWRP with a flashable zip instead of the rooted Android shell, see section 14.

1. Push the plan files:
   ```sh
   adb push plan1/new-primary.bin plan1/new-backup.bin plan1/write-gpt.sh plan1/restore-gpt.sh /data/local/tmp/gpt/
   ```
2. Dry run (checks only; writes nothing):
   ```sh
   adb shell su -c "sh /data/local/tmp/gpt/write-gpt.sh"
   ```
   It must print `CHECKS OK. Nothing written.` It checks root, the disk, sector size, disk size,
   the sha256 of all four files, and that the disk still holds exactly the backed-up original
   GPT. Anything else: STOP.
3. Last confirmation from the user that userdata will be wiped. Then write:
   ```sh
   adb shell su -c "sh /data/local/tmp/gpt/write-gpt.sh --write"
   ```
   The script writes the primary copy, then the backup copy (`dd ... conv=notrunc,fsync`), reads
   both back and compares sha256. It must print `WRITE OK`.
   - If it prints `READBACK MISMATCH` or `write failed`: run
     `adb shell su -c "sh /data/local/tmp/gpt/restore-gpt.sh --write"` immediately (must print
     `RESTORE OK`), then STOP. Do not reboot before the restore succeeded.
4. Do not use the device after `WRITE OK`. The running kernel still uses the old table.
   Go straight to step 10.

## 10. Reboot, check in LK fastboot, erase, boot

```sh
adb reboot bootloader
fastboot devices
fastboot getvar partition-size:userdata
fastboot getvar partition-size:super
```
Each value must equal the `partition-size:` line in `plan1/plan.txt` (hex bytes, e.g.
`0xf00000000`). Also check added partitions the same way (see section 13 if one shows empty).
If userdata or super shows the OLD size: do not erase; see section 13.

Then:
```sh
fastboot erase userdata
fastboot erase metadata
fastboot reboot
```
`metadata` holds the encryption keys of the old userdata; it must be erased too. The first boot
takes longer while Android formats userdata. Afterwards check in Settings > Storage (or
`adb shell df -h /data`) that the size matches, and run `lpdump` (section 6) if super was grown.
Keep the `gpt-work/` files and the off-device copies.

## 11. Restore the original GPT

From TWRP instead: flash `undo-NAME.zip` (section 14).

From a rooted Android (the backups must still match: `restore-gpt.sh` checks their sha256):
```sh
adb shell mkdir -p /data/local/tmp/gpt
adb push plan1/gpt-primary.bin plan1/gpt-backup.bin plan1/restore-gpt.sh /data/local/tmp/gpt/
adb shell su -c "sh /data/local/tmp/gpt/restore-gpt.sh"           # dry run: CHECKS OK
adb shell su -c "sh /data/local/tmp/gpt/restore-gpt.sh --write"   # must print RESTORE OK
adb reboot bootloader
fastboot getvar partition-size:userdata     # must be the ORIGINAL size again
fastboot erase userdata
fastboot erase metadata
fastboot reboot
```
Data in partitions that were added is lost after a restore (they no longer exist), and userdata
is wiped again.

If nothing boots: SP Flash Tool with the stock firmware's scatter file, **"Format All +
Download"** or **"Firmware Upgrade"**, rewrites the stock GPT and all partitions (stock layout,
all data gone). Prefer "Firmware Upgrade": "Format All" can also erase `nvram`/`nvdata`
(IMEI, calibration) unless they were backed up.
Note the reverse: these SP Flash Tool modes also UNDO a custom layout. "Download Only" of single
partitions keeps the GPT as it is. After an SP Flash Tool restore, redo this skill from section 3
to get the custom layout back (dump again; never reuse old `new-*.bin` blindly: the tool refuses
if the disk no longer matches the original dump).

## 12. Redo after a reflash

The plan is reproducible: run sections 3-10 again with the same operations. Compare the new
AFTER table with the old `plan.txt`; the LBA ranges should be identical.

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `show` says the file "looks like sector size 4096/512" | wrong `--sector`; read `logical_block_size` again. |
| `show`: dump does not contain the whole entry array | the backup dump is too short; use the exact `dumpcmds` lines. |
| `plan` STOP: dump reaches into partition data | the primary dump was taken with too many sectors; use `dumpcmds`. |
| `plan` STOP: needs space from userdata, pass `--take-from` | growing super eats userdata's start; add `--take-from userdata` only after the user agreed to the wipe. |
| `write-gpt.sh`: GPT on disk is not the backed-up original | the table changed since the dump (or wrong disk); dump again from section 4. |
| `write-gpt.sh`: `ALREADY WRITTEN` | the new table is already on disk; continue with section 10. |
| `adb pull`: permission denied | run `adb shell su -c "chmod 644 /data/local/tmp/gpt/*"`. |
| `adb push` fails into `/data/local/tmp/gpt` | the dir was made by root; `adb shell su -c "chmod 777 /data/local/tmp/gpt"`. |
| LK `getvar partition-size:<new partition>` is empty | some LK builds only list partitions they know, or keep the table read at power-on; run `fastboot reboot-bootloader` and check again. An empty value for a NEW partition is not an error if super/userdata are right. |
| LK still shows the OLD userdata size | the write did not land or LK did not re-read: `fastboot reboot-bootloader`; if still old, boot Android, run `python3 TOOL show` on a fresh dump; do not erase until it matches. |
| Android boot loops / "Can't load Android system" | userdata or metadata was not erased: LK fastboot `fastboot erase userdata` + `fastboot erase metadata`, or pick "Factory data reset" in recovery. |
| Android boots but storage shows the old size | userdata was not re-created; erase userdata + metadata again. |
| zip: `/data is mounted` / `... is mounted` | TWRP mounted Data: **Mount** > untick Data, flash again. |
| zip: `by-name link ... not found` | this recovery names the by-name dir differently or does not see the disk; check `ls -l /dev/block/by-name/` in its adb shell; STOP if `super` is missing. |
| zip: `primary/backup GPT on DISK ... is neither ...` | wrong device, or the table changed since the dump (e.g. after SP Flash Tool); dump again from Android (section 4), re-plan, rebuild the zips. |
| zip: `no sha256sum` / `no unzip` | the recovery lacks toybox/busybox tools; use section 9 from rooted Android instead. |
| `lpdump` shows the old super size | expected; see section 6. |
| `dd: Operation not permitted` / read-only | not root (`su -c id`), or the disk is read-only: `adb shell su -c "blockdev --setrw DEV"`, then retry the dry run. |

## 14. Alternative: apply from TWRP with a flashable zip

Use this instead of section 9 (and to undo instead of section 11) when the change should be
written from a custom recovery: TWRP, or any recovery that runs
`META-INF/com/google/android/update-binary` from a zip as a shell script. Typical reasons: the
user prefers recovery, or Android root is no longer available after the change. Sections 2-8
still apply in full: root dumps, the off-device backup copy, the data-loss confirmation, `plan`
and **`verify` (section 8) must be done first**. The zip does not replace any of them.

Extra prerequisites (STOP if one is missing):
- A recovery for this exact device that boots and shows `adb devices` as `recovery`.
- In the recovery's `adb shell`: `ls -l /dev/block/by-name/` lists `super` (or the partition you
  pass to `--by-name`) on the same disk `DISK` as in the worksheet.
- The zip must NOT be stored in internal storage (`/sdcard` lives inside userdata, which must
  be unmounted for the flash). Use `adb sideload`, or `adb push` to `/tmp`, or an external
  SD card / USB stick.

1. Build the zips on the PC (after section 8 printed `RESULT: VERIFY OK`):
   ```sh
   python3 TOOL mkzip --plan plan1 --sector SS --name NAME
   ```
   `NAME` is a short word (A-Z a-z 0-9 _ . -), e.g. `hakios`. It must end with
   `RESULT: MKZIP OK` and writes `plan1/apply-NAME.zip` and `plan1/undo-NAME.zip`. It re-checks the
   plan files against `plan.txt`. It takes the disk name from the plan's `--dev`; check that the
   printed `disk` and `sector` match the worksheet, else STOP. Optional: `--by-name PART` (default
   `super`; must exist in the old and the new table), `--out DIR`.
2. Copy BOTH zips to a second place off the device (like the GPT backups). The undo zip is the
   way back; it never needs to be on the device before you use it.
3. Show the user `unzip -p plan1/apply-NAME.zip manifest.txt` (disk, sector size, disk size,
   LBA ranges, sha256 of every image, expected partition sizes). Get the final "yes, wipe
   userdata" again.
4. Reboot to recovery (`adb reboot recovery`). In TWRP: **Mount** > untick **Data** (and any other
   ticked partition on the same disk). The zip refuses while `/data` or a changed partition is
   mounted.
5. Flash the apply zip. Either:
   - TWRP **Advanced > ADB Sideload**, untick "Wipe Dalvik Cache" and "Wipe Cache", swipe, then on
     the PC: `adb sideload plan1/apply-NAME.zip`; or
   - `adb push plan1/apply-NAME.zip /tmp/`, then **Install** > navigate to `/tmp` > select it >
     swipe (leave "zip signature verification" off; the zip is not signed).
6. Read the recovery screen (or `adb pull /tmp/recovery.log`). The zip first checks, and writes
   nothing (exit with `!! STOP: ...` and `Nothing was written`) unless ALL hold:
   - `by-name/<PART>` resolves to a partition of the manifest disk (e.g. `sdc`);
   - `/sys/block/DISK/queue/logical_block_size` = SS and `/sys/block/DISK/size` = the manifest size;
   - sha256 of every file in the zip matches;
   - each GPT copy on disk is exactly the expected "before" image (or already the target image).
   On `STOP`: STOP and report the message. Do not edit the zip or try another zip.
   - `ALREADY APPLIED`: the disk already has this table; nothing was written; continue with step 7.
   - `apply DONE`: both copies were written (primary first, then backup) and read back with
     matching sha256.
   - `GPT WRITE FAILED` / `READBACK MISMATCH`: do NOT reboot. Flash `undo-NAME.zip` at once (it
     accepts a half-written table) and STOP. The message also prints `dd` lines to restore by hand
     from the recovery's `adb shell`.
7. The recovery keeps using the OLD table until reboot. Do not mount, wipe, format or flash
   anything else. The zip never formats anything. Use TWRP **Reboot > Bootloader** (or
   `adb reboot bootloader`), then do section 10 exactly (getvar checks, then `fastboot erase
   userdata` and `fastboot erase metadata`, then `fastboot reboot`). The zip printed the expected
   `getvar` values; they equal `plan.txt`.

To undo (put the original table back): boot the recovery, untick Data in **Mount**, flash
`undo-NAME.zip` the same way (sideload from the PC). It refuses unless the disk holds the NEW
table of this plan (or a half-written mix of the two); on the original table it prints
`ALREADY APPLIED`. Then Reboot > Bootloader, `fastboot getvar partition-size:userdata` must be
the ORIGINAL size, then `fastboot erase userdata`, `fastboot erase metadata`, `fastboot reboot`.
The undo zip restores only the partition TABLE, not data: userdata must be erased again, the
added partitions and their data are gone, and anything in super beyond its original size is lost
(if a super image built for the bigger super was flashed, flash the stock super image too).
The same STOP rules apply: if the undo zip refuses because the disk holds neither table, use
section 11 or SP Flash Tool.

Test-only: `MTKREP_TEST_ROOT=<dir>` makes the update-binary read `<dir>/dev/block`, `<dir>/sys`
and `<dir>/proc/mounts` and accept an image file as the disk. It is for offline tests on a PC;
never set it on a device.

## Rules (summary)

- Never write anything to the device except through `write-gpt.sh` / `restore-gpt.sh` or the
  `apply-*.zip` / `undo-*.zip` from `mkzip`, all from a verified plan. Never hand-edit `.bin`
  files or zips.
- Never assume the sector size. Never skip the backup or its off-device copy.
- Never touch partitions other than `super`, `userdata` and newly added ones.
- Never erase anything in fastboot before `getvar` confirmed the new sizes.
- Any `STOP:`/`ERROR:`/mismatch: STOP and report it; do not "fix" it by trying variations.
