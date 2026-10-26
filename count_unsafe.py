#!/usr/bin/env python3
"""The unsafe surface of the dot15d4 workspace: Table 12 and the counts of Section 5.7.

    python3 count_unsafe.py                  # dot15d4/, the source with the fixes
    python3 count_unsafe.py /tmp/d47e523     # commit d47e523, the counts of Table 12

Comments are removed and string and char literals blanked; a code line is a
non-blank line. A site is one use of the unsafe keyword (block, fn, impl, trait,
extern block, fn-pointer type), and its unsafe lines run from the keyword to its
closing brace or `;`, nested sites counted once. The nRF52840 backend is
dot15d4-driver/src/socs/, the generic layer the rest of dot15d4-driver/src/.
Test code is any tests.rs, tests/ folder or #[cfg(test)] module; panicking sites
are .unwrap(), .expect(, panic!( and unreachable!( outside it.
"""
import os
import re
import sys

LIB_CRATES = ["dot15d4", "dot15d4-driver", "dot15d4-frame", "dot15d4-util", "dot15d4-embassy"]
CHAR_LIT = re.compile(r"'(\\[nrt0'\"\\]|\\x[0-9a-fA-F]{2}|\\u\{[0-9a-fA-F]+\}|[^\\'])'")
RAW_STR = re.compile(r'b?r(#*)"')
UNSAFE = re.compile(r"\bunsafe\b")
EXTERN = r'(extern\s*("[^"]*"\s*)?)?'
PANIC = re.compile(r"\.unwrap\(\)|\.expect\(|\bpanic!\s*\(|\bunreachable!\s*\(")
SAFETY = re.compile(r"//[/!]?\s*safety\b", re.IGNORECASE)
TEST_MOD = re.compile(r"#\[cfg\(test\)\]\s*(pub(\([^)]*\))?\s+)?mod\s+\w+\s*\{")


def strip(src):
    out, i, n, depth = [], 0, len(src), 0
    while i < n:
        c, nxt = src[i], src[i + 1] if i + 1 < n else ""
        if depth:
            if c == "/" and nxt == "*":
                depth, i = depth + 1, i + 2
            elif c == "*" and nxt == "/":
                depth, i = depth - 1, i + 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            depth, i = 1, i + 2
            continue
        ident = i > 0 and (src[i - 1].isalnum() or src[i - 1] == "_")
        m = RAW_STR.match(src, i)
        if m and not ident:
            end = src.find('"' + m.group(1), m.end())
            end = n if end < 0 else end
            out.append('""' + "\n" * src[i:end + 1 + len(m.group(1))].count("\n"))
            i = end + 1 + len(m.group(1))
            continue
        if c == '"' or (c == "b" and nxt == '"' and not ident):
            j = i + (2 if c == "b" else 1)
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append('""' + "\n" * src[i:j + 1].count("\n"))
            i = j + 1
            continue
        if c == "'":
            m = CHAR_LIT.match(src, i)
            if m:
                out.append("' '")
                i = m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


