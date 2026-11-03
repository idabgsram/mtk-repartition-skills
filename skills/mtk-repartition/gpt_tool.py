#!/usr/bin/env python3
"""gpt_tool.py - inspect, plan and verify GPT edits on dumps taken from an Android device.

Works OFFLINE on dump files (Python 3, stdlib only). It never writes to a block device.
`show` may read a block device read-only, but the normal flow is to work on dumps.

Dump files (all in units of the LOGICAL sector size SS of the disk):
  head dump     : LBA 0..33 (dd bs=SS count=34), only read, used by `dumpcmds`
  primary dump  : LBA 0 .. (start of the first partition)-1   (protective MBR + header + entry area)
  backup dump   : (end of the last partition)+1 .. last LBA   (backup entry area + backup header)
  (MTK often places partitions below first_usable_lba, so the dump limits come from the partitions.)
`dumpcmds` prints the exact dd commands for the primary and backup dumps.

Subcommands:
  show     FILE [FILE2] --sector SS      list partitions, free gaps, check CRCs/consistency
  dumpcmds --head-dump F --sector SS --dev DEV --disk-size-512 N
  plan     --primary P --backup B --sector SS --dev DEV --out DIR  ops...
  verify   --orig-primary P --orig-backup B --new-primary NP --new-backup NB --sector SS
           [--expect-changed a,b] [--expect-added x,y]

Every inconsistency is an error; the tool then exits with status 1 and prints "STOP".
"""
import argparse, os, re, struct, sys, uuid, zlib, hashlib, stat

HDR_FMT = '<8sIIIIQQQQ16sQIII'          # 92 bytes
HDR_SIZE = 92
SIG = b'EFI PART'
REV = 0x00010000
BASIC_DATA = 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7'   # type used by MTK for every partition
LINUX_FS = '0fc63daf-8483-4772-8e79-3d69d8477de4'

# Partitions that must never be moved or resized (regexes, full match, case-insensitive).
PROTECTED = [r'preloader.*', r'pgpt', r'sgpt', r'proinfo', r'nvram', r'nvcfg', r'nvdata', r'persist',
             r'protect.*', r'seccfg', r'sec1', r'otp', r'efuse', r'md\d.*', r'spmfw.*', r'pi_img.*',
             r'dpm.*', r'scp.*', r'sspm.*', r'mcupm.*', r'gz.*', r'lk.*', r'tee.*', r'boot.*',
             r'init_boot.*', r'vendor_boot.*', r'dtbo.*', r'vbmeta.*', r'para', r'misc', r'frp',
             r'metadata', r'expdb', r'flashinfo', r'logo', r'csci', r'boot_para', r'dram_para',
             r'connsys.*', r'gpueb.*', r'audio_dsp.*', r'cam_vpu.*', r'vcp.*', r'ccu.*', r'mvpu.*',
             r'oem.*', r'recovery.*', r'efs', r'nvdata.*', r'tkv', r'apdb', r'mddb', r'super_meta.*']
DEFAULT_ALLOWED = {'super', 'userdata'}


class Stop(Exception):
    pass


def is_protected(name):
    return any(re.fullmatch(p, name, re.I) for p in PROTECTED)


# ---------------------------------------------------------------- sizes
UNITS = {'': 1, 'B': 1, 'KIB': 1 << 10, 'MIB': 1 << 20, 'GIB': 1 << 30, 'TIB': 1 << 40}


def parse_size(s):
    m = re.fullmatch(r'\+?(\d+)\s*([A-Za-z]*)', s.strip())
    if not m or m.group(2).upper() not in UNITS:
        raise Stop(f'bad size {s!r}: use a whole number with B, KiB, MiB, GiB or TiB (e.g. 8GiB)')
    return int(m.group(1)) * UNITS[m.group(2).upper()]


def human(nbytes):
    for u, f in (('TiB', 1 << 40), ('GiB', 1 << 30), ('MiB', 1 << 20), ('KiB', 1 << 10)):
        if nbytes >= f:
            return f'{nbytes / f:.2f} {u}'
    return f'{nbytes} B'


