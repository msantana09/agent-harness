"""A stand-in for the one `ps` form codex_jobs runs: `ps -ww -o stat=,command= -p <pid>`.

The tests put it first on PATH only where running `ps` is refused (a sandbox that denies the
binary). It reads the same facts from libproc and the kern.procargs2 sysctl: the state letter
and the command line. A pid that does not exist prints nothing and exits 1, as ps does. macOS
only.
"""

import ctypes
import struct
import sys

PROC_PIDTBSDINFO = 3
CTL_KERN, KERN_PROCARGS2 = 1, 49
STATE_LETTERS = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}


class BsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def bsd_info(pid: int) -> BsdInfo | None:
    info = BsdInfo()
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    size = ctypes.sizeof(info)
    if libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), size) != size:
        return None
    return info


def command_line(pid: int) -> str:
    libc = ctypes.CDLL("/usr/lib/libc.dylib")
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return ""
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return ""
    raw = buffer.raw[: size.value]
    (argc,) = struct.unpack_from("i", raw)
    rest = raw[4:].split(b"\0", 1)[1].lstrip(b"\0")  # past the executable path and its padding
    return " ".join(part.decode(errors="replace") for part in rest.split(b"\0")[:argc])


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[:3] != ["-ww", "-o", "stat=,command="] or argv[3] != "-p":
        raise SystemExit(f"ps stand-in: expected -ww -o stat=,command= -p <pid>, got {argv}")
    pid = int(argv[4])
    info = bsd_info(pid)
    if info is None:
        return 1
    print(f"{STATE_LETTERS.get(info.pbi_status, '?')} {command_line(pid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