def match_brace(text, k):
    depth = 0
    for j in range(k, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if text[j] == "}" and depth == 0:
            return j
    return len(text) - 1


def item_end(text, k):
    b, s = text.find("{", k), text.find(";", k)
    return s if s != -1 and (b == -1 or s < b) else match_brace(text, b)


def sites(text):
    found = []
    for m in UNSAFE.finditer(text):
        k = m.end()
        while k < len(text) and text[k] in " \t\n":
            k += 1
        rest = text[k:k + 60]
        if rest.startswith("{"):
            found.append((m.start(), match_brace(text, k)))
        elif re.match(r'extern\s*("[^"]*"\s*)?\{', rest):
            found.append((m.start(), match_brace(text, text.find("{", k))))
        elif re.match(EXTERN + r"fn\s*\(", rest):
            found.append((m.start(), m.end()))
        elif re.match(EXTERN + r"fn\b", rest) or re.match(r"(impl|trait)\b", rest):
            found.append((m.start(), item_end(text, k)))
    return found


class File:
    def __init__(self, path):
        self.path = path
        self.src = open(path, encoding="utf-8").read()
        self.text = strip(self.src)
        lines = self.text.split("\n")
        self.code = [bool(line.strip()) for line in lines]
        self.starts = [0]
        for line in lines:
            self.starts.append(self.starts[-1] + len(line) + 1)
        self.sites = sites(self.text)
        self.unsafe_lines = {ln for s, e in self.sites
                             for ln in range(self.line_of(s), self.line_of(e) + 1) if self.code[ln]}

    def line_of(self, off):
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if self.starts[mid] <= off else (lo, mid - 1)
        return lo

    def test_lines(self):
        parts = self.path.replace(os.sep, "/").split("/")
        if parts[-1] == "tests.rs" or "tests" in parts[:-1]:
            return set(range(len(self.code)))
        return {ln for m in TEST_MOD.finditer(self.text)
                for ln in range(self.line_of(m.start()),
                                self.line_of(match_brace(self.text, self.text.find("{", m.start()))) + 1)}


def rs_files(root):
    return sorted(os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.endswith(".rs"))


def table12(ws):
    nrf = os.path.join(ws, "dot15d4-driver", "src", "socs")
    driver = rs_files(os.path.join(ws, "dot15d4-driver", "src"))
    rows = [("dot15d4 (MAC and TSCH scheduler)", rs_files(os.path.join(ws, "dot15d4", "src"))),
            ("dot15d4-driver, generic layer", [f for f in driver if not f.startswith(nrf + os.sep)]),
            ("dot15d4-driver, nRF52840 backend", rs_files(nrf)),
            ("dot15d4-frame (parser and builder)", rs_files(os.path.join(ws, "dot15d4-frame", "src"))),
            ("dot15d4-util (channels, pool, tokens)", rs_files(os.path.join(ws, "dot15d4-util", "src"))),
            ("dot15d4-embassy (integration adapter)", rs_files(os.path.join(ws, "dot15d4-embassy", "src")))]
    fmt = "%-40s %9s %6s %11s %7s"
    print(fmt % ("Crate", "Code LOC", "Sites", "Unsafe LOC", "Share"))
    total = [0, 0, 0]
    for name, paths in rows:
        files = [File(p) for p in paths]
        row = [sum(sum(f.code) for f in files), sum(len(f.sites) for f in files),
               sum(len(f.unsafe_lines) for f in files)]
        total = [a + b for a, b in zip(total, row)]
        print(fmt % (name, row[0], row[1], row[2], "%.1f%%" % (100.0 * row[2] / row[0])))
    print(fmt % ("Library total", total[0], total[1], total[2], "%.1f%%" % (100.0 * total[2] / total[0])))
    print("Safe share: %.1f%%" % (100.0 - 100.0 * total[2] / total[0]))


def section57(ws):
    loc_t = loc_n = u_t = u_n = panics = safety = 0
    for crate in LIB_CRATES:
        for p in rs_files(os.path.join(ws, crate, "src")):
            f = File(p)
            tests = f.test_lines()
            loc_t += sum(1 for i, c in enumerate(f.code) if c and i in tests)
            loc_n += sum(1 for i, c in enumerate(f.code) if c and i not in tests)
            u_t += sum(1 for i in f.unsafe_lines if i in tests)
            u_n += sum(1 for i in f.unsafe_lines if i not in tests)
            panics += sum(1 for m in PANIC.finditer(f.text) if f.line_of(m.start()) not in tests)
            safety += len(SAFETY.findall(f.src))
    example = sum(len(File(p).sites) for p in rs_files(os.path.join(ws, "examples", "nrf52840", "src")))
    print()
    print("Unit-test code:               %d LOC, %d unsafe LOC" % (loc_t, u_t))
    print("Library without tests:        %d LOC, %d unsafe LOC (%.1f%%)" % (loc_n, u_n, 100.0 * u_n / loc_n))
    print("Panicking sites outside tests: %d" % panics)
    print("Safety comments:               %d" % safety)
    print("Unsafe sites in the example application: %d" % example)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    workspace = args[0] if args else "dot15d4"
    table12(workspace)
    section57(workspace)