def sha256(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- parsing
def parse_header(buf, off):
    if off < 0 or off + HDR_SIZE > len(buf):
        return None
    f = struct.unpack(HDR_FMT, buf[off:off + HDR_SIZE])
    h = dict(sig=f[0], rev=f[1], hsize=f[2], crc=f[3], reserved=f[4], cur=f[5], alt=f[6], first=f[7],
             last=f[8], guid=f[9], elba=f[10], n=f[11], esz=f[12], ecrc=f[13], off=off)
    return h


def header_crc(buf, off, hsize):
    raw = bytearray(buf[off:off + hsize])
    raw[16:20] = b'\0\0\0\0'
    return zlib.crc32(raw) & 0xffffffff


def pack_header(h):
    return struct.pack(HDR_FMT, h['sig'], h['rev'], h['hsize'], h['crc'], h['reserved'], h['cur'], h['alt'],
                       h['first'], h['last'], h['guid'], h['elba'], h['n'], h['esz'], h['ecrc'])


def entry_fields(e):
    t, u, first, last, attr = struct.unpack('<16s16sQQQ', e[:56])
    name = e[56:128].decode('utf-16le', 'replace').split('\0')[0]
    return t, u, first, last, attr, name


def make_entry(type_guid, uniq, first, last, attr, name, esz):
    nm = name.encode('utf-16le')
    if len(nm) > 72:
        raise Stop(f'partition name {name!r} longer than 36 characters')
    e = struct.pack('<16s16sQQQ', type_guid, uniq, first, last, attr) + nm + b'\0' * (72 - len(nm))
    return e + b'\0' * (esz - 128)


class Copy:
    """One GPT copy (primary or backup) inside a dump image."""

    def __init__(self, img, ss, role, start_lba, hdr_lba_in_img, path):
        self.img, self.ss, self.role, self.start_lba, self.path = bytearray(img), ss, role, start_lba, path
        self.hoff = hdr_lba_in_img * ss
        self.h = parse_header(self.img, self.hoff)

    # byte offset of the entry array inside the image
    @property
    def eoff(self):
        return (self.h['elba'] - self.start_lba) * self.ss

    def entries(self, n=None):
        n = self.h['n'] if n is None else n
        return bytes(self.img[self.eoff:self.eoff + n * self.h['esz']])

    def capacity(self):
        """How many entries fit in this copy's entry area (on disk)."""
        h = self.h
        if self.role == 'primary':
            area = min(h['first'], self.lowest()) - h['elba']
        else:
            area = h['cur'] - h['elba']
        return area * self.ss // h['esz'] if h['esz'] else 0

    def image_capacity(self):
        """How many entries the dump image itself holds (bytes present in the file)."""
        h = self.h
        if self.eoff < 0:
            return 0
        if self.role == 'primary':
            room = len(self.img) - self.eoff
        else:
            room = self.hoff - self.eoff
        return max(0, room // h['esz']) if h['esz'] else 0

    def lowest(self):
        """Lowest LBA used by a partition (MTK often starts partitions BELOW first_usable_lba)."""
        return min([p['first'] for p in self.parts()] + [self.h['first']])

    def highest(self):
        return max([p['last'] for p in self.parts()] + [self.h['last']])

    def parts(self):
        out = []
        esz = self.h['esz']
        ent = self.entries()
        for i in range(self.h['n']):
            e = ent[i * esz:(i + 1) * esz]
            t, u, first, last, attr, name = entry_fields(e)
            if t != b'\0' * 16:
                out.append(dict(idx=i, name=name, first=first, last=last, type=t, uniq=u, attr=attr))
        return out

    def check(self):
        """Return a list of error strings for this copy alone."""
        errs, h, ss, r = [], self.h, self.ss, self.role
        self.warns = []
        if h is None:
            return [f'{r}: no header inside the dump']
        if h['sig'] != SIG:
            return [f'{r}: no "EFI PART" signature at the expected place (wrong --sector or wrong dump?)']
        if h['rev'] != REV:
            errs.append(f'{r}: unexpected revision {h["rev"]:#x}')
        if not (HDR_SIZE <= h['hsize'] <= ss):
            errs.append(f'{r}: bad header size {h["hsize"]}')
            return errs
        if header_crc(self.img, self.hoff, h['hsize']) != h['crc']:
            errs.append(f'{r}: HEADER CRC MISMATCH')
        if h['esz'] < 128 or h['esz'] % 128:
            errs.append(f'{r}: bad entry size {h["esz"]}')
            return errs
        if h['first'] > h['last']:
            errs.append(f'{r}: first usable LBA > last usable LBA')
        need = -(-h['n'] * h['esz'] // ss)          # entry LBAs in use
        if r == 'primary':
            if h['cur'] != 1:
                errs.append(f'primary: header says it is at LBA {h["cur"]}, expected 1')
            if h['alt'] <= h['last']:
                errs.append('primary: backup header LBA is inside the usable area')
            if h['elba'] < 2 or h['elba'] + need > h['first']:
                errs.append('primary: entry array does not fit between LBA 2 and the first usable LBA')
        else:
            if h['alt'] != 1:
                errs.append(f'backup: header says the primary is at LBA {h["alt"]}, expected 1')
            if h['elba'] <= h['last'] or h['elba'] + need > h['cur']:
                errs.append('backup: entry array does not fit between last usable LBA and the backup header')
        if self.eoff < 0 or self.image_capacity() < h['n']:
            errs.append(f'{r}: the dump {self.path} does not contain the whole entry array '
                        f'(entries at LBA {h["elba"]}..{h["elba"] + need - 1}); take the dump again with '
                        f'the commands from `dumpcmds`')
            return errs
        if zlib.crc32(self.entries()) & 0xffffffff != h['ecrc']:
            errs.append(f'{r}: ENTRY ARRAY CRC MISMATCH')
            return errs
        e, w = check_parts(self.parts(), h, r, h['elba'] + need if r == 'primary' else None)
        errs += e
        self.warns = w
        if r == 'backup' and h['elba'] <= self.highest():
            errs.append('backup: entry array overlaps a partition')
        return errs


def check_parts(parts, h, r, entry_end=None):
    """Returns (errors, warnings)."""
    errs, warns = [], []
    names, uniqs = {}, {}
    for p in parts:
        if p['first'] > p['last']:
            errs.append(f'{r}: {p["name"]} ends before it starts')
        if p['first'] < h['first'] and p['last'] <= h['last']:
            if p['first'] < max(2, entry_end or 0):
                errs.append(f'{r}: {p["name"]} (LBA {p["first"]}) overlaps the GPT header/entry array')
            else:
                warns.append(f'{r}: {p["name"]} starts at LBA {p["first"]}, below first usable LBA {h["first"]} '
                             f'(normal on MTK; leave it alone)')
        elif p['first'] < h['first'] or p['last'] > h['last']:
            errs.append(f'{r}: {p["name"]} ({p["first"]}..{p["last"]}) is outside the usable LBAs '
                        f'{h["first"]}..{h["last"]}')
        if p['name'] in names:
            errs.append(f'{r}: duplicate partition name {p["name"]!r}')
        names[p['name']] = 1
        if p['uniq'] in uniqs:
            errs.append(f'{r}: duplicate unique GUID ({p["name"]} and {uniqs[p["uniq"]]})')
        uniqs[p['uniq']] = p['name']
    srt = sorted(parts, key=lambda p: p['first'])
    for a, b in zip(srt, srt[1:]):
        if b['first'] <= a['last']:
            errs.append(f'{r}: OVERLAP {a["name"]} ({a["first"]}..{a["last"]}) and {b["name"]} ({b["first"]}..{b["last"]})')
    return errs, warns


def free_gaps(parts, h):
    gaps, pos = [], h['first']
    for p in sorted(parts, key=lambda p: p['first']):
        if p['first'] > pos:
            gaps.append((pos, p['first'] - 1))
        pos = max(pos, p['last'] + 1)
    if pos <= h['last']:
        gaps.append((pos, h['last']))
    return gaps


# ---------------------------------------------------------------- loading
def read_file(path, max_bytes=256 << 20):
    st = os.stat(path)
    if stat.S_ISBLK(st.st_mode):
        raise Stop('internal: block devices are handled by load_device')
    if st.st_size > max_bytes:
        raise Stop(f'{path} is {human(st.st_size)}; give a GPT dump, not a whole-disk image larger than '
                   f'{human(max_bytes)}')
    with open(path, 'rb') as f:
        return f.read()


def guess_sector(data):
    for ss in (512, 4096):
        if data[ss:ss + 8] == SIG or data[len(data) - ss:len(data) - ss + 8] == SIG:
            return ss
    return None


def load(path, ss):
    """Return list of Copy objects found in the file: [primary], [backup] or [primary, backup]."""
    st = os.stat(path)
    if stat.S_ISBLK(st.st_mode):
        return load_device(path, ss)
    data = read_file(path)
    if len(data) % ss:
        g = guess_sector(data)
        raise Stop(f'{path}: size {len(data)} is not a multiple of --sector {ss}'
                   + (f' (looks like sector size {g})' if g else ''))
    if len(data) >= 2 * ss and data[ss:ss + 8] == SIG:
        p = Copy(data, ss, 'primary', 0, 1, path)
        out = [p]
        alt = p.h['alt']
        if (alt + 1) * ss <= len(data):                     # whole-disk image (small test images)
            bstart = p.h['last'] + 1
            out.append(Copy(data[bstart * ss:(alt + 1) * ss], ss, 'backup', bstart, alt - bstart, path))
        return out
    if len(data) >= ss and data[len(data) - ss:len(data) - ss + 8] == SIG:
        h = parse_header(data, len(data) - ss)
        nl = len(data) // ss
        return [Copy(data, ss, 'backup', h['cur'] - (nl - 1), nl - 1, path)]
    g = guess_sector(data)
    raise Stop(f'{path}: no GPT header found at LBA 1 nor in the last LBA with --sector {ss}'
               + (f'; the file looks like sector size {g}' if g and g != ss else ''))


def load_device(path, ss):
    """Read-only access to a block device (or opened read-only), primary + backup."""
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)

        def rd(lba, count):
            os.lseek(fd, lba * ss, os.SEEK_SET)
            return os.read(fd, count * ss)
        head = rd(0, 2)
        h = parse_header(head, ss)
        if not h or h['sig'] != SIG:
            raise Stop(f'{path}: no GPT header at LBA 1 with sector size {ss}')
        last_lba = size // ss - 1
        if h['alt'] != last_lba:
            raise Stop(f'{path}: primary says backup header is at LBA {h["alt"]}, disk last LBA is {last_lba}')
        p = Copy(rd(0, h['first']), ss, 'primary', 0, 1, path)
        bstart = h['last'] + 1
        b = Copy(rd(bstart, h['alt'] - bstart + 1), ss, 'backup', bstart, h['alt'] - bstart, path)
        return [p, b]
    finally:
        os.close(fd)


def load_pair(primary_path, backup_path, ss):
    a = load(primary_path, ss)
    b = load(backup_path, ss) if backup_path else []
    prim = [c for c in a + b if c.role == 'primary']
    back = [c for c in a + b if c.role == 'backup']
    if len(prim) != 1 or len(back) != 1:
        raise Stop(f'need exactly one primary and one backup copy, found {len(prim)} primary and {len(back)} backup')
    return prim[0], back[0]


def check_pair(p, b):
    errs = p.check() + b.check()
    if errs:
        return errs
    hp, hb = p.h, b.h
    for k in ('first', 'last', 'guid', 'n', 'esz', 'ecrc'):
        if hp[k] != hb[k]:
            errs.append(f'primary and backup differ in header field {k}')
    if hp['alt'] != hb['cur'] or hb['alt'] != hp['cur']:
        errs.append('primary/backup header LBAs do not point at each other')
    if p.entries() != b.entries():
        errs.append('primary and backup entry arrays differ')
    return errs


# ---------------------------------------------------------------- show
def print_table(c, gaps=True):
    h, ss = c.h, c.ss
    print(f'  {"#":>3} {"name":<20} {"first LBA":>12} {"last LBA":>12} {"sectors":>12}  {"size":>11}  {"bytes(hex)":>14}  1MiB-aligned')
    for p in sorted(c.parts(), key=lambda p: p['first']):
        n = p['last'] - p['first'] + 1
        al = 'yes' if (p['first'] * ss) % (1 << 20) == 0 else 'no'
        prot = '  (protected)' if is_protected(p['name']) else ''
        print(f'  {p["idx"]:>3} {p["name"]:<20} {p["first"]:>12} {p["last"]:>12} {n:>12}  {human(n * ss):>11}  {n * ss:>#14x}  {al}{prot}')
    if gaps:
        gl = free_gaps(c.parts(), h)
        print('  free gaps:' + ('' if gl else ' none'))
        for a, z in gl:
            print(f'      LBA {a}..{z}  ({human((z - a + 1) * ss)})')


def cmd_show(args):
    copies = []
    for f in args.files:
        copies += load(f, args.sector)
    errs = []
    for c in copies:
        h = c.h
        print(f'== {c.role} GPT in {c.path} (sector {c.ss})')
        e = c.check()
        if h and h['sig'] == SIG:
            print(f'  header at LBA {h["cur"]}, other header at LBA {h["alt"]}, usable LBA {h["first"]}..{h["last"]}')
            print(f'  entries at LBA {h["elba"]}: {h["n"]} x {h["esz"]} bytes; room on disk for {c.capacity()} entries; '
                  f'dump holds {c.image_capacity()}')
            print(f'  disk GUID {uuid.UUID(bytes_le=h["guid"])}')
            if c.role == 'primary':
                print(f'  disk size implied by header: {h["alt"] + 1} sectors = {human((h["alt"] + 1) * c.ss)}')
        for w in getattr(c, 'warns', []):
            print('  note: ' + w)
        if not e:
            print('  header CRC OK, entry array CRC OK, no overlaps')
            print_table(c)
        else:
            for x in e:
                print('  ERROR: ' + x)
        errs += e
    prim = [c for c in copies if c.role == 'primary']
    back = [c for c in copies if c.role == 'backup']
    if prim and back and not errs:
        pe = check_pair(prim[0], back[0])
        print('== primary vs backup: ' + ('consistent' if not pe else 'INCONSISTENT'))
        for x in pe:
            print('  ERROR: ' + x)
        errs += pe
    if errs:
        raise Stop(f'{len(errs)} problem(s) found')
    print('RESULT: OK')


# ---------------------------------------------------------------- dumpcmds
def cmd_dumpcmds(args):
    ss = args.sector
    data = read_file(args.head_dump)
    if len(data) < 34 * ss or len(data) % ss:
        raise Stop(f'{args.head_dump} must hold LBA 0..33 exactly: dd if=DEV of=FILE bs={ss} count=34')
    data = data[:34 * ss]
    h = parse_header(data, ss)
    if h['sig'] != SIG:
        g = guess_sector(data)
        raise Stop(f'no GPT header at LBA 1 with sector {ss}' + (f' (looks like sector size {g})' if g else ''))
    p = Copy(data, ss, 'primary', 0, 1, args.head_dump)
    errs = p.check()
    for e in errs:
        print('ERROR: ' + e)
    if errs:
        raise Stop('the primary GPT is not valid; do not continue on this disk')
    if (args.disk_size_512 * 512) % ss:
        raise Stop('disk size is not a multiple of the sector size: wrong --sector?')
    last_lba = args.disk_size_512 * 512 // ss - 1
    if h['alt'] != last_lba:
        raise Stop(f'primary header puts the backup header at LBA {h["alt"]} but the disk\'s last LBA is '
                   f'{last_lba}: wrong disk, wrong sector size, or a damaged GPT')
    d, r = args.dev, args.remote_dir.rstrip('/')
    pcount = p.lowest()                       # LBA 0 .. first partition - 1 (never partition data)
    bstart = p.highest() + 1                  # after the last partition .. last LBA
    print(f'# sector {ss}, usable LBA {h["first"]}..{h["last"]}, backup header at LBA {h["alt"]}')
    print(f'# first partition starts at LBA {p.lowest()}, last partition ends at LBA {p.highest()}')
    print(f'# primary dump: LBA 0..{pcount - 1} ({pcount} sectors)')
    print(f'# backup dump : LBA {bstart}..{h["alt"]} ({h["alt"] - bstart + 1} sectors)')
    print('# run these lines on the PC, one by one, and check that each one succeeds:')
    print(f'adb shell su -c "dd if={d} of={r}/gpt-primary.bin bs={ss} count={pcount}"')
    print(f'adb shell su -c "dd if={d} of={r}/gpt-backup.bin bs={ss} skip={bstart} count={h["alt"] - bstart + 1}"')
    print(f'adb shell su -c "chmod 644 {r}/gpt-primary.bin {r}/gpt-backup.bin"')
    print(f'adb shell su -c "sha256sum {r}/gpt-primary.bin {r}/gpt-backup.bin"')
    print(f'adb pull {r}/gpt-primary.bin gpt-primary.bin')
    print(f'adb pull {r}/gpt-backup.bin gpt-backup.bin')
    print(f'# expected sizes: gpt-primary.bin {pcount * ss} bytes, gpt-backup.bin {(h["alt"] - bstart + 1) * ss} bytes')


# ---------------------------------------------------------------- plan
def apply_ops(parts, h, ss, ops, align, allowed, min_remaining, take_from, esz, n, cap, add_type):
    """Mutates parts (list of dicts). Returns (n, changed_names, added_names)."""
    by = {p['name']: p for p in parts}
    asec = align // ss
    changed, added = [], []

    def need_name(nm):
        if nm not in by:
            raise Stop(f'no partition named {nm!r}')
        return by[nm]

    def may_change(nm):
        if is_protected(nm):
            raise Stop(f'{nm} is a protected partition and must never be moved or resized')
        if nm not in allowed:
            raise Stop(f'{nm} is not in the allowed set {sorted(allowed)} (use --allow-change {nm} only if you are sure)')

    def sectors(nbytes, what):
        if nbytes <= 0 or nbytes % align:
            raise Stop(f'{what}: {nbytes} bytes is not a positive multiple of the alignment {human(align)}')
        return nbytes // ss

    for op, arg in ops:
        if op == 'grow':
            nm, _, sz = arg.partition(':')
            p = need_name(nm)
            may_change(nm)
            amount = sectors(parse_size(sz), f'--grow {arg}')
            new_last = p['last'] + amount
            nxt = [q for q in parts if q['first'] > p['last']]
            nxt = min(nxt, key=lambda q: q['first']) if nxt else None
            limit = (nxt['first'] - 1) if nxt else h['last']
            if new_last > limit:
                if nxt is None:
                    raise Stop(f'--grow {arg}: not enough room before the end of the usable area')
                if take_from != nxt['name']:
                    raise Stop(f'--grow {arg}: needs {(new_last - limit)} sectors from the start of the next partition '
                               f'{nxt["name"]}; that DESTROYS {nxt["name"]}. Pass --take-from {nxt["name"]} to accept.')
                may_change(nxt['name'])
                if new_last >= nxt['last']:
                    raise Stop(f'--grow {arg}: would swallow all of {nxt["name"]}')
                if (new_last + 1) % asec:
                    raise Stop(f'new start of {nxt["name"]} (LBA {new_last + 1}) would not be {human(align)}-aligned')
                if (nxt['last'] - new_last) * ss < min_remaining:
                    raise Stop(f'{nxt["name"]} would shrink to {human((nxt["last"] - new_last) * ss)}, below '
                               f'--min-remaining {human(min_remaining)}')
                nxt['first'] = new_last + 1
                changed.append(nxt['name'])
            p['last'] = new_last
            changed.append(nm)
        elif op == 'shrink-end':
            nm, _, sz = arg.partition(':')
            p = need_name(nm)
            may_change(nm)
            newsec = sectors(parse_size(sz), f'--shrink-end {arg}')
            cur = p['last'] - p['first'] + 1
            if newsec >= cur:
                raise Stop(f'--shrink-end {arg}: new size {human(newsec * ss)} is not smaller than now ({human(cur * ss)})')
            if newsec * ss < min_remaining:
                raise Stop(f'--shrink-end {arg}: below --min-remaining {human(min_remaining)}')
            p['last'] = p['first'] + newsec - 1
            changed.append(nm)
        elif op == 'add':
            bits = arg.split(':')
            if len(bits) != 2 or not bits[0]:
                raise Stop(f'--add {arg}: use NAME:SIZE or NAME:rest')
            nm, sz = bits
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,36}', nm):
                raise Stop(f'--add {arg}: name must be 1-36 characters of A-Z a-z 0-9 _ . -')
            if nm in by:
                raise Stop(f'--add {arg}: a partition named {nm} already exists')
            if is_protected(nm):
                raise Stop(f'--add {arg}: {nm} collides with a protected MTK partition name')
            gaps = []
            for a, z in free_gaps(parts, h):
                a2 = -(-a // asec) * asec
                if a2 <= z:
                    gaps.append((a2, z))
            if not gaps:
                raise Stop(f'--add {arg}: no free space')
            a, z = max(gaps, key=lambda g: (g[1] - g[0], -g[0]))     # largest free gap
            if sz.lower() == 'rest':
                last = z
            else:
                last = a + sectors(parse_size(sz), f'--add {arg}') - 1
                if last > z:
                    raise Stop(f'--add {arg}: largest free gap is only {human((z - a + 1) * ss)}')
            slots = [i for i in range(n) if i not in {q['idx'] for q in parts}]
            idx = slots[0] if slots else n
            if idx >= n:
                n = idx + 1
            if n > cap:
                raise Stop(f'--add {arg}: {n} entries would not fit the entry area ({cap} entries)')
            q = dict(idx=idx, name=nm, first=a, last=last, type=uuid.UUID(add_type).bytes_le,
                     uniq=uuid.uuid4().bytes_le, attr=0)
            parts.append(q)
            by[nm] = q
            added.append(nm)
        else:
            raise Stop(f'unknown op {op}')
    return n, sorted(set(changed)), added


def build_copy(orig, parts, n):
    """New image = original image with new entry array, n, entry CRC and header CRC."""
    c = Copy(bytes(orig.img), orig.ss, orig.role, orig.start_lba, orig.hoff // orig.ss, orig.path)
    esz = c.h['esz']
    ent = bytearray(orig.entries(n) if orig.image_capacity() >= n else b'')
    if len(ent) != n * esz:
        raise Stop(f'{orig.role} dump is too short to hold {n} entries')
    old = orig.entries()
    by_idx = {p['idx']: p for p in parts}
    for i in range(n):
        p = by_idx.get(i)
        if p is None:
            if i < orig.h['n']:
                continue
            ent[i * esz:(i + 1) * esz] = b'\0' * esz
            continue
        if i < orig.h['n']:
            o = old[i * esz:(i + 1) * esz]
            t, u, f, l, a, nm = entry_fields(o)
            if (f, l) == (p['first'], p['last']):
                continue
            e = bytearray(o)
            e[32:48] = struct.pack('<QQ', p['first'], p['last'])     # only the LBAs change
        else:
            e = make_entry(p['type'], p['uniq'], p['first'], p['last'], p['attr'], p['name'], esz)
        ent[i * esz:(i + 1) * esz] = e
    c.img[c.eoff:c.eoff + n * esz] = ent
    h = dict(c.h)
    h['n'] = n
    h['ecrc'] = zlib.crc32(bytes(ent)) & 0xffffffff
    h['crc'] = 0
    c.img[c.hoff:c.hoff + HDR_SIZE] = pack_header(h)
    h['crc'] = header_crc(c.img, c.hoff, h['hsize'])
    c.img[c.hoff:c.hoff + HDR_SIZE] = pack_header(h)
    c.h = parse_header(c.img, c.hoff)
    return c


def compare(op, ob, np_, nb, expect_changed=None, expect_added=None, allowed=None):
    """Full verification of a new pair against the original pair. Returns (errors, changed, added)."""
    errs = []
    oe = check_pair(op, ob)
    if oe:
        return ['ORIGINAL: ' + x for x in oe], [], []
    ne = check_pair(np_, nb)
    if ne:
        return ['NEW: ' + x for x in ne], [], []
    for o, nw in ((op, np_), (ob, nb)):
        r = o.role
        if len(o.img) != len(nw.img) or o.start_lba != nw.start_lba:
            errs.append(f'{r}: new dump has a different size/position than the original')
            continue
        ho, hn = o.h, nw.h
        for k in ('sig', 'rev', 'hsize', 'reserved', 'cur', 'alt', 'first', 'last', 'guid', 'elba', 'esz'):
            if ho[k] != hn[k]:
                errs.append(f'{r}: header field {k} changed')
        if hn['n'] < ho['n']:
            errs.append(f'{r}: number of entries went down')
        if hn['n'] > nw.capacity():
            errs.append(f'{r}: {hn["n"]} entries do not fit the entry area ({nw.capacity()})')
        # everything outside header bytes and the (new) entry array must be byte-identical
        mask = [(nw.hoff, nw.hoff + HDR_SIZE), (nw.eoff, nw.eoff + hn['n'] * hn['esz'])]
        pos = 0
        for a, z in sorted(mask):
            if o.img[pos:a] != nw.img[pos:a]:
                errs.append(f'{r}: bytes {pos}..{a} outside header/entries changed')
            pos = z
        if o.img[pos:] != nw.img[pos:]:
            errs.append(f'{r}: bytes after offset {pos} outside header/entries changed')
    if errs:
        return errs, [], []
    esz = op.h['esz']
    oent, nent = op.entries(), np_.entries()
    changed, added = [], []
    for i in range(np_.h['n']):
        ne_ = nent[i * esz:(i + 1) * esz]
        oe_ = oent[i * esz:(i + 1) * esz] if i < op.h['n'] else b'\0' * esz
        if ne_ == oe_:
            continue
        nt, nu, nf, nl, na, nn = entry_fields(ne_)
        ot, ou, of, ol, oa, on = entry_fields(oe_)
        if ot == b'\0' * 16:
            added.append(nn)
            continue
        if nt == b'\0' * 16:
            errs.append(f'entry {i} ({on}) was deleted')
            continue
        if ne_[:32] != oe_[:32] or ne_[48:] != oe_[48:]:
            errs.append(f'entry {i} ({on}): type/GUID/attributes/name changed (only LBAs may change)')
        changed.append(on)
        if is_protected(on):
            errs.append(f'entry {i} ({on}) is PROTECTED and was moved/resized')
        elif allowed is not None and on not in allowed:
            errs.append(f'entry {i} ({on}) changed but is not in the allowed set {sorted(allowed)}')
    if expect_changed is not None and sorted(changed) != sorted(expect_changed):
        errs.append(f'changed partitions {sorted(changed)} != expected {sorted(expect_changed)}')
    if expect_added is not None and sorted(added) != sorted(expect_added):
        errs.append(f'added partitions {sorted(added)} != expected {sorted(expect_added)}')
    return errs, changed, added


def write_scripts(out, args, op, ob, np_, nb, changed, added, newparts):
    ss, d, r = args.sector, args.dev, args.remote_dir.rstrip('/')
    pcnt, bstart, bcnt = len(op.img) // ss, ob.start_lba, len(ob.img) // ss
    so_p, so_b, sn_p, sn_b = sha256(op.img), sha256(ob.img), sha256(np_.img), sha256(nb.img)
    common = f'''#!/system/bin/sh
# Run on the device as root: su -c sh {r}/SCRIPT
DEV={d}
SS={ss}
D={r}
fail() {{ echo "STOP: $*"; exit 1; }}
[ "$(id -u)" = 0 ] || fail "not root"
[ -b "$DEV" ] || fail "$DEV is not a block device"
B=$(basename "$DEV")
[ "$(cat /sys/block/$B/queue/logical_block_size)" = "$SS" ] || fail "sector size of $DEV is not $SS"
[ "$(cat /sys/block/$B/size)" = "{(ob.h['cur'] + 1) * ss // 512}" ] || fail "size of $DEV changed / wrong disk"
cur_p=$(dd if=$DEV bs=$SS count={pcnt} 2>/dev/null | sha256sum | cut -d' ' -f1)
cur_b=$(dd if=$DEV bs=$SS skip={bstart} count={bcnt} 2>/dev/null | sha256sum | cut -d' ' -f1)
'''
    wr = common + f'''# Writes the NEW GPT. Refuses unless the disk still holds exactly the ORIGINAL GPT.
for f in gpt-primary.bin:{so_p} gpt-backup.bin:{so_b} new-primary.bin:{sn_p} new-backup.bin:{sn_b}; do
  n=${{f%%:*}}; s=${{f#*:}}
  [ "$(sha256sum $D/$n | cut -d' ' -f1)" = "$s" ] || fail "$D/$n missing or wrong sha256"
done
if [ "$cur_p" = "{sn_p}" ] && [ "$cur_b" = "{sn_b}" ]; then echo "ALREADY WRITTEN: disk holds the new GPT"; exit 0; fi
[ "$cur_p" = "{so_p}" ] || fail "primary GPT on disk is not the backed-up original (re-dump and re-plan)"
[ "$cur_b" = "{so_b}" ] || fail "backup GPT on disk is not the backed-up original (re-dump and re-plan)"
[ "$1" = "--write" ] || {{ echo "CHECKS OK. Nothing written. Run again with --write to write."; exit 0; }}
dd if=$D/new-primary.bin of=$DEV bs=$SS count={pcnt} conv=notrunc,fsync || fail "primary write failed: RESTORE NOW"
dd if=$D/new-backup.bin of=$DEV bs=$SS seek={bstart} count={bcnt} conv=notrunc,fsync || fail "backup write failed: RESTORE NOW"
sync
echo 3 > /proc/sys/vm/drop_caches
p=$(dd if=$DEV bs=$SS count={pcnt} 2>/dev/null | sha256sum | cut -d' ' -f1)
b=$(dd if=$DEV bs=$SS skip={bstart} count={bcnt} 2>/dev/null | sha256sum | cut -d' ' -f1)
echo "primary readback $p"; echo "backup  readback $b"
[ "$p" = "{sn_p}" ] || fail "PRIMARY READBACK MISMATCH: run restore-gpt.sh --write"
[ "$b" = "{sn_b}" ] || fail "BACKUP READBACK MISMATCH: run restore-gpt.sh --write"
echo "WRITE OK"
'''
    rs = common + f'''# Puts the ORIGINAL GPT back.
for f in gpt-primary.bin:{so_p} gpt-backup.bin:{so_b}; do
  n=${{f%%:*}}; s=${{f#*:}}
  [ "$(sha256sum $D/$n | cut -d' ' -f1)" = "$s" ] || fail "$D/$n missing or wrong sha256"
done
if [ "$cur_p" = "{so_p}" ] && [ "$cur_b" = "{so_b}" ]; then echo "disk already holds the original GPT"; exit 0; fi
[ "$1" = "--write" ] || {{ echo "CHECKS OK. Nothing written. Run again with --write to restore."; exit 0; }}
dd if=$D/gpt-primary.bin of=$DEV bs=$SS count={pcnt} conv=notrunc,fsync || fail "primary write failed"
dd if=$D/gpt-backup.bin of=$DEV bs=$SS seek={bstart} count={bcnt} conv=notrunc,fsync || fail "backup write failed"
sync
echo 3 > /proc/sys/vm/drop_caches
p=$(dd if=$DEV bs=$SS count={pcnt} 2>/dev/null | sha256sum | cut -d' ' -f1)
b=$(dd if=$DEV bs=$SS skip={bstart} count={bcnt} 2>/dev/null | sha256sum | cut -d' ' -f1)
[ "$p" = "{so_p}" ] || fail "primary readback mismatch"
[ "$b" = "{so_b}" ] || fail "backup readback mismatch"
echo "RESTORE OK"
'''
    for nm, txt in (('write-gpt.sh', wr), ('restore-gpt.sh', rs)):
        with open(os.path.join(out, nm), 'w', newline='\n') as f:
            f.write(txt.replace('SCRIPT', nm))
    lines = [f'# plan summary (sector {ss}, device {d})', '']
    lines.append('changed: ' + (', '.join(changed) or 'none'))
    lines.append('added:   ' + (', '.join(added) or 'none'))
    lines.append('')
    lines.append('expected LK fastboot values after reboot (fastboot getvar partition-size:NAME):')
    for p in sorted(newparts, key=lambda p: p['first']):
        if p['name'] in changed or p['name'] in added:
            nb_ = (p['last'] - p['first'] + 1) * ss
            lines.append(f'  partition-size:{p["name"]}: {nb_:#x}  ({human(nb_)}, LBA {p["first"]}..{p["last"]})')
    lines.append('')
    lines.append(f'sha256 gpt-primary.bin {so_p}')
    lines.append(f'sha256 gpt-backup.bin  {so_b}')
    lines.append(f'sha256 new-primary.bin {sn_p}')
    lines.append(f'sha256 new-backup.bin  {sn_b}')
    lines.append('')
    lines.append(f'primary area: LBA 0..{pcnt - 1}; backup area: LBA {bstart}..{bstart + bcnt - 1}')
    with open(os.path.join(out, 'plan.txt'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return '\n'.join(lines)


def cmd_plan(args):
    ss = args.sector
    op, ob = load_pair(args.primary, args.backup, ss)
    errs = check_pair(op, ob)
    if errs:
        for e in errs:
            print('ERROR: ' + e)
        raise Stop('the original GPT is not consistent; do not edit it')
    if len(op.img) // ss > op.lowest():
        raise Stop(f'the primary dump covers LBA 0..{len(op.img) // ss - 1}, which reaches into partition data '
                   f'(first partition starts at LBA {op.lowest()}); dump only LBA 0..{op.lowest() - 1}')
    if ob.start_lba <= op.highest():
        raise Stop(f'the backup dump starts at LBA {ob.start_lba}, inside partition data; dump only from LBA '
                   f'{op.highest() + 1}')
    if not args.ops:
        raise Stop('no operation given (--grow, --shrink-end, --add)')
    align = parse_size(args.align)
    if align % ss:
        raise Stop('--align must be a multiple of the sector size')
    allowed = set(DEFAULT_ALLOWED) | set(args.allow_change or [])
    for nm in allowed:
        if is_protected(nm):
            raise Stop(f'{nm} is protected and can not be allowed')
    parts = [dict(p) for p in op.parts()]
    cap = min(op.capacity(), ob.capacity(), op.image_capacity(), ob.image_capacity())
    n, changed, added = apply_ops(parts, op.h, ss, args.ops, align, allowed, parse_size(args.min_remaining),
                                  args.take_from, op.h['esz'], op.h['n'], cap, args.add_type)
    e, _ = check_parts(parts, op.h, 'plan', op.h['elba'] + -(-n * op.h['esz'] // ss))
    for p in parts:
        if (p['name'] in changed or p['name'] in added) and (p['first'] < op.h['first'] or p['last'] > op.h['last']):
            e.append(f'plan: {p["name"]} would be outside the usable LBAs {op.h["first"]}..{op.h["last"]}')
    if e:
        for x in e:
            print('ERROR: ' + x)
        raise Stop('the planned table is invalid')
    np_, nb = build_copy(op, parts, n), build_copy(ob, parts, n)
    errs, ch2, ad2 = compare(op, ob, np_, nb, changed, added, allowed)
    if errs:
        for x in errs:
            print('ERROR: ' + x)
        raise Stop('self-verification of the plan failed')
    os.makedirs(args.out, exist_ok=True)
    for nm, blob in (('gpt-primary.bin', op.img), ('gpt-backup.bin', ob.img),
                     ('new-primary.bin', np_.img), ('new-backup.bin', nb.img)):
        with open(os.path.join(args.out, nm), 'wb') as f:
            f.write(bytes(blob))
    print('== BEFORE')
    print_table(op)
    print('== AFTER')
    print_table(np_)
    print(write_scripts(args.out, args, op, ob, np_, nb, ch2, ad2, parts))
    print(f'files written to {args.out}: gpt-primary.bin gpt-backup.bin new-primary.bin new-backup.bin '
          f'write-gpt.sh restore-gpt.sh plan.txt')
    print('RESULT: PLAN OK (verified)')


def cmd_verify(args):
    ss = args.sector
    op, ob = load_pair(args.orig_primary, args.orig_backup, ss)
    np_, nb = load_pair(args.new_primary, args.new_backup, ss)
    ec = args.expect_changed.split(',') if args.expect_changed is not None else None
    ea = args.expect_added.split(',') if args.expect_added is not None else None
    ec = [x for x in ec if x] if ec is not None else None
    ea = [x for x in ea if x] if ea is not None else None
    allowed = set(DEFAULT_ALLOWED) | set(args.allow_change or [])
    errs, changed, added = compare(op, ob, np_, nb, ec, ea, allowed)
    for x in errs:
        print('ERROR: ' + x)
    if errs:
        raise Stop('verification failed')
    print('changed:', ', '.join(changed) or 'none')
    print('added:  ', ', '.join(added) or 'none')
    print('== NEW TABLE')
    print_table(np_)
    print(f'sha256 new primary {sha256(np_.img)}')
    print(f'sha256 new backup  {sha256(nb.img)}')
    print('RESULT: VERIFY OK')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('show', help='list partitions and check a dump (or two)')
    s.add_argument('files', nargs='+')
    s.add_argument('--sector', type=int, required=True)

    s = sub.add_parser('dumpcmds', help='print exact dd commands for the full primary/backup dumps')
    s.add_argument('--head-dump', required=True, help='file with LBA 0..33 (dd bs=SS count=34)')
    s.add_argument('--sector', type=int, required=True)
    s.add_argument('--dev', required=True, help='e.g. /dev/block/sdc')
    s.add_argument('--disk-size-512', type=int, required=True, help='content of /sys/block/sdX/size')
    s.add_argument('--remote-dir', default='/data/local/tmp/gpt')

    s = sub.add_parser('plan', help='compute and self-verify a new GPT')
    s.add_argument('--primary', required=True)
    s.add_argument('--backup', required=True)
    s.add_argument('--sector', type=int, required=True)
    s.add_argument('--dev', required=True)
    s.add_argument('--out', required=True)
    s.add_argument('--remote-dir', default='/data/local/tmp/gpt')
    s.add_argument('--align', default='1MiB')
    s.add_argument('--min-remaining', default='1GiB', help='smallest size a shrunk partition may keep')
    s.add_argument('--take-from', help='name of the partition after the grown one whose START may move (wipes it)')
    s.add_argument('--allow-change', action='append', help='extra partition name that may change (never a protected one)')
    s.add_argument('--add-type', default=BASIC_DATA, help=f'type GUID for --add (default MTK basic data {BASIC_DATA})')
    s.add_argument('--grow', dest='ops', action='append', type=lambda v: ('grow', v), metavar='NAME:+SIZE')
    s.add_argument('--shrink-end', dest='ops', action='append', type=lambda v: ('shrink-end', v), metavar='NAME:NEWSIZE')
    s.add_argument('--add', dest='ops', action='append', type=lambda v: ('add', v), metavar='NAME:SIZE|NAME:rest')

    s = sub.add_parser('verify', help='verify new dumps against the original dumps')
    s.add_argument('--orig-primary', required=True)
    s.add_argument('--orig-backup', required=True)
    s.add_argument('--new-primary', required=True)
    s.add_argument('--new-backup', required=True)
    s.add_argument('--sector', type=int, required=True)
    s.add_argument('--expect-changed')
    s.add_argument('--expect-added')
    s.add_argument('--allow-change', action='append')

    args = ap.parse_args()
    if getattr(args, 'sector', None) not in (512, 4096):
        print('STOP: --sector must be 512 or 4096 (read /sys/block/<disk>/queue/logical_block_size)')
        return 1
    try:
        {'show': cmd_show, 'dumpcmds': cmd_dumpcmds, 'plan': cmd_plan, 'verify': cmd_verify}[args.cmd](args)
    except Stop as e:
        print(f'STOP: {e}')
        return 1
    except OSError as e:
        print(f'STOP: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
